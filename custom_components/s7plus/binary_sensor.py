"""Binary sensors: BOOL tags, plus the controller connection status."""

from __future__ import annotations

from datetime import timedelta
import time
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_INVERT, CONF_LINK_TIMEOUT, DEFAULT_LINK_TIMEOUT
from .coordinator import S7PlusConfigEntry, S7PlusCoordinator, S7PlusRuntimeData, device_info_for
from .entity import S7PlusEntity, async_setup_platform_entities

CONF_DEVICE_CLASS = "device_class"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: S7PlusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    async_add_entities([S7PlusLinkSensor(entry)])
    async_setup_platform_entities(
        hass, entry, async_add_entities, "binary_sensor", S7PlusBinarySensor
    )


class S7PlusBinarySensor(S7PlusEntity, BinarySensorEntity):
    """A BOOL tag."""

    def __init__(self, coordinator: S7PlusCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator, subentry)
        if device_class := self.point.get(CONF_DEVICE_CLASS):
            self._attr_device_class = BinarySensorDeviceClass(device_class)
        self._invert = bool(self.point.get(CONF_INVERT))

    @property
    def is_on(self) -> bool | None:
        value = self.raw_value
        return None if value is None else bool(value) != self._invert


class S7PlusLinkSensor(BinarySensorEntity):
    """Connection status: off when nothing was read for longer than the timeout."""

    _attr_has_entity_name = True
    _attr_translation_key = "link"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: S7PlusConfigEntry) -> None:
        self._runtime: S7PlusRuntimeData = entry.runtime_data
        self._timeout = float(entry.options.get(CONF_LINK_TIMEOUT, DEFAULT_LINK_TIMEOUT))
        self._attr_unique_id = f"{entry.entry_id}_link"
        self._attr_device_info = device_info_for(entry, self._runtime.plc.identity)

    @property
    def available(self) -> bool:
        return True  # usable in automations exactly when the PLC is unreachable

    def _compute(self) -> bool:
        last = self._runtime.last_success
        return last is not None and time.monotonic() - last <= self._timeout

    @property
    def is_on(self) -> bool:
        return self._compute()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self._runtime.last_success_utc
        return {"last_seen": last.isoformat() if last else None, "timeout": self._timeout}

    async def async_added_to_hass(self) -> None:
        last_state: list[bool] = [self._compute()]

        @callback
        def _recheck(*_: Any) -> None:
            last_state[0] = self._compute()
            self.async_write_ha_state()

        for coordinator in self._runtime.coordinators:
            self.async_on_remove(coordinator.async_add_listener(_recheck))

        @callback
        def _tick(_now: Any) -> None:
            state = self._compute()
            if state != last_state[0]:
                last_state[0] = state
                self.async_write_ha_state()

        interval = min(1.0, max(self._timeout / 4, 0.1))
        self.async_on_remove(
            async_track_time_interval(self.hass, _tick, timedelta(seconds=interval))
        )
