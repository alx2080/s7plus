"""Selects: integer tags whose values are named modes."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_OPTIONS
from .coordinator import S7PlusConfigEntry, S7PlusCoordinator
from .entity import S7PlusEntity, async_setup_platform_entities
from .helpers import parse_options


async def async_setup_entry(
    hass: HomeAssistant,
    entry: S7PlusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up selects."""
    async_setup_platform_entities(hass, entry, async_add_entities, "select", S7PlusSelect)


class S7PlusSelect(S7PlusEntity, SelectEntity):
    """A tag whose values map to named modes."""

    def __init__(self, coordinator: S7PlusCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator, subentry)
        self._by_value = parse_options(self.point.get(CONF_OPTIONS, ""))
        self._by_label = {label: value for value, label in self._by_value.items()}
        self._attr_options = list(self._by_value.values())

    @property
    def current_option(self) -> str | None:
        value = self.raw_value
        return None if value is None else self._by_value.get(int(value))

    async def async_select_option(self, option: str) -> None:
        await self.async_write(self._by_label[option])
