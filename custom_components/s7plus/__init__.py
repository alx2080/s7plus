"""S7 Plus: Siemens S7-1200/1500 controllers over S7CommPlus, tags set up in the UI."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_FAST_POLL,
    CONF_FAST_SCAN_INTERVAL,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TAGS_PER_REQUEST,
    CONF_TIMEOUT,
    CONF_TLS_CA,
    CONF_TLS_CERT,
    CONF_TLS_KEY,
    CONF_USE_TLS,
    DEFAULT_FAST_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TAGS_PER_REQUEST,
    DEFAULT_TIMEOUT,
    DOMAIN,
    SUBENTRY_TAG,
)
from .coordinator import (
    S7PlusConfigEntry,
    S7PlusCoordinator,
    S7PlusRuntimeData,
    device_info_for,
)
from .plc import ConnectParams, PlcConnection, build_specs
from .registry import async_prepare_registries

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


def connect_params(data) -> ConnectParams:
    """Connection parameters from entry data (also used by the config flow)."""
    return ConnectParams(
        host=data[CONF_HOST],
        port=int(data.get(CONF_PORT, DEFAULT_PORT)),
        connection_type=data.get(CONF_CONNECTION_TYPE, "hmi"),
        use_tls=bool(data.get(CONF_USE_TLS)),
        tls_ca=data.get(CONF_TLS_CA) or None,
        tls_cert=data.get(CONF_TLS_CERT) or None,
        tls_key=data.get(CONF_TLS_KEY) or None,
        password=data.get(CONF_PASSWORD) or None,
    )


async def async_setup_entry(hass: HomeAssistant, entry: S7PlusConfigEntry) -> bool:
    """Set up one controller."""
    options = entry.options
    plc = PlcConnection(
        connect_params(entry.data),
        timeout=float(options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
        tags_per_request=int(options.get(CONF_TAGS_PER_REQUEST, DEFAULT_TAGS_PER_REQUEST)),
    )
    entry.async_on_unload(plc.close)

    tags = {
        subentry_id: dict(subentry.data)
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_TAG
    }
    specs, invalid = build_specs(tags)
    for key, reason in invalid.items():
        _LOGGER.error(
            "%s: tag %r is misconfigured and skipped: %s",
            entry.title,
            entry.subentries[key].title,
            reason,
        )
    fast_specs = {k: s for k, s in specs.items() if tags[k].get(CONF_FAST_POLL)}
    normal_specs = {k: s for k, s in specs.items() if k not in fast_specs}

    # The normal poll also reads the CPU state, so it runs even without tags.
    normal = S7PlusCoordinator(
        hass,
        entry,
        plc,
        normal_specs,
        timedelta(seconds=float(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))),
        read_cpu_state=True,
    )
    fast = None
    if fast_specs:
        fast = S7PlusCoordinator(
            hass,
            entry,
            plc,
            fast_specs,
            timedelta(
                seconds=float(
                    options.get(CONF_FAST_SCAN_INTERVAL, DEFAULT_FAST_SCAN_INTERVAL)
                )
            ),
            fast=True,
        )

    await normal.async_config_entry_first_refresh()
    if fast is not None:
        await fast.async_config_entry_first_refresh()

    # Entities of deleted tags are removed here (see registry.py).
    async_prepare_registries(
        hass,
        entry,
        device_info_for(entry, plc.identity),
        [f"{entry.entry_id}_{key}" for key in entry.subentries]
        + [f"{entry.entry_id}_link", f"{entry.entry_id}_cpu_state"],
    )
    entry.runtime_data = S7PlusRuntimeData(
        plc=plc,
        normal=normal,
        fast=fast,
        invalid=invalid,
        cache=hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {}),
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: S7PlusConfigEntry) -> None:
    """Rebuild after tags or options changed."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: S7PlusConfigEntry) -> bool:
    """Unload; the session is closed by the on-unload callback."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: S7PlusConfigEntry) -> None:
    """Forget the cached tag catalog of a deleted controller."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
