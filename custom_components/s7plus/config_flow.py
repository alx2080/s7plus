"""Config flow for S7 Plus.

* The config entry is one controller: address, connection role, TLS, password.
* Every tag is a config subentry ("+ Add tag" on the controller card): picked
  from the controller's own tag catalog, or typed in as a DB address.
* The options flow holds polling settings, refreshes tag addresses after a PLC
  program change, and copies tags from another controller.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.number import (
    DEVICE_CLASS_UNITS as NUMBER_CLASS_UNITS,
    NumberDeviceClass,
    NumberMode,
)
from homeassistant.components.sensor import (
    DEVICE_CLASS_STATE_CLASSES,
    DEVICE_CLASS_UNITS as SENSOR_CLASS_UNITS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

try:  # Home Assistant 2026.x moved from voluptuous to probatio
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol

from . import connect_params
from .const import (
    CONF_BIT,
    CONF_BLOCK,
    CONF_BYTE,
    CONF_CONNECTION_TYPE,
    CONF_DB,
    CONF_FAST_POLL,
    CONF_FAST_SCAN_INTERVAL,
    CONF_FILTER,
    CONF_HOST,
    CONF_INVERT,
    CONF_LENGTH,
    CONF_LINK_TIMEOUT,
    CONF_MAX,
    CONF_MIN,
    CONF_OFF_VALUE,
    CONF_ON_VALUE,
    CONF_OPTIONS,
    CONF_PASSWORD,
    CONF_PLATFORM,
    CONF_PORT,
    CONF_PRECISION,
    CONF_SCALE,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    CONF_SOURCE_ENTRY,
    CONF_STEP,
    CONF_TAG,
    CONF_TAG_NAME,
    CONF_TAGS_PER_REQUEST,
    CONF_TEST_READ,
    CONF_TIMEOUT,
    CONF_TLS_CA,
    CONF_TLS_CERT,
    CONF_TLS_KEY,
    CONF_TYPE,
    CONF_USE_TLS,
    CONF_VALUE_OFFSET,
    CONNECTION_TYPES,
    DEFAULT_FAST_SCAN_INTERVAL,
    DEFAULT_LINK_TIMEOUT,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TAGS_PER_REQUEST,
    DEFAULT_TIMEOUT,
    DOMAIN,
    PLATFORMS_ORDER,
    SOURCE_ABSOLUTE,
    SOURCE_SYMBOLIC,
    SUBENTRY_TAG,
)
from .helpers import parse_options
from .plc import (
    ABSOLUTE_TYPES,
    BOOL_TYPES,
    FLOAT_TYPES,
    NUMERIC_TYPES,
    TEXT_TYPES,
    PlcConnection,
    PlcError,
    TagSpec,
    platforms_for_type,
)

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE_CLASS = "device_class"
CONF_STATE_CLASS = "state_class"
CONF_UNIT_OF_MEASUREMENT = "unit_of_measurement"
CONF_MODE = "mode"
SECTION_TLS = "tls"

MAX_TAGS_IN_LIST = 500
COMMON_UNITS = [
    "°C", "°F", "%", "V", "mV", "A", "mA", "W", "kW", "VA", "var", "Wh", "kWh",
    "Hz", "bar", "Pa", "kPa", "m³", "m³/h", "L", "L/min", "rpm", "ms", "s", "min",
    "h", "ppm", "lx", "mm", "m",
]  # fmt: skip
INTEGER_TYPES = [t for t in NUMERIC_TYPES if t not in FLOAT_TYPES and t not in ("TIME", "LTIME")]


# --------------------------------------------------------------------------
# Selector helpers
# --------------------------------------------------------------------------


def _select(
    options: list[str] | list[SelectOptionDict], key: str | None = None, custom: bool = False
) -> SelectSelector:
    config = SelectSelectorConfig(
        options=options, mode=SelectSelectorMode.DROPDOWN, custom_value=custom
    )
    if key is not None:
        config["translation_key"] = key
    return SelectSelector(config)


def _number(
    min_: float | None = None,
    max_: float | None = None,
    step: float | str = 1,
    unit: str | None = None,
) -> NumberSelector:
    config = NumberSelectorConfig(step=step, mode=NumberSelectorMode.BOX)
    if min_ is not None:
        config["min"] = min_
    if max_ is not None:
        config["max"] = max_
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _int(value: Any) -> int:
    return int(float(value))


def _suggested(d: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {} if d.get(key) is None else {"suggested_value": d[key]}


# --------------------------------------------------------------------------
# Controller connection
# --------------------------------------------------------------------------


def connection_schema(d: Mapping[str, Any], *, reconfigure: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if not reconfigure:
        fields[vol.Required(CONF_NAME, default=d.get(CONF_NAME, "PLC"))] = TextSelector()
    fields[vol.Required(CONF_HOST, default=d.get(CONF_HOST, ""))] = TextSelector()
    fields[vol.Required(CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT))] = vol.All(
        _number(1, 65535), vol.Coerce(_int)
    )
    fields[vol.Required(CONF_CONNECTION_TYPE, default=d.get(CONF_CONNECTION_TYPE, "hmi"))] = (
        _select(list(CONNECTION_TYPES), "connection_type")
    )
    fields[vol.Optional(CONF_PASSWORD, description=_suggested(d, CONF_PASSWORD))] = (
        TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    )
    tls = d.get(SECTION_TLS, d)
    # S7-1200 V4.5+ and S7-1500 V2.9+ only talk TLS, so it is on for new controllers.
    use_tls = bool(tls.get(CONF_USE_TLS, True))
    fields[vol.Required(SECTION_TLS)] = section(
        vol.Schema(
            {
                vol.Required(CONF_USE_TLS, default=use_tls): BooleanSelector(),
                vol.Optional(CONF_TLS_CA, description=_suggested(tls, CONF_TLS_CA)): TextSelector(),
                vol.Optional(CONF_TLS_CERT, description=_suggested(tls, CONF_TLS_CERT)): TextSelector(),
                vol.Optional(CONF_TLS_KEY, description=_suggested(tls, CONF_TLS_KEY)): TextSelector(),
            }
        ),
        {"collapsed": True},
    )
    return vol.Schema(fields)


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    """Entry data from the form: the TLS section folded into the top level."""
    data = {k: v for k, v in user_input.items() if k not in (SECTION_TLS, CONF_NAME)}
    data.update(user_input.get(SECTION_TLS, {}))
    if not data.get(CONF_PASSWORD):
        data.pop(CONF_PASSWORD, None)
    return data


async def _async_probe_once(data: Mapping[str, Any]) -> str | None:
    """Connect once and return the controller identity.

    Raises ``PlcError``.
    """
    plc = PlcConnection(connect_params(data), timeout=10)
    try:
        await plc.cpu_state()  # connects first; any answer proves the session
        return plc.identity
    finally:
        await plc.close()


def _alternatives(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The settings to try: the user's first, then TLS and role variations.

    A controller set to 'only allow secure PG/PC and HMI communication' drops
    plain sessions (as TIA Portal V17+ connects with TLS), and some firmware
    refuses the HMI role; both show up as a reset connection.
    """
    use_tls = bool(data.get(CONF_USE_TLS))
    role = data.get(CONF_CONNECTION_TYPE, "hmi")
    other_role = "es" if role == "hmi" else "hmi"
    variants = [
        (use_tls, role),
        (not use_tls, role),
        (use_tls, other_role),
        (not use_tls, other_role),
    ]
    return [
        {**data, CONF_USE_TLS: tls, CONF_CONNECTION_TYPE: conn_type}
        for tls, conn_type in variants
    ]


