"""Gateway connectivity sensor for Discord Assist."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DiscordConfigEntry
from .const import DOMAIN, signal_connection


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DiscordConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([DiscordGatewaySensor(entry)])


class DiscordGatewaySensor(BinarySensorEntity):
    """On while the bot holds a live Discord gateway connection."""

    _attr_has_entity_name = True
    _attr_translation_key = "gateway"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: DiscordConfigEntry) -> None:
        self._client = entry.runtime_data.client
        self._signal = signal_connection(entry.entry_id)
        self._attr_unique_id = f"{entry.unique_id}_gateway"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name=entry.title,
            manufacturer="Discord",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        return self._client.connected

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._signal, self.async_write_ha_state)
        )
