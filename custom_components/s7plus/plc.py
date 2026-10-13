"""Controller layer: S7CommPlus reads and writes for UI-defined tags.

No Home Assistant imports, so it can be tested against the s7commplus server
emulator or a fake client without Home Assistant.

Two ways to address a tag:

* **symbolic** — a tag picked from the controller's own tag catalog
  (``browse()``). Its browse record is stored, so starting up needs no browse,
  and optimized data blocks work. This is the real S7CommPlus way.
* **absolute** — data block number + byte offset (+ bit) in a *non-optimized*
  data block, for when browsing is not available.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import logging
import struct
from typing import Any

from .const import (
    CONF_BIT,
    CONF_BYTE,
    CONF_DB,
    CONF_LENGTH,
    CONF_PLATFORM,
    CONF_SCALE,
    CONF_SOURCE,
    CONF_TAG,
    CONF_TYPE,
    CONF_VALUE_OFFSET,
    SOURCE_ABSOLUTE,
    SOURCE_SYMBOLIC,
)

_LOGGER = logging.getLogger(__name__)

# Softdatatype name -> (struct format, size) for scalar numbers (big-endian).
NUMERIC_TYPES: dict[str, tuple[str, int]] = {
    "BYTE": (">B", 1),
    "USINT": (">B", 1),
    "SINT": (">b", 1),
    "WORD": (">H", 2),
    "UINT": (">H", 2),
    "INT": (">h", 2),
    "DWORD": (">I", 4),
    "UDINT": (">I", 4),
    "DINT": (">i", 4),
    "REAL": (">f", 4),
    "LWORD": (">Q", 8),
    "ULINT": (">Q", 8),
    "LINT": (">q", 8),
    "LREAL": (">d", 8),
    "TIME": (">i", 4),  # milliseconds
    "LTIME": (">q", 8),  # nanoseconds
}
FLOAT_TYPES = frozenset({"REAL", "LREAL"})
BOOL_TYPES = frozenset({"BOOL", "BBOOL"})
TEXT_TYPES = frozenset({"STRING", "WSTRING", "CHAR"})
SUPPORTED_TYPES = BOOL_TYPES | TEXT_TYPES | frozenset(NUMERIC_TYPES)
# Types an absolute address can carry (S7 STRING has its 2-byte header).
ABSOLUTE_TYPES = ("BOOL", *NUMERIC_TYPES, "STRING")

WRITABLE_PLATFORMS = frozenset({"switch", "number", "select"})

# Absolute reads: merge nearby ranges of one data block into one request.
_MAX_GAP = 32
_MAX_SPAN = 200


class PlcError(Exception):
    """Base error of the controller layer."""


class PlcConnectionError(PlcError):
    """The link to the controller failed; it is dropped and reopened later."""


class PlcWriteError(PlcError):
    """The controller refused a write."""


def _error_classes() -> tuple[tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
    """(link errors, request errors) of the library.

    A link error means the session is gone or out of step: it is dropped and
    reopened on the next request. A request error means the controller answered
    but refused this request: the session is kept and only those tags fail.
    """
    link: list[type[BaseException]] = [OSError, EOFError, TimeoutError, ConnectionError]
    request: list[type[BaseException]] = [RuntimeError]
    try:
        from s7commplus import error as s7  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return tuple(link), tuple(request)
    for name in (
        "S7ConnectionError",
        "S7TimeoutError",
        "S7IntegrityError",
        "S7StalePacketError",
        "S7PacketLostError",
    ):
        if (cls := getattr(s7, name, None)) is not None:
            link.append(cls)
    # Any other library error (protocol, rate limit, ...) is a refused request.
    request.append(s7.S7Error)
    return tuple(link), tuple(request)


def _wire_datatype(softtype: str) -> Any:
    """The S7CommPlus wire datatype for a Softdatatype name (for writes)."""
    from s7commplus.catalog import _WIRE_TYPES  # noqa: PLC0415
    from s7commplus.typeinfo import Softdatatype  # noqa: PLC0415

    return _WIRE_TYPES.get(Softdatatype[softtype])


def browse_record_supported(record: Mapping[str, Any]) -> bool:
    """Whether a browse record is a scalar tag this integration can use."""
    return record.get("data_type") in SUPPORTED_TYPES and not record.get(
        "array_dimensions"
    )


def platforms_for_type(softtype: str) -> tuple[str, ...]:
    """Which Home Assistant platforms make sense for a PLC datatype."""
    if softtype in BOOL_TYPES:
        return ("binary_sensor", "switch", "sensor")
    if softtype in TEXT_TYPES:
        return ("sensor",)
    if softtype in FLOAT_TYPES or softtype in ("TIME", "LTIME"):
        return ("sensor", "number")
    return ("sensor", "number", "select", "switch")


@dataclass
class TagSpec:
    """One configured tag: where it lives, how to decode and encode it."""

    key: str
    source: str
    softtype: str
    writable: bool
    scale: float = 1.0
    offset: float = 0.0
    # symbolic
    symbolic: Any = None  # s7commplus.SymbolicTag
    # absolute
    db: int = 0
    byte: int = 0
    bit: int = 0
    length: int = 0

    @classmethod
    def from_config(cls, key: str, data: Mapping[str, Any]) -> TagSpec:
        """Build a tag from its subentry data.

        Raises ``ValueError`` for an inconsistent description.
        """
        platform = data[CONF_PLATFORM]
        writable = platform in WRITABLE_PLATFORMS
        raw_scale = data.get(CONF_SCALE)
        scale = 1.0 if raw_scale is None else float(raw_scale)
        offset = float(data.get(CONF_VALUE_OFFSET) or 0.0)
        if scale == 0:
            raise ValueError("scale cannot be 0")
        source = data.get(CONF_SOURCE, SOURCE_SYMBOLIC)
        if source == SOURCE_SYMBOLIC:
            from s7commplus import SymbolicTag  # noqa: PLC0415

            record = data[CONF_TAG]
            if not browse_record_supported(record):
                raise ValueError(f"unsupported tag type {record.get('data_type')!r}")
            symbolic = SymbolicTag.from_browse(record)
            if writable and symbolic.datatype is None:
                raise ValueError("this tag type cannot be written")
            if writable and record["data_type"] in TEXT_TYPES:
                raise ValueError("text tags are read-only")
            return cls(
                key=key,
                source=source,
                softtype=record["data_type"],
                writable=writable,
                scale=scale,
                offset=offset,
                symbolic=symbolic,
            )
        if source != SOURCE_ABSOLUTE:
            raise ValueError(f"unknown tag source {source!r}")
        softtype = data[CONF_TYPE]
        if softtype not in ABSOLUTE_TYPES:
            raise ValueError(f"unsupported type {softtype!r}")
        if writable and softtype in BOOL_TYPES:
            # Writing one bit means rewriting its whole byte, which would race
            # the PLC program updating the neighbouring bits. Use a symbolic tag.
            raise ValueError("absolute BOOL tags are read-only")
        if writable and softtype in TEXT_TYPES:
            raise ValueError("text tags are read-only")
        bit = int(data.get(CONF_BIT) or 0)
        if not 0 <= bit <= 7:
            raise ValueError("bit must be 0-7")
        length = int(data.get(CONF_LENGTH) or 0)
        if softtype == "STRING" and not 1 <= length <= 254:
            raise ValueError("STRING length must be 1-254")
        return cls(
            key=key,
            source=source,
            softtype=softtype,
            writable=writable,
            scale=scale,
            offset=offset,
            db=int(data[CONF_DB]),
            byte=int(data[CONF_BYTE]),
            bit=bit,
            length=length,
        )

    # -- sizes and addresses -------------------------------------------------

    @property
    def size(self) -> int:
        """Bytes an absolute read needs."""
        if self.softtype in BOOL_TYPES:
            return 1
        if self.softtype == "STRING":
            return self.length + 2
        return NUMERIC_TYPES[self.softtype][1]

    @property
    def numeric(self) -> bool:
        return self.softtype in NUMERIC_TYPES

    # -- decoding ------------------------------------------------------------

    def _scaled(self, value: float | int) -> float | int:
        if self.scale == 1.0 and self.offset == 0.0:
            return value
        return round(value * self.scale + self.offset, 6)

    def decode(self, raw: bytes) -> Any:
        """Turn the bytes the controller returned into a value.

        Raises ``ValueError`` when the bytes do not fit the type.
        """
        if self.source == SOURCE_ABSOLUTE:
            if self.softtype in BOOL_TYPES:
                if len(raw) < 1:
                    raise ValueError("no data")
                return bool((raw[0] >> self.bit) & 1)
            if self.softtype == "STRING":
                if len(raw) < 2:
                    raise ValueError("no data")
                count = min(raw[1], self.length, len(raw) - 2)
                return raw[2 : 2 + count].decode("latin-1")
        elif self.softtype in BOOL_TYPES:
            if len(raw) != 1:
                raise ValueError("unexpected BOOL size")
            return raw != b"\x00"
        elif self.softtype in TEXT_TYPES:
            value = self.symbolic.decode_value(raw)
            if isinstance(value, bytes):
                return value.decode("latin-1", errors="replace")
            return value.rstrip("\x00")
        fmt, size = NUMERIC_TYPES[self.softtype]
        if len(raw) != size:
            raise ValueError(f"expected {size} bytes, got {len(raw)}")
        value = struct.unpack(fmt, raw)[0]
        if self.softtype in FLOAT_TYPES and value != value:  # NaN
            return None
        if self.softtype == "LTIME":
            value = value / 1_000_000  # ns -> ms
        return self._scaled(value)

    def encode(self, value: Any) -> bytes:
        """Encode a value for writing.

        Raises ``ValueError`` when it does not fit the type.
        """
        if self.softtype in BOOL_TYPES:
            return b"\x01" if value else b"\x00"
        if not self.numeric:
            raise ValueError("this tag cannot be written")
        number = float(value)
        if self.scale != 1.0 or self.offset != 0.0:
            number = (number - self.offset) / self.scale
        if self.softtype == "LTIME":
            number *= 1_000_000
        fmt, _size = NUMERIC_TYPES[self.softtype]
        try:
            if self.softtype in FLOAT_TYPES:
                return struct.pack(fmt, number)
            return struct.pack(fmt, round(number))
        except struct.error as err:
            raise ValueError(f"{value} does not fit {self.softtype}") from err

    @property
    def address_text(self) -> str:
        """Human-readable address, for logs and the UI."""
        if self.source == SOURCE_SYMBOLIC:
            return self.symbolic.name
        if self.softtype in BOOL_TYPES:
            return f"DB{self.db}.DBX{self.byte}.{self.bit}"
        return f"DB{self.db}.{self.byte} ({self.softtype})"


@dataclass(frozen=True, kw_only=True)
class ConnectParams:
    """How to reach the controller."""

    host: str
    port: int = 102
    connection_type: str = "hmi"
    use_tls: bool = False
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    password: str | None = None


@dataclass
class ReadResult:
    """Values read in one poll, and the tags that could not be read."""

    values: dict[str, Any] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


def _default_client_factory() -> Any:
    from s7commplus import AsyncClient  # noqa: PLC0415

    return AsyncClient()


class PlcConnection:
    """One S7CommPlus session to a controller, shared by all its tags.

    S7-1200/1500 controllers have few connection resources, so every poll and
    write of one controller goes through this single session. It connects on
    demand and reconnects on the next request after a failure.
    """

    def __init__(
        self,
        params: ConnectParams,
        *,
        timeout: float = 5.0,
        tags_per_request: int = 20,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.params = params
        self.timeout = timeout
        self.tags_per_request = max(1, tags_per_request)
        self._factory = client_factory or _default_client_factory
        self._client: Any = None
        self._connect_lock = asyncio.Lock()
        # One library operation at a time. Waiting here does not count against
        # the response timeout: a long catalog browse must not make a queued
        # poll time out and tear the session down under the browse.
        self._op_lock = asyncio.Lock()
        self.identity: str | None = None
        self._cpu_state_supported = True
        self._link_errors, self._request_errors = _error_classes()

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "connected", False))

    async def _ensure(self) -> Any:
        async with self._connect_lock:
            if self.connected:
                return self._client
            await self._drop()
            client = self._factory()
            p = self.params
            try:
                async with asyncio.timeout(self.timeout + 10):
                    await client.connect(
                        p.host,
                        p.port,
                        use_tls=p.use_tls,
                        tls_ca=p.tls_ca or None,
                        tls_cert=p.tls_cert or None,
                        tls_key=p.tls_key or None,
                        password=p.password or None,
                        connection_type=p.connection_type,
                    )
            except (*self._link_errors, *self._request_errors) as err:
                await self._safe_disconnect(client)
                raise PlcConnectionError(f"{type(err).__name__}: {err}" if str(err) else type(err).__name__) from err
            self._client = client
            self.identity = self._read_identity(client)
            return client

    @staticmethod
    def _read_identity(client: Any) -> str | None:
        """Order number and firmware, e.g. '1;6ES7 512-1CK01-0AB0;V2.9'."""
        try:
            from s7commplus.legitimation import (  # noqa: PLC0415
                extract_session_version_string,
            )

            raw = getattr(client, "_server_session_version", None)
            return extract_session_version_string(raw) if raw else None
        except Exception:  # noqa: BLE001 - purely informative
            return None

    @staticmethod
    async def _safe_disconnect(client: Any) -> None:
        try:
            async with asyncio.timeout(2):
                await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def _drop(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await self._safe_disconnect(client)

    async def close(self) -> None:
        """Close the session (on unload)."""
        async with self._connect_lock:
            await self._drop()

    async def _call(self, func: Callable[[Any], Any], timeout: float | None = None) -> Any:
        """Run one library call; a link-level failure drops the session.

        The timeout covers only the call itself, not the wait for a previous
        operation (e.g. a catalog browse) to finish.
        """
        async with self._op_lock:
            client = await self._ensure()
            try:
                async with asyncio.timeout(timeout or self.timeout):
                    return await func(client)
            except self._link_errors as err:
                await self._drop()
                raise PlcConnectionError(str(err) or type(err).__name__) from err

    # -- reading -------------------------------------------------------------

    async def read(self, specs: list[TagSpec]) -> ReadResult:
        """Read every tag in as few requests as possible.

        Raises ``PlcConnectionError`` when the link fails.
        """
        result = ReadResult()
        symbolic = [s for s in specs if s.source == SOURCE_SYMBOLIC]
        absolute = [s for s in specs if s.source == SOURCE_ABSOLUTE]
        for start in range(0, len(symbolic), self.tags_per_request):
            await self._read_symbolic(symbolic[start : start + self.tags_per_request], result)
        await self._read_absolute(absolute, result)
        return result

    def _store(self, spec: TagSpec, raw: bytes | None, result: ReadResult) -> None:
        if raw is None or raw == b"":
            result.failed[spec.key] = "no data returned"
            return
        try:
            result.values[spec.key] = spec.decode(raw)
        except (ValueError, struct.error, UnicodeDecodeError) as err:
            result.failed[spec.key] = f"cannot decode: {err}"

    async def _read_symbolic(self, chunk: list[TagSpec], result: ReadResult) -> None:
        items = [(s.symbolic.access_area, list(s.symbolic.lids), 0) for s in chunk]
        try:
            raws = await self._call(lambda c: c.read_symbolic_multi(items))
        except self._request_errors:
            # The controller refused the batch: isolate the bad tag(s).
            answered = False
            for spec, item in zip(chunk, items, strict=True):
                try:
                    raw = await self._call(
                        lambda c, it=item: c.read_symbolic(it[0], it[1], 0)
                    )
                except self._request_errors as err:
                    result.failed[spec.key] = str(err) or type(err).__name__
                else:
                    answered = True
                    self._store(spec, raw, result)
            if not answered and len(chunk) > 1:
                # Nothing at all was accepted: the session may be out of step.
                await self.close()
            return
        for spec, raw in zip(chunk, raws, strict=False):
            self._store(spec, raw, result)

    async def _read_absolute(self, specs: list[TagSpec], result: ReadResult) -> None:
        # Merge nearby ranges of the same data block into single read items.
        ranges: list[tuple[int, int, int, list[TagSpec]]] = []  # db, start, end, specs
        for spec in sorted(specs, key=lambda s: (s.db, s.byte)):
            end = spec.byte + spec.size
            if (
                ranges
                and ranges[-1][0] == spec.db
                and spec.byte - ranges[-1][2] <= _MAX_GAP
                and max(end, ranges[-1][2]) - ranges[-1][1] <= _MAX_SPAN
            ):
                db, start, old_end, members = ranges[-1]
                ranges[-1] = (db, start, max(end, old_end), [*members, spec])
            else:
                ranges.append((spec.db, spec.byte, end, [spec]))
        for begin in range(0, len(ranges), self.tags_per_request):
            chunk = ranges[begin : begin + self.tags_per_request]
            items = [(db, start, end - start) for db, start, end, _ in chunk]
            try:
                blocks = await self._call(lambda c, it=items: c.db_read_multi(it))
            except self._request_errors as err:
                for *_, members in chunk:
                    for spec in members:
                        result.failed[spec.key] = str(err)
                continue
            for (db, start, end, members), block in zip(chunk, blocks, strict=False):
                for spec in members:
                    lo = spec.byte - start
                    raw = block[lo : lo + spec.size] if len(block) >= lo + spec.size else b""
                    self._store(spec, raw, result)

    # -- writing -------------------------------------------------------------

    async def write(self, spec: TagSpec, value: Any) -> None:
        """Write one tag.

        Raises ``ValueError`` for a value that does not fit, ``PlcWriteError``
        when the controller refuses, ``PlcConnectionError`` on link failure.
        """
        if not spec.writable:
            raise ValueError("this tag is read-only")
        data = spec.encode(value)
        datatype = _wire_datatype(spec.softtype)
        try:
            if spec.source == SOURCE_SYMBOLIC:
                tag = spec.symbolic
                await self._call(
                    lambda c: c.write_symbolic(
                        tag.access_area, list(tag.lids), data, 0, datatype=tag.datatype
                    )
                )
            else:
                await self._call(
                    lambda c: c.db_write(spec.db, spec.byte, data, datatype=datatype)
                )
        except self._request_errors as err:
            raise PlcWriteError(str(err) or type(err).__name__) from err

    # -- catalog and status --------------------------------------------------

    async def browse(self) -> list[dict[str, Any]]:
        """The controller's tag catalog, scalar tags only (can take a while)."""
        try:
            records = await self._call(lambda c: c.browse(), timeout=max(self.timeout, 120))
        except (*self._request_errors, ValueError, KeyError) as err:
            raise PlcError(f"browse failed: {err or type(err).__name__}") from err
        return [r for r in records if browse_record_supported(r)]

    async def cpu_state(self) -> str | None:
        """'RUN', 'STOP' or None when the controller does not say.

        Reading the CPU state is experimental in the library. A controller that
        refuses it is not asked again, so it can never disturb tag polling.
        """
        if not self._cpu_state_supported:
            return None
        try:
            state = await self._call(lambda c: c.get_cpu_state())
        except (*self._request_errors, ValueError) as err:
            self._cpu_state_supported = False
            _LOGGER.info("Controller does not report its CPU state (%s); not asking again", err)
            return None
        return state if state in ("RUN", "STOP") else None

def build_specs(
    tags: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, TagSpec], dict[str, str]]:
    """Build every tag; return the valid ones and the reasons for the rest."""
    specs: dict[str, TagSpec] = {}
    invalid: dict[str, str] = {}
    for key, data in tags.items():
        try:
            specs[key] = TagSpec.from_config(key, data)
        except (ValueError, KeyError, TypeError) as err:
            invalid[key] = str(err)
    return specs, invalid