async def _async_probe(data: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Find settings the controller accepts; return them and its identity.

    Raises the first attempt's ``PlcError`` when nothing works, since it was made
    with the settings the user entered.
    """
    first_error: PlcError | None = None
    for attempt in _alternatives(data):
        try:
            identity = await _async_probe_once(attempt)
        except PlcError as err:
            _LOGGER.debug(
                "Connecting to %s with TLS=%s as %s failed: %s",
                attempt[CONF_HOST], attempt[CONF_USE_TLS], attempt[CONF_CONNECTION_TYPE], err,
            )
            first_error = first_error or err
            continue
        if attempt != dict(data):
            _LOGGER.info(
                "Controller %s accepted TLS=%s as %s instead of the entered settings",
                attempt[CONF_HOST], attempt[CONF_USE_TLS], attempt[CONF_CONNECTION_TYPE],
            )
        return attempt, identity
    assert first_error is not None
    raise first_error


class S7PlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Siemens S7-1200/1500 controller."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return S7PlusOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TAG: TagSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_connection(user_input)

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        return await self._async_connection(user_input)

    async def _async_connection(self, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        reconfigure = self.source == SOURCE_RECONFIGURE
        defaults: dict[str, Any] = (
            dict(self._get_reconfigure_entry().data) if reconfigure else {}
        )
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            defaults = dict(user_input)
            name = user_input.get(CONF_NAME)
            data = _flatten(user_input)
            unique_id = f"{data[CONF_HOST].strip().lower()}:{data[CONF_PORT]}"
            data[CONF_HOST] = data[CONF_HOST].strip()
            try:
                data, identity = await _async_probe(data)
            except PlcError as err:
                text = str(err)
                errors["base"] = (
                    "connection_reset"
                    if "reset" in text.lower() or "104" in text
                    else "cannot_connect"
                )
                placeholders["error"] = text
            except Exception as err:  # noqa: BLE001 - beta library, show anything
                _LOGGER.exception("Unexpected error connecting to %s", data[CONF_HOST])
                errors["base"] = "cannot_connect"
                placeholders["error"] = f"{type(err).__name__}: {err}"
            else:
                if reconfigure:
                    entry = self._get_reconfigure_entry()
                    if unique_id != entry.unique_id:
                        await self.async_set_unique_id(unique_id)
                        self._abort_if_unique_id_configured()
                    return self.async_update_reload_and_abort(
                        entry, unique_id=unique_id, data=data
                    )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                _LOGGER.info("Connected to %s: %s", data[CONF_HOST], identity)
                return self.async_create_entry(
                    title=name or "PLC",
                    data=data,
                    options={
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        CONF_FAST_SCAN_INTERVAL: DEFAULT_FAST_SCAN_INTERVAL,
                        CONF_TIMEOUT: DEFAULT_TIMEOUT,
                        CONF_LINK_TIMEOUT: DEFAULT_LINK_TIMEOUT,
                        CONF_TAGS_PER_REQUEST: DEFAULT_TAGS_PER_REQUEST,
                    },
                )
        return self.async_show_form(
            step_id="reconfigure" if reconfigure else "user",
            data_schema=connection_schema(defaults, reconfigure=reconfigure),
            errors=errors,
            description_placeholders=placeholders,
        )


# --------------------------------------------------------------------------
# Options: settings, refresh tag addresses, copy tags
# --------------------------------------------------------------------------


class S7PlusOptionsFlow(OptionsFlow):
    """Controller options."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "refresh_tags", "copy_from"]
        )

    async def async_step_settings(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=o.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): vol.All(_number(0.1, 86400, 0.1, "s"), vol.Coerce(float)),
                vol.Required(
                    CONF_FAST_SCAN_INTERVAL,
                    default=o.get(CONF_FAST_SCAN_INTERVAL, DEFAULT_FAST_SCAN_INTERVAL),
                ): vol.All(_number(0.05, 3600, 0.05, "s"), vol.Coerce(float)),
                vol.Required(
                    CONF_TIMEOUT, default=o.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                ): vol.All(_number(0.5, 120, 0.5, "s"), vol.Coerce(float)),
                vol.Required(
                    CONF_LINK_TIMEOUT, default=o.get(CONF_LINK_TIMEOUT, DEFAULT_LINK_TIMEOUT)
                ): vol.All(_number(0.5, 86400, 0.5, "s"), vol.Coerce(float)),
                vol.Required(
                    CONF_TAGS_PER_REQUEST,
                    default=o.get(CONF_TAGS_PER_REQUEST, DEFAULT_TAGS_PER_REQUEST),
                ): vol.All(_number(1, 200), vol.Coerce(_int)),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_refresh_tags(self, user_input=None) -> ConfigFlowResult:
        """Re-resolve symbolic tags by name after the PLC program changed."""
        entry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            return self.async_create_entry(data=dict(entry.options))
        try:
            catalog = await entry.runtime_data.async_catalog(refresh=True)
        except PlcError as err:
            return self.async_abort(reason="browse_failed", description_placeholders={"error": str(err)})
        by_name = {record["name"]: record for record in catalog}
        updated: list[str] = []
        missing: list[str] = []
        for subentry in list(entry.subentries.values()):
            data = subentry.data
            if subentry.subentry_type != SUBENTRY_TAG or data.get(CONF_SOURCE) != SOURCE_SYMBOLIC:
                continue
            old = data[CONF_TAG]
            new = by_name.get(old["name"])
            if new is None:
                missing.append(f"{subentry.title} ({old['name']})")
            elif new != old:
                updated.append(subentry.title)
                self.hass.config_entries.async_update_subentry(
                    entry, subentry, data={**data, CONF_TAG: new}
                )
        return self.async_show_form(
            step_id="refresh_tags",
            data_schema=vol.Schema({}),
            description_placeholders={
                "updated": str(len(updated)),
                "missing": ", ".join(missing) if missing else "—",
            },
        )

    async def async_step_copy_from(self, user_input=None) -> ConfigFlowResult:
        """Copy every tag of another controller into this one."""
        sources = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != self.config_entry.entry_id
            and any(s.subentry_type == SUBENTRY_TAG for s in entry.subentries.values())
        ]
        if not sources:
            return self.async_abort(reason="no_source")
        if user_input is not None:
            source = self.hass.config_entries.async_get_entry(user_input[CONF_SOURCE_ENTRY])
            if source is not None:
                for subentry in source.subentries.values():
                    if subentry.subentry_type == SUBENTRY_TAG:
                        self.hass.config_entries.async_add_subentry(
                            self.config_entry,
                            ConfigSubentry(
                                data=dict(subentry.data),
                                subentry_type=SUBENTRY_TAG,
                                title=subentry.title,
                                unique_id=None,
                            ),
                        )
            return self.async_create_entry(data=dict(self.config_entry.options))
        return self.async_show_form(
            step_id="copy_from",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SOURCE_ENTRY): _select(
                        [SelectOptionDict(value=e.entry_id, label=e.title) for e in sources]
                    )
                }
            ),
        )


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------


