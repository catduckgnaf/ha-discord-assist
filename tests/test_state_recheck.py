"""Re-read state before acting on a button press (issue #30)."""

from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.discord_assist.actions import action_specs
from custom_components.discord_assist.const import EVENT_ACTION
from custom_components.discord_assist.state_check import (
    capture_expectations,
    find_state_changes,
)
from tests.test_actionable_buttons import _posted, _press


async def test_unchanged_state_proceeds(hass: HomeAssistant):
    hass.states.async_set("cover.garage", "open")
    events = []
    hass.bus.async_listen(EVENT_ACTION, events.append)
    client, store, message, ids = await _posted(
        hass, [{"id": "close", "label": "Close garage", "entity_ids": ["cover.garage"]}]
    )
    await client.on_interaction(_press(message, ids[0]))
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_changed_state_refuses_and_explains(hass: HomeAssistant):
    hass.states.async_set("cover.garage", "open")
    events = []
    hass.bus.async_listen(EVENT_ACTION, events.append)
    client, store, message, ids = await _posted(
        hass, [{"id": "close", "label": "Close garage", "entity_ids": ["cover.garage"]}]
    )
    hass.states.async_set("cover.garage", "closed")
    press = _press(message, ids[0])
    await client.on_interaction(press)
    await hass.async_block_till_done()
    assert events == []
    replies = [
        call.args[0]
        for call in press.followup.send.await_args_list
        + press.response.send_message.await_args_list
    ]
    assert len(replies) == 1
    assert replies[0].startswith("Nothing changed: cover.garage is now closed")
    edit = message.edit.await_args.kwargs
    assert edit["view"] is None
    assert edit["content"].endswith(
        "Not done: the state changed since this was posted."
    )


async def test_toggled_and_back_counts_as_changed(hass: HomeAssistant):
    hass.states.async_set("cover.garage", "open")
    expectations = capture_expectations(hass, ["cover.garage"])
    hass.states.async_set("cover.garage", "closed")
    hass.states.async_set("cover.garage", "open")
    assert find_state_changes(hass, expectations)
    # Before the last restart only the state itself can be compared.
    assert find_state_changes(hass, expectations, compare_last_changed=False) == []


async def test_vanished_entity_counts_as_changed(hass: HomeAssistant):
    hass.states.async_set("switch.heater", "on")
    expectations = capture_expectations(hass, ["switch.heater"])
    hass.states.async_remove("switch.heater")
    assert find_state_changes(hass, expectations) == ["switch.heater no longer exists"]


async def test_buttons_from_before_a_restart_compare_state_only(hass: HomeAssistant):
    """A restart resets last_changed; that alone must not refuse every button."""
    hass.states.async_set("cover.garage", "open")
    events = []
    hass.bus.async_listen(EVENT_ACTION, events.append)
    with patch(
        "custom_components.discord_assist.actions.time.time", return_value=10**10
    ):
        # The handler's run started "after" the button was recorded.
        client, store, message, ids = await _posted(
            hass,
            [{"id": "close", "label": "Close garage", "entity_ids": ["cover.garage"]}],
        )
    # Same state, new last_changed (as after a restart).
    hass.states.async_set("cover.garage", "open", force_update=True)
    await client.on_interaction(_press(message, ids[0]))
    await hass.async_block_till_done()
    assert len(events) == 1


def test_specs_without_hass_capture_nothing():
    (spec,) = action_specs([{"id": "x", "entity_ids": ["light.a"]}])
    assert "expectations" not in spec.payload
