"""Diagnostics: controller identity, tag map and current values."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PASSWORD
from .coordinator import CPU_KEY, FAILED_KEY, S7PlusConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: S7PlusConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    values: dict[str, Any] = {}
    for coordinator in runtime.coordinators:
        for key, value in (coordinator.data or {}).items():
            if key in (FAILED_KEY, CPU_KEY):
                continue
            title = entry.subentries[key].title if key in entry.subentries else key
            values[title] = value
    return {
        "connection": async_redact_data(dict(entry.data), {CONF_PASSWORD}),
        "options": dict(entry.options),
        "identity": runtime.plc.identity,
        "cpu_state": (runtime.normal.data or {}).get(CPU_KEY),
        "tags": {s.title: dict(s.data) for s in entry.subentries.values()},
        "invalid": runtime.invalid,
        "failed": sorted(k for c in runtime.coordinators for k in c.failed),
        "values": values,
        "catalog_size": len(runtime.cache.get("catalog") or []),
    }
