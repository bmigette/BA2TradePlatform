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

Host-neutral: stdlib + pandas/numpy/pyarrow only.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

import numpy as np
import pandas as pd
import pyarrow as pa

CODEC_VERSION = 1

KIND_JSON = "json"
KIND_ARROW = "arrow"

#: Arrow schema-metadata key holding this codec's frame meta, so the object's
#: bytes alone fully determine its meaning (content addressing stays honest).
_ARROW_META_KEY = b"ba2_replay"

#: Sentinel column name for a Series stored as a one-column table.
_SERIES_COLUMN = "__ba2_replay_series__"

__all__ = [
    "CODEC_VERSION",
    "KIND_JSON",
    "KIND_ARROW",
    "Encoded",
    "UnsupportedCaptureType",
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
        """``{content_hash: Encoded}`` for the referenced frame objects."""
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


def _encode_value(obj: Any, path: str, sides: List[Encoded], seen: Dict[str, None]) -> Any:
    # Order matters: bool before int, Enum before str/int (str-Enums are str),
    # pandas NaT/Timestamp before datetime, numpy scalars before float/int.
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, Enum):
        cls = type(obj)
        return {"$enum": f"{cls.__module__}.{cls.__qualname__}.{obj.name}"}
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
            out[key] = _encode_value(value, f'{path}["{key}"]', sides, seen)
        return out
    if isinstance(obj, (pd.DataFrame, pd.Series)):
        frame = _encode_frame(obj, path)
        frame_hash = content_hash(frame.kind, frame.data)
        if frame_hash not in seen:
            seen[frame_hash] = None
            sides.append(frame)
        return {"$frame": {"hash": frame_hash, "pandas_type": frame.meta["pandas_type"]}}
    raise UnsupportedCaptureType(type(obj).__name__, path)


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
        # datetime64 / timedelta64: .item() loses the unit at ns precision.
        return {"dtype": dtype.str, "value": str(obj)}
    return {"dtype": dtype.str, "value": _encode_value(obj.item(), path, sides, seen)}


def _encode_frame(obj: Union[pd.DataFrame, pd.Series], path: str) -> Encoded:
    sides_meta: Dict[str, Any] = {
        "codec_version": CODEC_VERSION,
        "object_kind": KIND_ARROW,
    }
    if isinstance(obj, pd.Series):
        sides_meta["pandas_type"] = "series"
        sides_meta["series_name"] = _encode_value(obj.name, f"{path}.name", [], {})
        frame = obj.to_frame(name=_SERIES_COLUMN)
    else:
        sides_meta["pandas_type"] = "dataframe"
        frame = obj
    sides_meta["row_count"] = int(len(frame))
    sides_meta["columns"] = [
        _encode_value(column, f"{path}.columns", [], {}) for column in frame.columns
    ]
    try:
        table = pa.Table.from_pandas(frame, preserve_index=True)
    except Exception as exc:  # pyarrow refuses mixed/opaque object columns
        raise UnsupportedCaptureType(
            f"{type(obj).__name__}({exc.__class__.__name__}: {exc})", path
        ) from exc
    metadata = dict(table.schema.metadata or {})
    metadata[_ARROW_META_KEY] = json.dumps(
        sides_meta, allow_nan=False, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    table = table.replace_schema_metadata(metadata)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return Encoded(kind=KIND_ARROW, data=sink.getvalue().to_pybytes(), meta=sides_meta)


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
        if tag == "$enum":
            module_name, class_name, member = value.rsplit(".", 2)
            cls = getattr(importlib.import_module(module_name), class_name)
            return cls[member]
        if tag == "$float":
            return float(value)
        if tag == "$nat":
            return pd.NaT
        if tag == "$timestamp":
            return _decode_timestamp(value)
        if tag == "$datetime":
            return datetime.fromisoformat(value["iso"])
        if tag == "$date":
            return date.fromisoformat(value)
        if tag == "$decimal":
            return Decimal(value)
        if tag == "$np":
            return _decode_numpy(value, frames)
        if tag == "$frame":
            data, frame_meta = _resolve_frame(frames, value["hash"])
            return _decode_frame(data, frame_meta)
    return {key: _decode_value(item, frames) for key, item in node.items()}


def _decode_timestamp(payload: Mapping[str, Any]) -> pd.Timestamp:
    """Restore a pandas Timestamp, including its named zone when it had one.

    The ISO string already pins the instant and the UTC offset; ``tzname`` only
    restores the *label* (``America/New_York`` rather than ``UTC-05:00``). A zone
    name pandas cannot resolve leaves the instant and offset untouched.
    """
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
    value = _decode_value(payload["value"], frames)
    return np.array([value], dtype=dtype)[0]


def _decode_frame(data: bytes, meta: Optional[Mapping[str, Any]]) -> Union[pd.DataFrame, pd.Series]:
    reader = pa.ipc.open_stream(pa.BufferReader(data))
    table = reader.read_all()
    embedded = _arrow_meta(table.schema)
    effective = dict(embedded)
    if meta is not None:
        effective.update({k: v for k, v in meta.items() if k in embedded or k == "row_count"})
    frame = table.to_pandas()
    expected_rows = effective["row_count"]
    if len(frame) != expected_rows:
        raise ValueError(
            f"row_count mismatch decoding frame: {len(frame)} rows, meta says {expected_rows}"
        )
    if effective["pandas_type"] == "series":
        series = frame[_SERIES_COLUMN].copy()
        series.name = _decode_value(effective["series_name"], None)
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
    are shared. Types the codec will later refuse are copied here anyway -- the
    refusal (and the capture gap it records) belongs to :func:`encode`.
    """
    if obj is None or isinstance(obj, (bool, int, float, str, bytes, Decimal, Enum)):
        return obj
    if isinstance(obj, (datetime, date, np.generic)):
        return obj
    if isinstance(obj, (pd.DataFrame, pd.Series, pd.Index)):
        return obj.copy(deep=True)
    if isinstance(obj, np.ndarray):
        return obj.copy()
    if isinstance(obj, dict):
        return {key: freeze(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [freeze(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(freeze(item) for item in obj)
    if isinstance(obj, (set, frozenset)):
        return type(obj)(freeze(item) for item in obj)
    import copy

    return copy.deepcopy(obj)
