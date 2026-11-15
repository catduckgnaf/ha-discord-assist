"""Tests for the deterministic camera snapshot command (issue #20)."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)

from custom_components.discord_assist.bot import DiscordAssistClient
from custom_components.discord_assist.conversation_cache import ConversationCache
from custom_components.discord_assist.conversation_router import ConversationRouter
from custom_components.discord_assist.snapshot import (
    DOWNSCALE_WIDTH,
    LARGE_IMAGE_BYTES,
    SnapshotResult,
    parse_show_me_command,
    parse_snapshot_command,
)

_GET_IMAGE = "custom_components.discord_assist.snapshot._fetch_image"
_SHOULD_EXPOSE = "custom_components.discord_assist.snapshot.async_should_expose"

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def _state(entity_id="camera.driveway", name="Driveway"):
    return SimpleNamespace(entity_id=entity_id, name=name)


def _user(readable=True):
    return SimpleNamespace(
        permissions=SimpleNamespace(
            check_entity=lambda entity_id, key: readable and key == "read"
        )
    )


def _router(hass, **overrides):
    defaults = dict(
        hass=hass,
        agent_id="conversation.ollama",
        language=None,
        channels={111},
        respond_dms=True,
        respond_mentions=True,
        allowlist=set(),
        user_map={7: "ha-a"},
        fallback_user=None,
        cache=ConversationCache(timedelta(minutes=15)),
        speaker_context=False,
    )
    defaults.update(overrides)
    return ConversationRouter(**defaults)


def _fake_hass(states, user):
    return SimpleNamespace(
        states=SimpleNamespace(async_all=lambda domain=None: list(states)),
        auth=SimpleNamespace(async_get_user=AsyncMock(return_value=user)),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("snapshot driveway", "driveway"),
        ("snapshot camera.driveway", "camera.driveway"),
        ("Snapshot Driveway Camera!", "Driveway Camera"),
        ("snapshot", ""),
        ("turn off the lights", None),
        ("show me the driveway", None),
        ("please snapshot the driveway", None),
        ("snapshot: porch", "porch"),
        ("snapshots of the backup failed", None),
        ("Snapshotting HA now", None),
    ],
)
def test_parse_snapshot_command(text, expected):
    assert parse_snapshot_command(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("show me the driveway", "driveway"),
        ("show me driveway", "driveway"),
        ("show me how to bake bread", "how to bake bread"),
        ("snapshot driveway", None),
    ],
)
def test_parse_show_me_command(text, expected):
    assert parse_show_me_command(text) == expected


async def test_exposed_readable_camera_attaches_snapshot():
    hass = _fake_hass([_state()], _user(readable=True))
    router = _router(hass)
    image = SimpleNamespace(content=JPEG, content_type="image/jpeg")
    with (
        patch(_SHOULD_EXPOSE, return_value=True),
        patch(_GET_IMAGE, new=AsyncMock(return_value=image)) as get_image,
    ):
        result = await router.process_snapshot(query="driveway", discord_user_id=7)
    assert result.image_content == JPEG
    assert result.filename == "snapshot.jpg"
    assert "Driveway" in result.text
    get_image.assert_awaited_once()
    args, kwargs = get_image.call_args
    assert args[1] == "camera.driveway"
    assert kwargs["timeout"] == 10


async def test_non_exposed_camera_is_refused_and_never_fetched():
    hass = _fake_hass([_state()], _user(readable=True))
    router = _router(hass)
    with (
        patch(_SHOULD_EXPOSE, return_value=False),
        patch(_GET_IMAGE, new=AsyncMock()) as get_image,
    ):
        result = await router.process_snapshot(query="driveway", discord_user_id=7)
    assert result.image_content is None
    assert "could not find" in result.text
    get_image.assert_not_called()


async def test_unreadable_camera_is_refused_and_never_fetched():
    hass = _fake_hass([_state()], _user(readable=False))
    router = _router(hass)
    with (
        patch(_SHOULD_EXPOSE, return_value=True),
        patch(_GET_IMAGE, new=AsyncMock()) as get_image,
    ):
        result = await router.process_snapshot(query="driveway", discord_user_id=7)
    assert result.image_content is None
    assert "could not find" in result.text
    get_image.assert_not_called()


async def test_snapshot_refuses_unauthorized_discord_user():
    hass = _fake_hass([_state()], _user())
    router = _router(hass, user_map={})
    with pytest.raises(PermissionError):
        await router.process_snapshot(query="driveway", discord_user_id=7)


async def test_snapshot_by_entity_id_and_ambiguous_name():
    states = [
        _state("camera.driveway", "Driveway"),
        _state("camera.driveway_east", "Driveway East"),
    ]
    hass = _fake_hass(states, _user())
    router = _router(hass)
    image = SimpleNamespace(content=JPEG, content_type="image/jpeg")
    with (
        patch(_SHOULD_EXPOSE, return_value=True),
        patch(_GET_IMAGE, new=AsyncMock(return_value=image)) as get_image,
    ):
        by_id = await router.process_snapshot(
            query="camera.driveway", discord_user_id=7
        )
        assert by_id.image_content == JPEG
        assert get_image.call_args.args[1] == "camera.driveway"
        ambiguous = await router.process_snapshot(query="drive", discord_user_id=7)
    # "drive" is a substring of both names: refuse instead of guessing.
    assert ambiguous.image_content is None
    assert "More than one camera" in ambiguous.text


async def test_exact_only_skips_fuzzy_camera_matches():
    """'show me the front door status' must not grab the Front Door camera."""
    hass = _fake_hass([_state("camera.front_door", "Front Door")], _user())
    router = _router(hass)
    with (
        patch(_SHOULD_EXPOSE, return_value=True),
        patch(_GET_IMAGE, new=AsyncMock()) as get_image,
    ):
        fuzzy = await router.process_snapshot(
            query="front door status", discord_user_id=7, exact_only=True
        )
    assert fuzzy.matched is False
    assert fuzzy.image_content is None
    get_image.assert_not_called()


async def test_large_snapshot_is_downscaled_by_width():
    hass = _fake_hass([_state()], _user())
    router = _router(hass)
    big = SimpleNamespace(
        content=b"x" * (LARGE_IMAGE_BYTES + 1), content_type="image/jpeg"
    )
    small = SimpleNamespace(content=JPEG, content_type="image/jpeg")
    with (
        patch(_SHOULD_EXPOSE, return_value=True),
        patch(_GET_IMAGE, new=AsyncMock(side_effect=[big, small])) as get_image,
    ):
        result = await router.process_snapshot(query="driveway", discord_user_id=7)
    assert result.image_content == JPEG
    assert get_image.await_count == 2
    assert get_image.call_args_list[1].kwargs["width"] == DOWNSCALE_WIDTH


async def test_camera_fetch_failure_is_a_plain_refusal():
    hass = _fake_hass([_state()], _user())
    router = _router(hass)
    with (
        patch(_SHOULD_EXPOSE, return_value=True),
        patch(_GET_IMAGE, new=AsyncMock(side_effect=RuntimeError("offline"))),
    ):
        result = await router.process_snapshot(query="driveway", discord_user_id=7)
    assert result.image_content is None
    assert "could not get a snapshot" in result.text


async def test_real_hass_exposed_owner_camera_attaches(hass, setup_core_components):
    """End-to-end against real exposed-entities and permission machinery."""
    user = await hass.auth.async_create_user("Owner")
    hass.states.async_set("camera.driveway", "idle", {"friendly_name": "Driveway"})
    async_expose_entity(hass, "conversation", "camera.driveway", True)
    router = _router(hass, user_map={7: user.id})
    image = SimpleNamespace(content=JPEG, content_type="image/jpeg")
    with patch(_GET_IMAGE, new=AsyncMock(return_value=image)):
        result = await router.process_snapshot(query="driveway", discord_user_id=7)
    assert result.image_content == JPEG


async def test_real_hass_unexposed_and_unreadable_are_refused(
    hass, setup_core_components
):
    owner = await hass.auth.async_create_user("Owner")
    hass.states.async_set("camera.driveway", "idle", {"friendly_name": "Driveway"})
    router = _router(hass, user_map={7: owner.id})
    with patch(_GET_IMAGE, new=AsyncMock()) as get_image:
        # Not exposed to conversation at all.
        result = await router.process_snapshot(query="driveway", discord_user_id=7)
    assert result.image_content is None
    get_image.assert_not_called()

    # Exposed, but a user with no groups has no POLICY_READ on it.
    async_expose_entity(hass, "conversation", "camera.driveway", True)
    other = await hass.auth.async_create_user("No Access")
    router = _router(hass, user_map={8: other.id})
    with patch(_GET_IMAGE, new=AsyncMock()) as get_image:
        result = await router.process_snapshot(query="driveway", discord_user_id=8)
    assert result.image_content is None
    get_image.assert_not_called()


# --- Bot level: attachments only ever come from the snapshot path ---


def _message(content, *, channel_id=111):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.send = AsyncMock()
    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock()
    typing_cm.__aexit__ = AsyncMock(return_value=False)
    channel.typing = MagicMock(return_value=typing_cm)
    msg = SimpleNamespace(
        content=content,
        channel=channel,
        author=SimpleNamespace(id=7, bot=False, display_name="Catduck"),
        mentions=[],
    )
    msg.ref = SimpleNamespace(message_id=1)
    msg.to_reference = MagicMock(return_value=msg.ref)
    return msg


def _client(router, monkeypatch):
    with patch("discord.Client.__init__", return_value=None):
        client = DiscordAssistClient(
            hass=SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock())),
            router=router,
            status_signal="test_signal",
        )
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=999))
    )
    return client


def _snapshot_router(result):
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value="agent reply")
    router.process_snapshot = AsyncMock(return_value=result)
    return router


async def test_bot_snapshot_command_attaches_fetched_file(monkeypatch):
    router = _snapshot_router(
        SnapshotResult(text="Snapshot from Driveway.", image_content=JPEG)
    )
    client = _client(router, monkeypatch)
    msg = _message("snapshot driveway")
    await client.on_message(msg)
    router.process_snapshot.assert_awaited_once_with(
        query="driveway", discord_user_id=7, exact_only=False
    )
    router.process.assert_not_called()
    msg.channel.send.assert_awaited_once()
    kwargs = msg.channel.send.await_args.kwargs
    assert kwargs["file"].filename == "snapshot.jpg"
    assert kwargs["file"].fp.getvalue() == JPEG


async def test_bot_show_me_attaches_fetched_file(monkeypatch):
    router = _snapshot_router(
        SnapshotResult(text="Snapshot from Driveway.", image_content=JPEG, matched=True)
    )
    client = _client(router, monkeypatch)
    msg = _message("show me the driveway")
    await client.on_message(msg)
    # "show me" only takes over on an exact camera name.
    router.process_snapshot.assert_awaited_once_with(
        query="driveway", discord_user_id=7, exact_only=True
    )
    router.process.assert_not_called()
    assert msg.channel.send.await_args.kwargs["file"].fp.getvalue() == JPEG


async def test_bot_show_me_without_camera_match_falls_back_to_agent(monkeypatch):
    """'show me how to bake bread' is not a camera request: the agent answers."""
    router = _snapshot_router(
        SnapshotResult(text="I could not find a camera.", matched=False)
    )
    router.process = AsyncMock(return_value="Use a hot oven.")
    client = _client(router, monkeypatch)
    msg = _message("show me how to bake bread")
    await client.on_message(msg)
    router.process.assert_awaited_once()
    kwargs = msg.channel.send.await_args.kwargs
    assert "file" not in kwargs and "files" not in kwargs
    assert msg.channel.send.await_args.args[0] == "Use a hot oven."


async def test_bot_snapshot_refusal_sends_text_without_file(monkeypatch):
    router = _snapshot_router(SnapshotResult(text="I could not find a camera."))
    client = _client(router, monkeypatch)
    msg = _message("snapshot nowhere")
    await client.on_message(msg)
    kwargs = msg.channel.send.await_args.kwargs
    assert "file" not in kwargs and "files" not in kwargs


async def test_bot_ignores_unauthorized_snapshot_user(monkeypatch):
    router = _snapshot_router(
        SnapshotResult(text="Snapshot from Driveway.", image_content=JPEG)
    )
    router.should_respond.return_value = False
    client = _client(router, monkeypatch)
    msg = _message("snapshot driveway")
    await client.on_message(msg)
    router.process_snapshot.assert_not_called()
    router.process.assert_not_called()
    msg.channel.send.assert_not_called()


async def test_crafted_agent_reply_attaches_nothing(monkeypatch):
    """Model text naming files, URLs, or markers must never attach anything."""
    crafted = (
        "Here is your file: /etc/passwd\n"
        "file:///etc/passwd [attachment: /etc/passwd] "
        "![snapshot](https://evil.example/snapshot.jpg) "
        "{{media:/etc/passwd}} [[attach:snapshot.jpg]] "
        "discord.File(open('/etc/passwd'))"
    )
    router = MagicMock()
    router.should_respond.return_value = True
    router.process = AsyncMock(return_value=crafted)
    router.process_snapshot = AsyncMock()
    client = _client(router, monkeypatch)
    msg = _message("tell me a story about a driveway")
    await client.on_message(msg)
    router.process_snapshot.assert_not_called()
    assert msg.channel.send.await_count >= 1
    for call in msg.channel.send.await_args_list:
        assert "file" not in call.kwargs
        assert "files" not in call.kwargs
