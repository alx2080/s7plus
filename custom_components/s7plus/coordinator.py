"""Polling coordinators for S7 Plus."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .plc import PlcConnection, PlcConnectionError, TagSpec

_LOGGER = logging.getLogger(__name__)

FAILED_KEY = "_failed"  # never a subentry id
CPU_KEY = "_cpu"
CATALOG_MAX_AGE = 600  # seconds a browsed tag catalog is reused by the UI

type S7PlusConfigEntry = ConfigEntry[S7PlusRuntimeData]


class S7PlusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll one group of tags (normal or fast) of one controller."""

    config_entry: S7PlusConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: S7PlusConfigEntry,
        plc: PlcConnection,
        specs: dict[str, TagSpec],
        interval: timedelta,
        *,
        fast: bool = False,
        read_cpu_state: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{entry.title} (fast)" if fast else entry.title,
            update_interval=interval,
            always_update=False,
        )
        self.plc = plc
        self.specs = specs
        self.fast = fast
        self._read_cpu_state = read_cpu_state
        self.failed: frozenset[str] = frozenset()
        self.last_success: float | None = None
        self.last_success_utc = None

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self.specs)

    async def _async_update_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        try:
            result = await self.plc.read(list(self.specs.values()))
            if self._read_cpu_state:
                data[CPU_KEY] = await self.plc.cpu_state()
        except PlcConnectionError as err:
            raise UpdateFailed(str(err)) from err

        if result.failed and not result.values:
            first = next(iter(result.failed.values()))
            raise UpdateFailed(f"No tag could be read: {first}")
        for key in sorted(result.failed.keys() - self.failed):
            subentry = self.config_entry.subentries.get(key)
            _LOGGER.warning(
                "%s: tag %s could not be read: %s. If the PLC program changed, "
                "use 'Refresh tag addresses' in the integration options",
                self.config_entry.title,
                subentry.title if subentry else key,
                result.failed[key],
            )
        self.failed = frozenset(result.failed)
        # Any answer proves the link, even if some tags failed.
        self.last_success = time.monotonic()
        self.last_success_utc = dt_util.utcnow()
        data.update(result.values)
        data[FAILED_KEY] = self.failed
        return data


@dataclass
class S7PlusRuntimeData:
    """Everything one controller entry runs on."""

    plc: PlcConnection
    normal: S7PlusCoordinator
    fast: S7PlusCoordinator | None
    invalid: dict[str, str] = field(default_factory=dict)
    # Survives reloads (adding a tag reloads the entry): lives in hass.data.
    cache: dict[str, Any] = field(default_factory=dict)

    @property
    def coordinators(self) -> list[S7PlusCoordinator]:
        return [c for c in (self.fast, self.normal) if c is not None]

    def coordinator_for(self, key: str) -> S7PlusCoordinator:
        if self.fast is not None and key in self.fast.keys:
            return self.fast
        return self.normal

    @property
    def last_success(self) -> float | None:
        times = [c.last_success for c in self.coordinators if c.last_success]
        return max(times) if times else None

    @property
    def last_success_utc(self):
        times = [c.last_success_utc for c in self.coordinators if c.last_success_utc]
        return max(times) if times else None

    async def async_catalog(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """The controller's tag catalog, cached for a few minutes."""
        catalog = self.cache.get("catalog")
        if (
            refresh
            or catalog is None
            or time.monotonic() - self.cache.get("time", 0.0) > CATALOG_MAX_AGE
        ):
            catalog = await self.plc.browse()
            self.cache["catalog"] = catalog
            self.cache["time"] = time.monotonic()
        return catalog


def device_info_for(entry: ConfigEntry, identity: str | None) -> DeviceInfo:
    """Describe the controller, using its order number when it tells us.

    ``identity`` looks like '1;6ES7 512-1CK01-0AB0;V2.9'.
    """
    model: str | None = None
    sw_version: str | None = None
    parts = [part.strip() for part in identity.split(";")] if identity else []
    if len(parts) >= 2 and parts[1]:
        try:
            from s7commplus import device_name  # noqa: PLC0415

            model = device_name(identity) or parts[1]
        except Exception:  # noqa: BLE001 - purely informative
            model = parts[1]
    if len(parts) >= 3 and parts[2]:
        sw_version = parts[2]
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Siemens",
        model=model or "S7-1200/1500",
        sw_version=sw_version,
    )
