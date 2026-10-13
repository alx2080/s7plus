"""Keep the device and entities in the main config entry.

Home Assistant now lets a device belong to a single config subentry. Entities of
several register subentries sharing one device would make the device jump to
whichever subentry registered last, and the other entities fall off it. So the
entities belong to the main entry (subentries only hold their settings), and the
entities of deleted subentries are removed here instead of by Home Assistant.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo

_LOGGER = logging.getLogger(__name__)


@callback
def async_prepare_registries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_info: DeviceInfo,
    keep_unique_ids: Iterable[str],
) -> None:
    """Register the device in the main entry and drop entities of deleted tags."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers=device_info["identifiers"])
    if device is not None:
        _move_to_main_entry(dev_reg, entry, device)
    dev_reg.async_get_or_create(config_entry_id=entry.entry_id, **device_info)

    keep = set(keep_unique_ids)
    ent_reg = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if entity.unique_id not in keep:
            _LOGGER.debug("Removing %s: its register no longer exists", entity.entity_id)
            ent_reg.async_remove(entity.entity_id)


def _move_to_main_entry(dev_reg: dr.DeviceRegistry, entry: ConfigEntry, device) -> None:
    """Undo the subentry links earlier versions of this integration created."""
    if hasattr(device, "config_subentry_id"):
        # Single-owner device model (Home Assistant 2026.10+).
        if device.config_subentry_id is not None:
            dev_reg.async_update_device(device.id, new_config_subentry_id=None)
        return
    # Older model: a device may sit in several subentries of the entry.
    subentries = device.config_entries_subentries.get(entry.entry_id, set())
    if None not in subentries:
        dev_reg.async_update_device(
            device.id, add_config_entry_id=entry.entry_id, add_config_subentry_id=None
        )
    for subentry_id in subentries - {None}:
        try:
            dev_reg.async_update_device(
                device.id,
                remove_config_entry_id=entry.entry_id,
                remove_config_subentry_id=subentry_id,
            )
        except Exception:  # noqa: BLE001 - purely cosmetic clean-up
            _LOGGER.debug("Could not unlink device from subentry %s", subentry_id)
