"""Sensors: PLC tags read as values, plus the CPU operating state."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    DEVICE_CLASS_STATE_CLASSES,
    DEVICE_CLASS_UNITS,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PRECISION
from .coordinator import CPU_KEY, S7PlusConfigEntry, S7PlusCoordinator, device_info_for
from .entity import S7PlusEntity, async_setup_platform_entities
from .plc import TEXT_TYPES

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE_CLASS = "device_class"
CONF_STATE_CLASS = "state_class"
CONF_UNIT_OF_MEASUREMENT = "unit_of_measurement"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: S7PlusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    async_add_entities([S7PlusCpuStateSensor(entry.runtime_data.normal)])
    async_setup_platform_entities(hass, entry, async_add_entities, "sensor", S7PlusSensor)


class S7PlusSensor(S7PlusEntity, SensorEntity):
    """A tag shown as a sensor."""

    def __init__(self, coordinator: S7PlusCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator, subentry)
        spec = coordinator.specs.get(self.key)
        if spec is not None and spec.softtype in TEXT_TYPES:
            return
        point = self.point
        if spec is not None and spec.softtype in ("TIME", "LTIME") and not point.get(CONF_DEVICE_CLASS):
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_native_unit_of_measurement = "ms"
        if device_class := point.get(CONF_DEVICE_CLASS):
            self._attr_device_class = SensorDeviceClass(device_class)
        if state_class := point.get(CONF_STATE_CLASS):
            self._attr_state_class = SensorStateClass(state_class)
        if unit := point.get(CONF_UNIT_OF_MEASUREMENT):
            self._attr_native_unit_of_measurement = unit
        self._drop_contradicting_classes(subentry.title)
        if (precision := point.get(CONF_PRECISION)) is not None:
            self._attr_suggested_display_precision = int(precision)

    def _drop_contradicting_classes(self, title: str) -> None:
        """Ignore a class saved before the form checked it (e.g. "power" in kWh)."""
        device_class = getattr(self, "_attr_device_class", None)
        if device_class is None:
            return
        unit = getattr(self, "_attr_native_unit_of_measurement", None)
        allowed = DEVICE_CLASS_UNITS.get(device_class)
        if allowed is not None and unit not in allowed:
            _LOGGER.warning(
                "%s: unit %s does not fit device class %s; the class is ignored. "
                "Edit the tag to pick a matching class or unit",
                title, unit, device_class.value,
            )
            self._attr_device_class = None
            return
        state_class = getattr(self, "_attr_state_class", None)
        classes = DEVICE_CLASS_STATE_CLASSES.get(device_class)
        if state_class is not None and classes is not None and state_class not in classes:
            _LOGGER.warning(
                "%s: state class %s does not fit device class %s; the state class "
                "is ignored. Edit the tag to fix it",
                title, state_class.value, device_class.value,
            )
            self._attr_state_class = None

    @property
    def native_value(self) -> Any:
        value = self.raw_value
        if isinstance(value, bool) and self._attr_device_class is None:
            return "on" if value else "off"
        return value


class S7PlusCpuStateSensor(CoordinatorEntity[S7PlusCoordinator], SensorEntity):
    """RUN / STOP of the controller."""

    _attr_has_entity_name = True
    _attr_translation_key = "cpu_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["run", "stop"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: S7PlusCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_cpu_state"
        self._attr_device_info = device_info_for(entry, coordinator.plc.identity)

    @property
    def native_value(self) -> str | None:
        state = (self.coordinator.data or {}).get(CPU_KEY)
        return state.lower() if state else None
