"""Numbers: writable numeric tags (setpoints, limits, ...)."""

from __future__ import annotations

import logging

from homeassistant.components.number import (
    DEVICE_CLASS_UNITS,
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MAX, CONF_MIN, CONF_STEP
from .coordinator import S7PlusConfigEntry, S7PlusCoordinator
from .entity import S7PlusEntity, async_setup_platform_entities

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE_CLASS = "device_class"
CONF_MODE = "mode"
CONF_UNIT_OF_MEASUREMENT = "unit_of_measurement"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: S7PlusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up numbers."""
    async_setup_platform_entities(hass, entry, async_add_entities, "number", S7PlusNumber)


class S7PlusNumber(S7PlusEntity, NumberEntity):
    """A writable numeric tag."""

    def __init__(self, coordinator: S7PlusCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator, subentry)
        point = self.point
        self._attr_native_min_value = float(point.get(CONF_MIN, 0))
        self._attr_native_max_value = float(point.get(CONF_MAX, 100))
        self._attr_native_step = float(point.get(CONF_STEP, 1))
        self._attr_mode = NumberMode(point.get(CONF_MODE, NumberMode.BOX))
        self._attr_native_unit_of_measurement = point.get(CONF_UNIT_OF_MEASUREMENT) or None
        if device_class := point.get(CONF_DEVICE_CLASS):
            device_class = NumberDeviceClass(device_class)
            allowed = DEVICE_CLASS_UNITS.get(device_class)
            if allowed is not None and self._attr_native_unit_of_measurement not in allowed:
                _LOGGER.warning(
                    "%s: unit %s does not fit device class %s; the class is ignored. "
                    "Edit the tag to pick a matching class or unit",
                    subentry.title,
                    self._attr_native_unit_of_measurement,
                    device_class.value,
                )
            else:
                self._attr_device_class = device_class

    @property
    def native_value(self) -> float | None:
        return self.raw_value

    async def async_set_native_value(self, value: float) -> None:
        await self.async_write(value)
