"""Base entity for S7 Plus."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PLATFORM, DOMAIN, SUBENTRY_TAG
from .coordinator import S7PlusConfigEntry, S7PlusCoordinator, device_info_for
from .plc import PlcError


class S7PlusEntity(CoordinatorEntity[S7PlusCoordinator]):
    """An entity backed by one UI-defined PLC tag."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: S7PlusCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.key = subentry.subentry_id
        self.point: dict[str, Any] = dict(subentry.data)
        self._attr_name = subentry.title
        self._attr_unique_id = f"{entry.entry_id}_{self.key}"
        self._attr_device_info = device_info_for(entry, coordinator.plc.identity)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.key in self.coordinator.keys
            and self.key not in self.coordinator.failed
        )

    @property
    def raw_value(self) -> Any:
        return (self.coordinator.data or {}).get(self.key)

    async def async_write(self, value: Any) -> None:
        """Write the tag, then refresh its poll."""
        spec = self.coordinator.specs[self.key]
        name = self.name or self.key
        try:
            await self.coordinator.plc.write(spec, value)
        except ValueError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_invalid",
                translation_placeholders={"name": name, "error": str(err)},
            ) from err
        except PlcError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"name": name, "error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()


def async_setup_platform_entities(
    hass: HomeAssistant,
    entry: S7PlusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: str,
    factory: Callable[[S7PlusCoordinator, ConfigSubentry], S7PlusEntity],
) -> None:
    """One entity per tag subentry of this platform."""
    runtime = entry.runtime_data
    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_TAG or subentry.data.get(CONF_PLATFORM) != platform:
            continue
        # Entities belong to the main entry: a device can only belong to one
        # subentry, so subentry-bound entities would pull it back and forth.
        async_add_entities([factory(runtime.coordinator_for(subentry_id), subentry)])
