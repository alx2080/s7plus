"""Switches: BOOL tags, or numeric tags with on/off values."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_INVERT, CONF_OFF_VALUE, CONF_ON_VALUE
from .coordinator import S7PlusConfigEntry, S7PlusCoordinator
from .entity import S7PlusEntity, async_setup_platform_entities
from .plc import BOOL_TYPES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: S7PlusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    async_setup_platform_entities(hass, entry, async_add_entities, "switch", S7PlusSwitch)


class S7PlusSwitch(S7PlusEntity, SwitchEntity):
    """A writable on/off tag."""

    def __init__(self, coordinator: S7PlusCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator, subentry)
        spec = coordinator.specs.get(self.key)
        self._boolean = spec is None or spec.softtype in BOOL_TYPES
        self._invert = bool(self.point.get(CONF_INVERT))
        self._on = self.point.get(CONF_ON_VALUE, 1)
        self._off = self.point.get(CONF_OFF_VALUE, 0)

    @property
    def is_on(self) -> bool | None:
        value = self.raw_value
        if value is None:
            return None
        state = bool(value) if self._boolean else value == self._on
        return state != self._invert

    def _value_for(self, on: bool) -> Any:
        on = on != self._invert
        if self._boolean:
            return on
        return self._on if on else self._off

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_write(self._value_for(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_write(self._value_for(False))
