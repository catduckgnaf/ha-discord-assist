"""Approval buttons for sensitive actions (permissions phase C)."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.discord_assist.approvals import (
    APPROVE_PREFIX,
    DENY_PREFIX,
    ApprovalManager,
    ApprovalPolicy,
    ApprovalStore,
)
from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.const import (
    CONF_AGENT_ID,
    CONF_APPROVERS,
    CONF_BOT_ID,
    CONF_CHANNELS,
    CONF_CONFIRM_DOMAINS,
    CONF_TOKEN,
    CONF_USER_MAP,
    DOMAIN,
    EVENT_APPROVAL,
    SERVICE_REQUEST_APPROVAL,
)
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import ConversationRouter

ENTRY_ID = "entry-35"
CHANNEL_ID = 4242
REQUESTER = 111
APPROVER = 222
STRANGER = 333
UNMAPPED = 999999

CLIENT = "custom_components.discord_assist.DiscordAssistClient"


@pytest.fixture(autouse=True)
async def auto_enable_custom_integrations(
    enable_custom_integrations, setup_core_components
):
    """Load the custom integration so setup and flows run the real path."""
    yield


class FakeMessage:
    def __init__(self, message_id, content):
        self.id = message_id
        self.content = content
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []
        self.messages = {}
        self._next_id = 5000

    async def send(self, content, **kwargs):
        self._next_id += 1
        message = FakeMessage(self._next_id, content)
        self.messages[message.id] = message
        self.sent.append({"content": content, "message": message, **kwargs})
        return message

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class FakeClient:
    """The slice of the Discord client the approval manager uses."""

    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel if channel_id == self._channel.id else None

    async def fetch_channel(self, channel_id):
        return self.get_channel(channel_id)


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.sent_messages = []
        self.edits = []

    async def defer(self):
        self.deferred = True

    async def send_message(self, content, *, ephemeral=False):
        self.sent_messages.append({"content": content, "ephemeral": ephemeral})

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class FakeInteraction:
    def __init__(self, custom_id, user_id, display_name="User"):
        self.data = {"custom_id": custom_id}
        self.user = SimpleNamespace(id=user_id, display_name=display_name)
        self.response = FakeResponse()
        self.original_edits = []

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)


@pytest.fixture
def executed(hass: HomeAssistant):
    """Register capture handlers for the services approvals can execute."""
    calls = []

    async def _handler(call):
        calls.append(call)

    hass.services.async_register("cover", "close_cover", _handler)
    hass.services.async_register("lock", "unlock", _handler)
    return calls


def _capture_events(hass: HomeAssistant):
    events = []
    hass.bus.async_listen(EVENT_APPROVAL, events.append)
    return events


def _manager(hass: HomeAssistant, *, policy=None):
    router = ConversationRouter(
        hass=hass,
        agent_id=None,
        language=None,
        channels=set(),
        respond_dms=True,
        respond_mentions=True,
        allowlist=set(),
        user_map={
            REQUESTER: "ha-requester",
            APPROVER: "ha-approver",
            STRANGER: "ha-stranger",
        },
        fallback_user=None,
        cache=ConversationCache(timedelta(minutes=15)),
    )
    manager = ApprovalManager(
        hass,
        ENTRY_ID,
        router,
        ApprovalStore(hass, ENTRY_ID),
        policy or ApprovalPolicy(),
    )
    channel = FakeChannel(CHANNEL_ID)
    manager.bind_client(FakeClient(channel))
    return manager, channel


async def _request(manager, **overrides):
    kwargs = {
        "channel_id": CHANNEL_ID,
        "requester_discord_id": REQUESTER,
        "domain": "cover",
        "service": "close_cover",
        "service_data": {"entity_id": "cover.garage"},
        "description": "Close the garage cover?",
    }
    kwargs.update(overrides)
    return await manager.async_request_approval(**kwargs)


def _press(record, action, user_id, name="User"):
    prefix = APPROVE_PREFIX if action == "approve" else DENY_PREFIX
    return FakeInteraction(f"{prefix}{record.record_id}", user_id, name)


def test_policy_from_options():
    policy = ApprovalPolicy.from_options(
        {
            CONF_CONFIRM_DOMAINS: ["Lock", " cover "],
            CONF_APPROVERS: ["222", "not-a-number", 333],
        }
    )
    assert policy.requires_confirmation("lock")
    assert policy.requires_confirmation("COVER")
    assert not policy.requires_confirmation("light")
    assert policy.approvers == frozenset({APPROVER, STRANGER})
    # Locks and alarms need a second person by default; covers do not.
    assert not policy.self_approve_default("lock")
    assert not policy.self_approve_default("alarm_control_panel")
    assert policy.self_approve_default("cover")


async def test_request_posts_buttons_and_audits_without_executing(
    hass: HomeAssistant, executed
):
    manager, channel = _manager(hass)
    events = _capture_events(hass)
    record = await _request(manager)
    await hass.async_block_till_done()

    assert executed == []
    assert len(channel.sent) == 1
    sent = channel.sent[0]
    assert "Close the garage cover?" in sent["content"]
    custom_ids = {item.custom_id for item in sent["view"].children}
    assert custom_ids == {
        f"{APPROVE_PREFIX}{record.record_id}",
        f"{DENY_PREFIX}{record.record_id}",
    }
    assert record.message_id == sent["message"].id
    assert manager.store.get(record.record_id) is record

    assert len(events) == 1
    data = events[0].data
    assert data["outcome"] == "requested"
    assert data["requester_discord_id"] == REQUESTER
    assert data["requester_ha_user_id"] == "ha-requester"
    assert data["presser_discord_id"] is None
    assert data["domain"] == "cover"


async def test_approve_by_requester_executes_once_as_requester(
    hass: HomeAssistant, executed
):
    manager, _channel = _manager(hass)
    events = _capture_events(hass)
    record = await _request(manager)

    interaction = _press(record, "approve", REQUESTER, "Requester")
    assert await manager.async_handle_interaction(interaction) is True
    await hass.async_block_till_done()

    assert len(executed) == 1
    call = executed[0]
    assert call.domain == "cover"
    assert call.service == "close_cover"
    assert call.data == {"entity_id": "cover.garage"}
    # Executed as the requester, with a real identity, always.
    assert call.context.user_id == "ha-requester"

    assert interaction.response.deferred is True
    edit = interaction.original_edits[-1]
    assert edit["view"] is None
    assert "Approved by Requester." in edit["content"]
    assert manager.store.get(record.record_id) is None
    assert [event.data["outcome"] for event in events] == ["requested", "approved"]
    assert events[-1].data["presser_discord_id"] == REQUESTER


async def test_deny_executes_nothing_and_retires(hass: HomeAssistant, executed):
    manager, _channel = _manager(hass)
    events = _capture_events(hass)
    record = await _request(manager)

    interaction = _press(record, "deny", REQUESTER, "Requester")
    assert await manager.async_handle_interaction(interaction) is True
    await hass.async_block_till_done()

    assert executed == []
    edit = interaction.response.edits[-1]
    assert edit["view"] is None
    assert "Denied by Requester." in edit["content"]
    assert manager.store.get(record.record_id) is None
    assert [event.data["outcome"] for event in events] == ["requested", "denied"]


async def test_stranger_press_refused_and_record_survives(
    hass: HomeAssistant, executed
):
    manager, _channel = _manager(hass)
    events = _capture_events(hass)
    record = await _request(manager)

    interaction = _press(record, "approve", STRANGER, "Stranger")
    assert await manager.async_handle_interaction(interaction) is True
    await hass.async_block_till_done()

    assert executed == []
    refusal = interaction.response.sent_messages[-1]
    assert refusal["ephemeral"] is True
    assert "Only the requester or an approver" in refusal["content"]
    # The record is not consumed: the requester can still approve.
    assert manager.store.get(record.record_id) is record
    assert events[-1].data["outcome"] == "refused"
    assert events[-1].data["presser_discord_id"] == STRANGER

    follow_up = _press(record, "approve", REQUESTER, "Requester")
    assert await manager.async_handle_interaction(follow_up) is True
    assert len(executed) == 1


async def test_approver_approves_lock_and_it_runs_as_requester(
    hass: HomeAssistant, executed
):
    policy = ApprovalPolicy(approvers=frozenset({APPROVER}))
    manager, _channel = _manager(hass, policy=policy)
    events = _capture_events(hass)
    record = await _request(
        manager,
        domain="lock",
        service="unlock",
        service_data={"entity_id": "lock.front_door"},
        description="Unlock the front door?",
    )

    interaction = _press(record, "approve", APPROVER, "Approver")
    assert await manager.async_handle_interaction(interaction) is True
    await hass.async_block_till_done()

    assert len(executed) == 1
    # The approver pressed, but the call runs as the requester.
    assert executed[0].context.user_id == "ha-requester"
    assert events[-1].data["outcome"] == "approved"
    assert events[-1].data["presser_discord_id"] == APPROVER


async def test_lock_requester_cannot_self_approve_by_default(
    hass: HomeAssistant, executed
):
    policy = ApprovalPolicy(approvers=frozenset({APPROVER}))
    manager, _channel = _manager(hass, policy=policy)
    events = _capture_events(hass)
    record = await _request(
        manager,
        domain="lock",
        service="unlock",
        service_data={"entity_id": "lock.front_door"},
    )

    interaction = _press(record, "approve", REQUESTER, "Requester")
    assert await manager.async_handle_interaction(interaction) is True
    await hass.async_block_till_done()

    assert executed == []
    refusal = interaction.response.sent_messages[-1]
    assert refusal["ephemeral"] is True
    assert "cannot approve your own" in refusal["content"]
    assert manager.store.get(record.record_id) is record
    assert events[-1].data["outcome"] == "refused"

    # An approver can still close it out.
    deny = _press(record, "deny", APPROVER, "Approver")
    assert await manager.async_handle_interaction(deny) is True
    assert executed == []
    assert events[-1].data["outcome"] == "denied"


async def test_self_approve_override_allows_lock_self_approval(
    hass: HomeAssistant, executed
):
    manager, _channel = _manager(hass)
    record = await _request(
        manager,
        domain="lock",
        service="unlock",
        service_data={"entity_id": "lock.front_door"},
        self_approve=True,
    )
    interaction = _press(record, "approve", REQUESTER, "Requester")
    assert await manager.async_handle_interaction(interaction) is True
    assert len(executed) == 1
    assert executed[0].context.user_id == "ha-requester"


async def test_expired_press_executes_nothing_and_retires(
    hass: HomeAssistant, executed
):
    manager, _channel = _manager(hass)
    events = _capture_events(hass)
    record = await _request(manager, ttl_minutes=0)

    interaction = _press(record, "approve", REQUESTER, "Requester")
    assert await manager.async_handle_interaction(interaction) is True
    await hass.async_block_till_done()

    assert executed == []
    edit = interaction.response.edits[-1]
    assert edit["view"] is None
    assert "Expired without approval." in edit["content"]
    assert manager.store.get(record.record_id) is None
    assert [event.data["outcome"] for event in events] == ["requested", "expired"]


async def test_double_press_executes_once(hass: HomeAssistant, executed):
    manager, _channel = _manager(hass)
    record = await _request(manager)

    first = _press(record, "approve", REQUESTER, "Requester")
    assert await manager.async_handle_interaction(first) is True
    second = _press(record, "approve", REQUESTER, "Requester")
    assert await manager.async_handle_interaction(second) is True
    await hass.async_block_till_done()

    assert len(executed) == 1
    refusal = second.response.sent_messages[-1]
    assert refusal["ephemeral"] is True
    assert "already been handled" in refusal["content"]


async def test_unmapped_requester_fails_closed(hass: HomeAssistant, executed):
    manager, channel = _manager(hass)
    events = _capture_events(hass)
    with pytest.raises(ServiceValidationError):
        await _request(manager, requester_discord_id=UNMAPPED)
    await hass.async_block_till_done()
    assert channel.sent == []
    assert executed == []
    assert events == []


async def test_expiry_sweep_retires_buttons(hass: HomeAssistant, executed):
    manager, channel = _manager(hass)
    events = _capture_events(hass)
    record = await _request(manager, ttl_minutes=0)

    await manager.async_expire_sweep()
    await hass.async_block_till_done()

    assert executed == []
    assert manager.store.get(record.record_id) is None
    message = channel.messages[record.message_id]
    edit = message.edits[-1]
    assert edit["view"] is None
    assert "Expired without approval." in edit["content"]
    expired = [e for e in events if e.data["outcome"] == "expired"]
    assert len(expired) == 1
    assert expired[0].data["presser_discord_id"] is None


async def test_store_persists_records_and_drops_expired(hass: HomeAssistant):
    manager, _channel = _manager(hass)
    record = await _request(manager)
    expired = await _request(manager, ttl_minutes=0)
    await hass.async_block_till_done()

    store = ApprovalStore(hass, ENTRY_ID)
    await store.async_load()
    loaded = store.get(record.record_id)
    assert loaded is not None
    assert loaded.domain == "cover"
    assert loaded.requester_ha_user_id == "ha-requester"
    assert loaded.message_id == record.message_id
    # A restart never resurrects a request whose window has closed.
    assert store.get(expired.record_id) is None


async def test_non_approval_interactions_are_ignored(hass: HomeAssistant):
    manager, _channel = _manager(hass)
    other = FakeInteraction("da:act:some-record", REQUESTER)
    assert await manager.async_handle_interaction(other) is False
    assert other.response.sent_messages == []
    no_data = SimpleNamespace(data=None, user=SimpleNamespace(id=REQUESTER))
    assert await manager.async_handle_interaction(no_data) is False


async def test_bot_on_interaction_delegates_to_approvals(monkeypatch):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=object(), router=MagicMock(), status_signal="test_signal"
        )
    # No manager wired: a press is a no-op, not an error.
    await client.on_interaction(SimpleNamespace())

    handler = AsyncMock(return_value=True)
    client.approvals = SimpleNamespace(async_handle_interaction=handler)
    interaction = SimpleNamespace()
    await client.on_interaction(interaction)
    handler.assert_awaited_once_with(interaction)


class _SetupFakeClient:
    """A DiscordAssistClient stand-in good enough for entry setup."""

    def __init__(self, channel):
        self._channel = channel
        self.connected = False
        self.approvals = None

    async def login(self, token):
        pass

    async def connect(self, reconnect=True):
        pass

    async def close(self):
        pass

    def is_closed(self):
        return True

    def get_channel(self, channel_id):
        return self._channel if channel_id == self._channel.id else None

    async def fetch_channel(self, channel_id):
        return self.get_channel(channel_id)


async def test_request_approval_service_end_to_end(hass: HomeAssistant, executed):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options={
            CONF_AGENT_ID: "conversation.ollama",
            CONF_USER_MAP: {str(REQUESTER): "ha-requester"},
            CONF_APPROVERS: [str(APPROVER)],
        },
    )
    entry.add_to_hass(hass)
    channel = FakeChannel(CHANNEL_ID)
    events = _capture_events(hass)

    with patch(CLIENT, return_value=_SetupFakeClient(channel)):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()
        assert hass.services.has_service(DOMAIN, SERVICE_REQUEST_APPROVAL)

        await hass.services.async_call(
            DOMAIN,
            SERVICE_REQUEST_APPROVAL,
            {
                "config_entry_id": entry.entry_id,
                "channel_id": str(CHANNEL_ID),
                "requester_discord_id": str(REQUESTER),
                "domain": "cover",
                "service": "close_cover",
                "service_data": {"entity_id": "cover.garage"},
                "description": "Close the garage cover?",
            },
            blocking=True,
        )
        await hass.async_block_till_done()

        # Nothing ran yet; the buttons are waiting in the channel.
        assert executed == []
        assert len(channel.sent) == 1
        view = channel.sent[0]["view"]
        approve_id = next(
            item.custom_id
            for item in view.children
            if item.custom_id.startswith(APPROVE_PREFIX)
        )

        manager = entry.runtime_data.approvals
        press = FakeInteraction(approve_id, APPROVER, "Approver")
        assert await manager.async_handle_interaction(press) is True
        await hass.async_block_till_done()
        assert len(executed) == 1
        assert executed[0].context.user_id == "ha-requester"
        assert [e.data["outcome"] for e in events] == ["requested", "approved"]

        assert await hass.config_entries.async_unload(entry.entry_id) is True
        assert not hass.services.has_service(DOMAIN, SERVICE_REQUEST_APPROVAL)


async def test_options_settings_store_approval_fields(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="botid-1",
        data={CONF_TOKEN: "tok", CONF_BOT_ID: "botid-1"},
        options={CONF_AGENT_ID: "conversation.ollama", CONF_USER_MAP: {}},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.discord_assist.config_flow.list_text_channels",
        new=AsyncMock(return_value=[("555", "Home / #general")]),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "settings"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_AGENT_ID: "conversation.ollama",
                CONF_CHANNELS: ["555"],
                CONF_CONFIRM_DOMAINS: ["lock"],
                CONF_APPROVERS: [str(APPROVER)],
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_CONFIRM_DOMAINS] == ["lock"]
    assert entry.options[CONF_APPROVERS] == [str(APPROVER)]