def _block_of(name: str) -> str:
    return name.split(".", 1)[0]


def manual_types(platform: str) -> list[str]:
    """Datatypes a manual (absolute) address can have for this platform."""
    if platform == "binary_sensor":
        return ["BOOL"]
    if platform in ("switch", "select"):
        return INTEGER_TYPES
    if platform == "number":
        return list(NUMERIC_TYPES)
    return list(ABSOLUTE_TYPES)


def tag_schema(platform: str, source: str, softtype: str | None, d: Mapping[str, Any]) -> vol.Schema:
    """The form for one tag, pre-filled with ``d``."""
    f: dict[Any, Any] = {vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): TextSelector()}

    if source == SOURCE_ABSOLUTE:
        f[vol.Required(CONF_DB, default=d.get(CONF_DB, 1))] = vol.All(
            _number(1, 65535), vol.Coerce(_int)
        )
        f[vol.Required(CONF_BYTE, default=d.get(CONF_BYTE, 0))] = vol.All(
            _number(0, 65535), vol.Coerce(_int)
        )
        types = manual_types(platform)
        # Translation keys must be lower case; the subentry stores "REAL", "INT", ...
        f[vol.Required(CONF_TYPE, default=str(d.get(CONF_TYPE, types[0])).lower())] = (
            _select([t.lower() for t in types], "plc_type")
        )
        if "BOOL" in types:
            f[vol.Optional(CONF_BIT, default=d.get(CONF_BIT, 0))] = vol.All(
                _number(0, 7), vol.Coerce(_int)
            )
        if "STRING" in types:
            f[vol.Optional(CONF_LENGTH, description=_suggested(d, CONF_LENGTH))] = vol.All(
                _number(1, 254), vol.Coerce(_int)
            )

    numeric = source == SOURCE_ABSOLUTE or softtype in NUMERIC_TYPES
    scaling = numeric and platform in ("sensor", "number")

    def unit_field() -> None:
        f[vol.Optional(CONF_UNIT_OF_MEASUREMENT, description=_suggested(d, CONF_UNIT_OF_MEASUREMENT))] = (
            _select(COMMON_UNITS, custom=True)
        )

    def device_class(values: list[str]) -> None:
        f[vol.Optional(CONF_DEVICE_CLASS, description=_suggested(d, CONF_DEVICE_CLASS))] = (
            _select(sorted(values))
        )

    if scaling:
        f[vol.Required(CONF_SCALE, default=d.get(CONF_SCALE, 1.0))] = vol.All(
            _number(step="any"), vol.Coerce(float)
        )
        f[vol.Required(CONF_VALUE_OFFSET, default=d.get(CONF_VALUE_OFFSET, 0.0))] = vol.All(
            _number(step="any"), vol.Coerce(float)
        )

    if platform == "sensor":
        if numeric:
            f[vol.Optional(CONF_PRECISION, description=_suggested(d, CONF_PRECISION))] = vol.All(
                _number(0, 6), vol.Coerce(_int)
            )
            unit_field()
            device_class([c.value for c in SensorDeviceClass])
            f[vol.Optional(CONF_STATE_CLASS, description=_suggested(d, CONF_STATE_CLASS))] = (
                _select([c.value for c in SensorStateClass], "state_class")
            )
    elif platform == "binary_sensor":
        f[vol.Required(CONF_INVERT, default=d.get(CONF_INVERT, False))] = BooleanSelector()
        device_class([c.value for c in BinarySensorDeviceClass])
    elif platform == "switch":
        if source == SOURCE_ABSOLUTE or softtype not in BOOL_TYPES:
            f[vol.Required(CONF_ON_VALUE, default=d.get(CONF_ON_VALUE, 1))] = vol.All(
                _number(step=1), vol.Coerce(_int)
            )
            f[vol.Required(CONF_OFF_VALUE, default=d.get(CONF_OFF_VALUE, 0))] = vol.All(
                _number(step=1), vol.Coerce(_int)
            )
        f[vol.Required(CONF_INVERT, default=d.get(CONF_INVERT, False))] = BooleanSelector()
    elif platform == "number":
        f[vol.Required(CONF_MIN, default=d.get(CONF_MIN, 0))] = vol.All(
            _number(step="any"), vol.Coerce(float)
        )
        f[vol.Required(CONF_MAX, default=d.get(CONF_MAX, 100))] = vol.All(
            _number(step="any"), vol.Coerce(float)
        )
        f[vol.Required(CONF_STEP, default=d.get(CONF_STEP, 1))] = vol.All(
            _number(step="any"), vol.Coerce(float)
        )
        f[vol.Required(CONF_MODE, default=d.get(CONF_MODE, NumberMode.BOX.value))] = _select(
            [m.value for m in NumberMode], "number_mode"
        )
        unit_field()
        device_class([c.value for c in NumberDeviceClass])
    elif platform == "select":
        f[vol.Required(CONF_OPTIONS, default=d.get(CONF_OPTIONS, "0=Off\n1=On"))] = TextSelector(
            TextSelectorConfig(multiline=True)
        )

    f[vol.Required(CONF_FAST_POLL, default=d.get(CONF_FAST_POLL, False))] = BooleanSelector()
    f[vol.Required(CONF_TEST_READ, default=False)] = BooleanSelector()
    return vol.Schema(f)


