"""The versioned exact capture codec (spec step 1, section 3).

Requirements this file exists to satisfy:

* "Serialize enums, UTC/timezone metadata, numbers, nulls, DataFrame index/column
  order and dtypes without rounding or silently dropping rows."
* "Store arrays/frames as typed Arrow/Parquet objects."
* "Use a versioned codec with round-trip tests; reject unsupported types as
  capture gaps."
* "Copy/freeze the bundle before later mutations."

So: JSON for the value tree (tagged for every type JSON cannot express), a
SEPARATE Arrow IPC object per DataFrame/Series referenced from that tree by
content hash, and :class:`UnsupportedCaptureType` for anything else -- a refusal
that the caller records as a capture gap, never a coercion.

Two properties that are easy to lose and expensive to discover later:

* **Tags are escaped, not assumed.** A provider payload may legitimately contain
  a key named ``$decimal`` or ``$enum``. Every user dict key starting with ``$``
  is written with a doubled sigil and unescaped on decode, so a payload can never
  be re-read as a different value -- and ``$enum`` can never be used to import an
  arbitrary module (resolution is limited to :data:`ENUM_MODULE_PREFIXES`).
* **Hashes are environment-stable.** pyarrow stamps its own version and the
  pandas version into the table's pandas metadata; both are stripped before the
  IPC bytes are written, so the same frame content-addresses identically across
  hosts and after a pyarrow/pandas upgrade.

Host-neutral: stdlib + pandas/numpy/pyarrow only.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple, Union
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow as pa

CODEC_VERSION = 1

KIND_JSON = "json"
KIND_ARROW = "arrow"

#: Enum and dataclass types may only be resolved from these module prefixes. A
#: capture is data, not code: an ``$enum`` or ``$dataclass`` payload must never be
#: able to name an arbitrary importable module.
ENUM_MODULE_PREFIXES = (
    "ba2_common.",
    "ba2_providers.",
    "ba2_experts.",
    "ba2_trade_platform.",
)

#: Arrow schema-metadata key holding this codec's frame meta, so the object's
#: bytes alone fully determine its meaning (content addressing stays honest).
_ARROW_META_KEY = b"ba2_replay"

#: pandas-metadata keys that carry the writing environment's versions.
_ENV_METADATA_KEYS = ("creator", "pandas_version")

#: Sentinel column name for a Series stored as a one-column table.
_SERIES_COLUMN = "__ba2_replay_series__"

_TAGS = (
    "$enum",
    "$dataclass",
    "$float",
    "$nat",
    "$timestamp",
    "$datetime",
    "$date",
    "$decimal",
    "$np",
    "$frame",
)

__all__ = [
    "CODEC_VERSION",
    "CodecDrift",
    "KIND_JSON",
    "KIND_ARROW",
    "ENUM_MODULE_PREFIXES",
    "Encoded",
    "UnsupportedCaptureType",
    "UnsafeEnumReference",
    "encode",
    "decode",
    "content_hash",
    "extract_meta",
    "frame_refs",
    "freeze",
]


class UnsupportedCaptureType(TypeError):
    """A value the codec refuses to represent -- an explicit capture gap.

    Carries the offending ``type_name`` and the ``path`` inside the captured
    object so the gap can be reported precisely instead of "capture failed".
    """

    def __init__(self, type_name: str, path: str):
        super().__init__(f"unsupported capture type {type_name!r} at {path}")
        self.type_name = type_name
        self.path = path


class UnsafeEnumReference(ValueError):
    """An ``$enum`` payload named a module outside :data:`ENUM_MODULE_PREFIXES`."""

    def __init__(self, reference: str):
        super().__init__(
            f"refusing to resolve enum reference {reference!r}: module is not one of "
            f"{ENUM_MODULE_PREFIXES}"
        )
        self.reference = reference


class CodecDrift(ValueError):
    """A stored record no longer matches the class it names.

    The point of recording a dataclass by NAME is that a replay rebuilds the real
    object; that only holds while the class still has the fields the record was
    written with. When it does not -- a field renamed or removed, a new required
    field added, a field that cannot be passed to the constructor -- the honest
    answer is "this record cannot be replayed against today's code", not an
    object silently missing a field, nor a bare ``TypeError`` out of ``__init__``
    that reads like a bug in the replay tool.
    """

    def __init__(self, type_name: str, field: str, reason: str):
        super().__init__(
            f"recorded {type_name} does not match the current class: "
            f"field {field!r} {reason}"
        )
        self.type_name = type_name
        self.field = field
        self.reason = reason


class Encoded(NamedTuple):
    """``(kind, data, meta)`` plus the separate frame objects it references.

    ``sides`` holds the Arrow IPC objects for every DataFrame/Series inside
    ``data``; they must be published to the object store before anything commits
    a reference to the root object.
    """

    kind: str
    data: bytes
    meta: Dict[str, Any]
    sides: Tuple["Encoded", ...] = ()

    def frame_map(self) -> Dict[str, "Encoded"]:
        """``{content_hash: Encoded}`` for the referenced frame objects.

        Re-hashes each side (sha256 over the IPC bytes): an object cannot carry
        its own hash inside itself, so this is the only way to key them.
        """
        return {content_hash(side.kind, side.data): side for side in self.sides}


def content_hash(kind: str, data: bytes) -> str:
    """sha256 hex of an object's kind + bytes."""
    digest = hashlib.sha256()
    digest.update(kind.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(data)
    return digest.hexdigest()


# --------------------------------------------------------------------------- encode


def encode(obj: Any) -> Encoded:
    """Encode ``obj`` exactly.

    A DataFrame/Series at the root becomes an Arrow object; anything else
    becomes a tagged JSON object whose nested frames are returned in ``sides``.
    """
    if isinstance(obj, (pd.DataFrame, pd.Series)):
        return _encode_frame(obj, "$")
    sides: List[Encoded] = []
    seen: Dict[str, None] = {}
    payload = _encode_value(obj, "$", sides, seen)
    body = {"codec_version": CODEC_VERSION, "object_kind": KIND_JSON, "payload": payload}
    data = json.dumps(
        body, allow_nan=False, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    meta = {"codec_version": CODEC_VERSION, "object_kind": KIND_JSON}
    return Encoded(kind=KIND_JSON, data=data, meta=meta, sides=tuple(sides))


def _escape_key(key: str) -> str:
    """User keys starting with ``$`` get a doubled sigil so tags stay unambiguous."""
    return f"${key}" if key.startswith("$") else key


def _unescape_key(key: str) -> str:
    return key[1:] if key.startswith("$$") else key


def _encode_value(obj: Any, path: str, sides: List[Encoded], seen: Dict[str, None]) -> Any:
    # Order matters: bool before int, Enum before str/int (str-Enums are str),
    # pandas NaT/Timestamp before datetime, numpy scalars before float/int.
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, Enum):
        cls = type(obj)
        reference = f"{cls.__module__}.{cls.__qualname__}.{obj.name}"
        if not _enum_module_allowed(cls.__module__):
            # Capturing it would produce a record replay must refuse to decode:
            # report the gap now, where the health counter can see it.
            raise UnsupportedCaptureType(f"Enum({reference})", path)
        return {"$enum": reference}
    if obj is pd.NaT:
        return {"$nat": True}
    if isinstance(obj, pd.Timestamp):
        return {"$timestamp": _datetime_payload(obj)}
    if isinstance(obj, datetime):
        return {"$datetime": _datetime_payload(obj)}
    if isinstance(obj, date):
        return {"$date": obj.isoformat()}
    if isinstance(obj, Decimal):
        return {"$decimal": str(obj)}
    if isinstance(obj, np.generic):
        return {"$np": _numpy_payload(obj, path, sides, seen)}
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if obj != obj:
            return {"$float": "nan"}
        if obj == float("inf"):
            return {"$float": "inf"}
        if obj == float("-inf"):
            return {"$float": "-inf"}
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return [_encode_value(item, f"{path}[{i}]", sides, seen) for i, item in enumerate(obj)]
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise UnsupportedCaptureType(type(key).__name__, path)
            out[_escape_key(key)] = _encode_value(value, f'{path}["{key}"]', sides, seen)
        return out
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        # The value objects the platform passes around -- Recommendation above
        # all -- are dataclasses, and the OUTPUT of every recorded analysis is
        # one. Encoding it as a named record (rather than as an anonymous dict)
        # is what lets a replay rebuild the real object and compare it field by
        # field. Its fields go through this same encoder, so an unsupported value
        # INSIDE one is still refused, with its path.
        cls = type(obj)
        reference = f"{cls.__module__}.{cls.__qualname__}"
        if not _enum_module_allowed(cls.__module__):
            raise UnsupportedCaptureType(f"dataclass({reference})", path)
        for name, spec in getattr(cls, "__dataclass_fields__", {}).items():
            # An InitVar is required by __init__ but is NOT listed by fields(), so
            # a record written without it could never be replayed.
            if str(getattr(spec, "_field_type", "")).endswith("INITVAR"):
                raise UnsupportedCaptureType(
                    f"dataclass({reference}) with InitVar field {name!r}", path)
        if any(not field.init for field in dataclasses.fields(obj)):
            # A non-init field cannot be handed back to the constructor, so a
            # replay would silently rebuild a DIFFERENT object. Refuse it here,
            # where the gap is counted, rather than drop it quietly.
            raise UnsupportedCaptureType(
                f"dataclass({reference}) with non-init field(s)", path)
        return {"$dataclass": {
            "type": reference,
            "fields": {
                field.name: _encode_value(
                    getattr(obj, field.name), f'{path}.{field.name}', sides, seen)
                for field in dataclasses.fields(obj)
            },
        }}
    if isinstance(obj, (pd.DataFrame, pd.Series)):
        frame = _encode_frame(obj, path)
        frame_hash = content_hash(frame.kind, frame.data)
        if frame_hash not in seen:
            seen[frame_hash] = None
            sides.append(frame)
        return {"$frame": {"hash": frame_hash, "pandas_type": frame.meta["pandas_type"]}}
    raise UnsupportedCaptureType(type(obj).__name__, path)


def _enum_module_allowed(module_name: str) -> bool:
    return any(
        module_name == prefix.rstrip(".") or module_name.startswith(prefix)
        for prefix in ENUM_MODULE_PREFIXES
    )


def _datetime_payload(value: datetime) -> Dict[str, Any]:
    tzinfo = value.tzinfo
    if tzinfo is None:
        return {"iso": value.isoformat(), "naive": True}
    offset = value.utcoffset()
    return {
        "iso": value.isoformat(),
        "naive": False,
        "tzname": str(getattr(value, "tz", None) or tzinfo),
        "utcoffset_seconds": None if offset is None else int(offset.total_seconds()),
    }


def _numpy_payload(
    obj: np.generic, path: str, sides: List[Encoded], seen: Dict[str, None]
) -> Dict[str, Any]:
    dtype = obj.dtype
    if dtype.kind in ("M", "m"):
        # datetime64 / timedelta64: store the raw ticks in the dtype's own unit.
        # `.item()` is lossy (ns returns a plain int, us returns a datetime) and
        # `str()` cannot be parsed back into a timedelta64 at all. NaT's sentinel
        # tick value round-trips as NaT.
        return {"dtype": dtype.str, "ticks": int(obj.astype("int64"))}
    return {"dtype": dtype.str, "value": _encode_value(obj.item(), path, sides, seen)}


def _strip_env_metadata(metadata: Optional[Mapping[bytes, bytes]]) -> Dict[bytes, bytes]:
    """Drop the writing environment's version stamps from pandas metadata.

    pyarrow writes ``creator`` (its own version) and ``pandas_version`` into the
    ``b"pandas"`` schema metadata. Keeping them would make identical frame
    content hash differently on another host or after an upgrade, defeating
    content addressing and the byte-exact replay comparison.
    """
    out: Dict[bytes, bytes] = dict(metadata or {})
    raw = out.get(b"pandas")
    if raw is None:
        return out
    pandas_meta = json.loads(raw.decode("utf-8"))
    for key in _ENV_METADATA_KEYS:
        pandas_meta.pop(key, None)
    out[b"pandas"] = json.dumps(
        pandas_meta, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return out


def _encode_frame(obj: Union[pd.DataFrame, pd.Series], path: str) -> Encoded:
    frame_meta: Dict[str, Any] = {
        "codec_version": CODEC_VERSION,
        "object_kind": KIND_ARROW,
    }
    if isinstance(obj, pd.Series):
        frame_meta["pandas_type"] = "series"
        frame_meta["series_name"] = _encode_value(obj.name, f"{path}.name", [], {})
        frame = obj.to_frame(name=_SERIES_COLUMN)
    else:
        frame_meta["pandas_type"] = "dataframe"
        frame = obj
    frame_meta["row_count"] = int(len(frame))
    frame_meta["columns"] = [
        _encode_value(column, f"{path}.columns", [], {}) for column in frame.columns
    ]
    try:
        table = pa.Table.from_pandas(frame, preserve_index=True)
    except Exception as exc:  # pyarrow refuses mixed/opaque object columns
        raise UnsupportedCaptureType(
            f"{type(obj).__name__}({exc.__class__.__name__}: {exc})", path
        ) from exc
    metadata = _strip_env_metadata(table.schema.metadata)
    metadata[_ARROW_META_KEY] = json.dumps(
        frame_meta, allow_nan=False, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    table = table.replace_schema_metadata(metadata)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return Encoded(kind=KIND_ARROW, data=sink.getvalue().to_pybytes(), meta=frame_meta)


# --------------------------------------------------------------------------- decode

FrameSource = Union[Mapping[str, Any], Callable[[str], Any], None]


def decode(
    kind: str,
    data: bytes,
    meta: Optional[Mapping[str, Any]] = None,
    *,
    frames: FrameSource = None,
) -> Any:
    """Inverse of :func:`encode`.

    ``frames`` resolves referenced frame objects: a mapping ``hash -> Encoded``
    (or ``(kind, data, meta)``), or a callable taking the hash. A reference that
    cannot be resolved raises ``KeyError`` -- never a silent ``None``.
    """
    if kind == KIND_ARROW:
        return _decode_frame(data, meta)
    if kind != KIND_JSON:
        raise ValueError(f"unknown object kind {kind!r}")
    body = json.loads(data.decode("utf-8"))
    version = body["codec_version"]
    if version != CODEC_VERSION:
        raise ValueError(f"codec_version {version} is not supported (expected {CODEC_VERSION})")
    return _decode_value(body["payload"], frames)


def _resolve_frame(frames: FrameSource, frame_hash: str) -> Tuple[bytes, Optional[Mapping[str, Any]]]:
    if frames is None:
        raise KeyError(f"no frame source to resolve object {frame_hash}")
    if callable(frames):
        resolved = frames(frame_hash)
    else:
        if frame_hash not in frames:
            raise KeyError(f"frame object {frame_hash} is missing from the bundle")
        resolved = frames[frame_hash]
    if isinstance(resolved, Encoded):
        return resolved.data, resolved.meta
    kind, data, frame_meta = resolved[0], resolved[1], resolved[2]
    if kind != KIND_ARROW:
        raise ValueError(f"frame object {frame_hash} has kind {kind!r}")
    return data, frame_meta


def _decode_value(node: Any, frames: FrameSource) -> Any:
    if isinstance(node, list):
        return [_decode_value(item, frames) for item in node]
    if not isinstance(node, dict):
        return node
    if len(node) == 1:
        tag = next(iter(node))
        value = node[tag]
        if tag in _TAGS:
            return _decode_tag(tag, value, frames)
        if tag.startswith("$") and not tag.startswith("$$"):
            raise ValueError(f"unknown capture tag {tag!r}")
    return {_unescape_key(key): _decode_value(item, frames) for key, item in node.items()}


def _decode_tag(tag: str, value: Any, frames: FrameSource) -> Any:
    if tag == "$enum":
        return _decode_enum(value)
    if tag == "$dataclass":
        return _decode_dataclass(value, frames)
    if tag == "$float":
        return float(value)
    if tag == "$nat":
        return pd.NaT
    if tag == "$timestamp":
        return _decode_timestamp(value)
    if tag == "$datetime":
        return _decode_datetime(value)
    if tag == "$date":
        return date.fromisoformat(value)
    if tag == "$decimal":
        return Decimal(value)
    if tag == "$np":
        return _decode_numpy(value, frames)
    data, frame_meta = _resolve_frame(frames, value["hash"])
    return _decode_frame(data, frame_meta)


def _decode_enum(reference: str) -> Enum:
    module_name, class_name, member = reference.rsplit(".", 2)
    if not _enum_module_allowed(module_name):
        raise UnsafeEnumReference(reference)
    cls = getattr(importlib.import_module(module_name), class_name)
    if not (isinstance(cls, type) and issubclass(cls, Enum)):
        raise UnsafeEnumReference(reference)
    return cls[member]


def _decode_dataclass(payload: Mapping[str, Any], frames: FrameSource) -> Any:
    """Rebuild a recorded dataclass, refusing anything outside the allowlist.

    Same rule as ``$enum``: the reference names a type, and only a type this
    platform owns may be imported and constructed. Anything else -- a name
    outside the allowlist, a name that is not a dataclass -- is refused rather
    than instantiated.
    """
    reference = payload["type"]
    module_name, _, class_name = reference.rpartition(".")
    if not module_name or not _enum_module_allowed(module_name):
        raise UnsafeEnumReference(reference)
    cls = getattr(importlib.import_module(module_name), class_name, None)
    if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
        raise UnsafeEnumReference(reference)
    # Validate the record against TODAY's class BEFORE constructing anything, so
    # drift is reported as drift (naming the field) instead of surfacing as a
    # TypeError from __init__ or as an object quietly missing a value.
    recorded = payload["fields"]
    current = {field.name: field for field in dataclasses.fields(cls)}
    for name in recorded:
        spec = current.get(name)
        if spec is None:
            raise CodecDrift(reference, name, "is no longer a field of this class")
        if not spec.init:
            raise CodecDrift(reference, name, "can no longer be passed to __init__")
    for name, spec in current.items():
        if name in recorded or not spec.init:
            continue
        # A field ADDED since the record was written is replayable when the class
        # can supply it itself; without a default there is nothing to supply.
        if (spec.default is dataclasses.MISSING
                and spec.default_factory is dataclasses.MISSING):
            raise CodecDrift(
                reference, name, "was added since this record and has no default")
    for name, spec in getattr(cls, "__dataclass_fields__", {}).items():
        if str(getattr(spec, "_field_type", "")).endswith("INITVAR"):
            raise CodecDrift(reference, name, "is an InitVar and cannot be replayed")

    fields = {name: _decode_value(item, frames) for name, item in recorded.items()}
    return cls(**fields)


def _restore_zone(value: datetime, payload: Mapping[str, Any]) -> datetime:
    """Re-attach the recorded zone NAME so decode -> re-encode is byte-stable.

    ``fromisoformat`` only recovers a fixed offset, which would re-encode as
    ``UTC-05:00`` instead of ``America/New_York``. A name zoneinfo cannot
    resolve leaves the instant and its offset untouched.
    """
    tzname = payload["tzname"]
    if not tzname or str(value.tzinfo) == tzname:
        return value
    try:
        zone = ZoneInfo(tzname)
    except Exception:
        return value
    return value.astimezone(zone)


def _decode_datetime(payload: Mapping[str, Any]) -> datetime:
    value = datetime.fromisoformat(payload["iso"])
    if payload["naive"]:
        return value
    return _restore_zone(value, payload)


def _decode_timestamp(payload: Mapping[str, Any]) -> pd.Timestamp:
    """Restore a pandas Timestamp, including its named zone when it had one."""
    stamp = pd.Timestamp(payload["iso"])
    if payload["naive"]:
        return stamp
    tzname = payload["tzname"]
    if not tzname or str(stamp.tz) == tzname:
        return stamp
    try:
        return stamp.tz_convert(tzname)
    except (TypeError, ValueError):
        return stamp


def _decode_numpy(payload: Mapping[str, Any], frames: FrameSource) -> np.generic:
    dtype = np.dtype(payload["dtype"])
    if dtype.kind in ("M", "m"):
        return np.array([payload["ticks"]], dtype="int64").astype(dtype)[0]
    value = _decode_value(payload["value"], frames)
    return np.array([value], dtype=dtype)[0]


def _decode_frame(data: bytes, meta: Optional[Mapping[str, Any]]) -> Union[pd.DataFrame, pd.Series]:
    reader = pa.ipc.open_stream(pa.BufferReader(data))
    table = reader.read_all()
    embedded = _arrow_meta(table.schema)
    version = embedded["codec_version"]
    if version != CODEC_VERSION:
        raise ValueError(f"codec_version {version} is not supported (expected {CODEC_VERSION})")
    if meta is not None:
        # The object's own metadata is authoritative -- it is what the content
        # hash covers. A caller-supplied meta that disagrees means the index and
        # the bytes have diverged, which is an error, not something to merge.
        for key, value in meta.items():
            if key in embedded and embedded[key] != value:
                raise ValueError(
                    f"frame meta mismatch for {key!r}: object says {embedded[key]!r}, "
                    f"caller says {value!r}"
                )
    frame = table.to_pandas()
    expected_rows = embedded["row_count"]
    if len(frame) != expected_rows:
        raise ValueError(
            f"row_count mismatch decoding frame: {len(frame)} rows, meta says {expected_rows}"
        )
    if embedded["pandas_type"] == "series":
        series = frame[_SERIES_COLUMN].copy()
        series.name = _decode_value(embedded["series_name"], None)
        return series
    return frame


def _arrow_meta(schema: "pa.Schema") -> Dict[str, Any]:
    metadata = schema.metadata or {}
    if _ARROW_META_KEY not in metadata:
        raise ValueError("arrow object is missing its ba2_replay codec metadata")
    return json.loads(metadata[_ARROW_META_KEY].decode("utf-8"))


def extract_meta(kind: str, data: bytes) -> Dict[str, Any]:
    """Recover an object's meta from its bytes alone (no sidecar file)."""
    if kind == KIND_ARROW:
        return _arrow_meta(pa.ipc.open_stream(pa.BufferReader(data)).schema)
    if kind == KIND_JSON:
        body = json.loads(data.decode("utf-8"))
        return {key: value for key, value in body.items() if key != "payload"}
    raise ValueError(f"unknown object kind {kind!r}")


def frame_refs(kind: str, data: bytes) -> Tuple[str, ...]:
    """The frame object hashes referenced by a JSON object, in document order."""
    if kind != KIND_JSON:
        return ()
    found: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        if len(node) == 1 and "$frame" in node:
            frame_hash = node["$frame"]["hash"]
            if frame_hash not in found:
                found.append(frame_hash)
            return
        for item in node.values():
            _walk(item)

    _walk(json.loads(data.decode("utf-8"))["payload"])
    return tuple(found)


# --------------------------------------------------------------------------- freeze


def freeze(obj: Any) -> Any:
    """A deep, copy-based snapshot: later mutation by the expert cannot reach it.

    DataFrames/Series are deep-copied, containers are rebuilt, immutable scalars
    are shared, and a shared/cyclic reference is copied once (``memo``).

    LIMIT, deliberately: a DataFrame ``copy(deep=True)`` copies the blocks, not
    the Python objects an ``object``-dtype cell points at. A bundle that stores a
    mutable object INSIDE a frame cell is not isolated by this call -- and such a
    frame is refused by :func:`encode` anyway, which is where that gap is
    reported. Types the codec will later refuse are copied here regardless; the
    refusal (and the capture gap it records) belongs to :func:`encode`.
    """
    return _freeze(obj, {})


def _freeze(obj: Any, memo: Dict[int, Any]) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str, bytes, Decimal, Enum)):
        return obj
    if isinstance(obj, (datetime, date, np.generic)):
        return obj
    key = id(obj)
    if key in memo:
        return memo[key]
    if isinstance(obj, (pd.DataFrame, pd.Series, pd.Index)):
        copied: Any = obj.copy(deep=True)
    elif isinstance(obj, np.ndarray):
        copied = obj.copy()
    elif isinstance(obj, dict):
        copied = {}
        memo[key] = copied
        for name, value in obj.items():
            copied[name] = _freeze(value, memo)
    elif isinstance(obj, list):
        copied = []
        memo[key] = copied
        for item in obj:
            copied.append(_freeze(item, memo))
    elif isinstance(obj, tuple):
        copied = tuple(_freeze(item, memo) for item in obj)
    elif isinstance(obj, (set, frozenset)):
        copied = type(obj)(_freeze(item, memo) for item in obj)
    else:
        copied = copy.deepcopy(obj, memo)
    memo[key] = copied
    return copied