def class_mismatch(platform: str, data: Mapping[str, Any]) -> tuple[str, str] | None:
    """(error key, what is allowed) when unit or state class contradict the class.

    Home Assistant only accepts certain units per device class (power in W/kW,
    energy in Wh/kWh, ...) and certain state classes; a mismatch only shows up
    later as a warning in the log, so the form catches it instead.
    """
    if not (cls := data.get(CONF_DEVICE_CLASS)):
        return None
    units_by_class = SENSOR_CLASS_UNITS if platform == "sensor" else NUMBER_CLASS_UNITS
    try:
        allowed = units_by_class.get(
            SensorDeviceClass(cls) if platform == "sensor" else NumberDeviceClass(cls)
        )
    except ValueError:
        return None
    unit = data.get(CONF_UNIT_OF_MEASUREMENT) or None
    if allowed is not None and unit not in allowed:
        names = sorted(str(u) for u in allowed if u is not None)
        if None in allowed:
            names.append("—")
        return "unit_not_for_class", ", ".join(names)
    state_class = data.get(CONF_STATE_CLASS)
    if platform == "sensor" and state_class:
        classes = DEVICE_CLASS_STATE_CLASSES.get(SensorDeviceClass(cls))
        if classes is not None and SensorStateClass(state_class) not in classes:
            return "state_class_not_for_class", ", ".join(
                sorted(str(c) for c in classes)
            ) or "—"
    return None


def validate_tag(platform: str, data: dict[str, Any]) -> str | None:
    """An error key, or None when the tag is consistent."""
    if data.get(CONF_SCALE) == 0:
        return "invalid_scale"
    if platform == "number" and data[CONF_MIN] >= data[CONF_MAX]:
        return "invalid_range"
    if platform == "select":
        try:
            parse_options(data[CONF_OPTIONS])
        except ValueError:
            return "invalid_options"
    if data.get(CONF_TYPE) == "STRING" and not data.get(CONF_LENGTH):
        return "length_required"
    try:
        TagSpec.from_config("check", data)
    except (ValueError, KeyError, TypeError):
        return "invalid_tag"
    return None


class TagSubentryFlow(ConfigSubentryFlow):
    """Add or edit one tag of a controller."""

    def __init__(self) -> None:
        self._platform: str = "sensor"
        self._source: str = SOURCE_SYMBOLIC
        self._record: dict[str, Any] | None = None
        self._block: str | None = None
        self._filter: str = ""
        self._pending: tuple[str, dict[str, Any]] | None = None
        # The platform steps first lead to the source menu; once a tag is
        # chosen, the same step ids show and submit the tag form.
        self._in_form = False

    # -- what the tag becomes ------------------------------------------------

    async def async_step_user(self, user_input=None) -> SubentryFlowResult:
        return self.async_show_menu(step_id="user", menu_options=list(PLATFORMS_ORDER))

    async def _choose_source(self, platform: str) -> SubentryFlowResult:
        self._platform = platform
        return await self.async_step_source()

    async def async_step_source(self, user_input=None) -> SubentryFlowResult:
        """Pick the tag from the controller's catalog, or type its address."""
        return self.async_show_menu(step_id="source", menu_options=["from_plc", "manual"])

    async def async_step_sensor(self, user_input=None) -> SubentryFlowResult:
        if self._configuring:
            return await self._async_tag_form("sensor", user_input)
        return await self._choose_source("sensor")

    async def async_step_binary_sensor(self, user_input=None) -> SubentryFlowResult:
        if self._configuring:
            return await self._async_tag_form("binary_sensor", user_input)
        return await self._choose_source("binary_sensor")

    async def async_step_switch(self, user_input=None) -> SubentryFlowResult:
        if self._configuring:
            return await self._async_tag_form("switch", user_input)
        return await self._choose_source("switch")

    async def async_step_number(self, user_input=None) -> SubentryFlowResult:
        if self._configuring:
            return await self._async_tag_form("number", user_input)
        return await self._choose_source("number")

    async def async_step_select(self, user_input=None) -> SubentryFlowResult:
        if self._configuring:
            return await self._async_tag_form("select", user_input)
        return await self._choose_source("select")

    @property
    def _configuring(self) -> bool:
        """True once the source is settled and the tag form is being shown."""
        return self._in_form

    def _start_form(self) -> None:
        self._in_form = True

    # -- where the tag comes from --------------------------------------------

    async def async_step_manual(self, user_input=None) -> SubentryFlowResult:
        self._source = SOURCE_ABSOLUTE
        self._start_form()
        return await self._async_tag_form(self._platform, None)

    def _runtime(self):
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return None
        return entry.runtime_data

    async def async_step_from_plc(self, user_input=None) -> SubentryFlowResult:
        """Pick the block (data block or I/Q/M area) holding the tag."""
        runtime = self._runtime()
        if runtime is None:
            return self.async_abort(reason="not_loaded")
        try:
            catalog = await runtime.async_catalog()
        except PlcError as err:
            return self.async_abort(reason="browse_failed", description_placeholders={"error": str(err)})
        usable = [r for r in catalog if self._platform in platforms_for_type(r["data_type"])]
        if not usable:
            return self.async_abort(reason="no_tags")
        if user_input is not None:
            self._block = user_input[CONF_BLOCK]
            self._filter = (user_input.get(CONF_FILTER) or "").strip().lower()
            return await self.async_step_pick_tag()
        counts: dict[str, int] = {}
        for record in usable:
            counts[_block_of(record["name"])] = counts.get(_block_of(record["name"]), 0) + 1
        options = [
            SelectOptionDict(value=block, label=f"{block} ({count})")
            for block, count in sorted(counts.items())
        ]
        return self.async_show_form(
            step_id="from_plc",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BLOCK, default=options[0]["value"]): _select(options),
                    vol.Optional(CONF_FILTER): TextSelector(),
                }
            ),
            description_placeholders={"count": str(len(usable))},
        )

    async def async_step_pick_tag(self, user_input=None) -> SubentryFlowResult:
        """Pick the tag inside the chosen block."""
        runtime = self._runtime()
        if runtime is None:
            return self.async_abort(reason="not_loaded")
        catalog = await runtime.async_catalog()
        matches = [
            r
            for r in catalog
            if _block_of(r["name"]) == self._block
            and self._platform in platforms_for_type(r["data_type"])
            and self._filter in r["name"].lower()
        ]
        if user_input is not None:
            chosen = next((r for r in matches if r["name"] == user_input[CONF_TAG_NAME]), None)
            if chosen is not None:
                self._record = dict(chosen)
                self._source = SOURCE_SYMBOLIC
                self._start_form()
                return await self._async_tag_form(self._platform, None)
        if not matches:
            return self.async_abort(reason="no_tags")
        shown = sorted(matches, key=lambda r: r["name"])[:MAX_TAGS_IN_LIST]
        options = [
            SelectOptionDict(value=r["name"], label=f"{r['name']}  [{r['data_type']}]")
            for r in shown
        ]
        return self.async_show_form(
            step_id="pick_tag",
            data_schema=vol.Schema({vol.Required(CONF_TAG_NAME): _select(options)}),
            description_placeholders={
                "count": str(len(matches)),
                "shown": str(len(shown)),
            },
        )

    # -- editing -------------------------------------------------------------

    async def async_step_reconfigure(self, user_input=None) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        self._platform = subentry.data[CONF_PLATFORM]
        self._source = subentry.data.get(CONF_SOURCE, SOURCE_SYMBOLIC)
        self._record = subentry.data.get(CONF_TAG)
        self._start_form()
        return await self._async_tag_form(self._platform, None)

    # -- the tag form --------------------------------------------------------

    def _tag_text(self) -> str:
        if self._source == SOURCE_SYMBOLIC and self._record:
            return f"{self._record['name']} [{self._record['data_type']}]"
        return "DB"

    async def _async_tag_form(
        self, platform: str, user_input: dict[str, Any] | None
    ) -> SubentryFlowResult:
        reconfigure = self.source == SOURCE_RECONFIGURE
        softtype = self._record["data_type"] if self._record else None
        if user_input is None:
            if reconfigure:
                subentry = self._get_reconfigure_subentry()
                defaults: dict[str, Any] = {**subentry.data, CONF_NAME: subentry.title}
            else:
                defaults = {}
                if self._record:
                    defaults[CONF_NAME] = self._record["name"].rsplit(".", 1)[-1]
        else:
            defaults = dict(user_input)
        errors: dict[str, str] = {}
        placeholders = {"tag": self._tag_text(), "error": "", "allowed": ""}

        if user_input is not None:
            test_read = user_input.pop(CONF_TEST_READ, False)
            name = user_input.pop(CONF_NAME).strip()
            data: dict[str, Any] = {CONF_PLATFORM: platform, CONF_SOURCE: self._source, **user_input}
            if CONF_TYPE in data:
                data[CONF_TYPE] = str(data[CONF_TYPE]).upper()
            if self._source == SOURCE_SYMBOLIC:
                data[CONF_TAG] = self._record
            if (error := validate_tag(platform, data)) is not None:
                errors["base"] = error
            elif platform in ("sensor", "number") and (
                mismatch := class_mismatch(platform, data)
            ):
                errors["base"], placeholders["allowed"] = mismatch
            elif test_read:
                value, error, text = await self._async_test_read(data)
                if error is None:
                    self._pending = (name, data)
                    return self.async_show_form(
                        step_id="test_result",
                        data_schema=vol.Schema({}),
                        description_placeholders={"name": name or self._tag_text(), "value": str(value)},
                    )
                errors["base"] = error
                placeholders["error"] = text
            else:
                return self._async_save(name, data)

        return self.async_show_form(
            step_id=platform,
            data_schema=tag_schema(platform, self._source, softtype, defaults),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_test_result(self, user_input=None) -> SubentryFlowResult:
        assert self._pending is not None
        name, data = self._pending
        return self._async_save(name, data)

    async def _async_test_read(self, data: dict[str, Any]) -> tuple[Any, str | None, str]:
        runtime = self._runtime()
        if runtime is None:
            return None, "not_loaded", ""
        spec = TagSpec.from_config("probe", data)
        try:
            result = await runtime.plc.read([spec])
        except PlcError as err:
            return None, "read_failed", str(err)
        if "probe" in result.failed:
            return None, "read_failed", result.failed["probe"]
        return result.values.get("probe"), None, ""

    @callback
    def _async_save(self, name: str, data: dict[str, Any]) -> SubentryFlowResult:
        if self._source == SOURCE_SYMBOLIC and self._record:
            fallback = self._record["name"]
        else:
            fallback = f"DB{data.get(CONF_DB)}.{data.get(CONF_BYTE)}"
        title = name or fallback
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_and_abort(
                self._get_entry(), self._get_reconfigure_subentry(), title=title, data=data
            )
        return self.async_create_entry(title=title, data=data)
