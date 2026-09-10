"""Task 1 (spec step 1): the versioned exact capture codec.

Every value that can enter a normalized expert bundle must survive encode ->
decode byte-exactly, with NaN / None / 0 kept distinct, enums kept as enums,
timezone metadata kept, and DataFrames stored as SEPARATE Arrow IPC objects that
preserve index, column order, dtypes and tz. Anything the codec cannot represent
must be refused loudly (a capture gap), never silently coerced.
"""
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.replay.codec import (
    CODEC_VERSION,
    UnsafeEnumReference,
    UnsupportedCaptureType,
    content_hash,
    decode,
    encode,
    freeze,
)
from ba2_common.core.types import OrderDirection, TimeInterval

NY = "America/New_York"


def _roundtrip(obj):
    enc = encode(obj)
    assert enc.meta["codec_version"] == CODEC_VERSION
    return decode(enc.kind, enc.data, enc.meta, frames=enc.frame_map())


# --------------------------------------------------------------------------- scalars


def test_roundtrip_json_scalars_and_containers():
    obj = {
        "none": None,
        "true": True,
        "false": False,
        "int": 7,
        "big_int": 2**70,
        "float": 1.2345678901234567,
        "neg_zero": -0.0,
        "str": "hello é中",
        "list": [1, "a", None, 2.5],
        "nested": {"b": [{"c": 1}]},
    }
    back = _roundtrip(obj)
    assert back == obj
    assert isinstance(back["int"], int) and not isinstance(back["int"], bool)
    assert back["big_int"] == 2**70
    assert math.copysign(1.0, back["neg_zero"]) == -1.0


def test_dict_key_order_is_preserved():
    obj = {"z": 1, "a": 2, "m": 3}
    back = _roundtrip(obj)
    assert list(back.keys()) == ["z", "a", "m"]


def test_nan_inf_none_and_zero_stay_distinct():
    obj = {
        "nan": float("nan"),
        "inf": float("inf"),
        "ninf": float("-inf"),
        "none": None,
        "zero_int": 0,
        "zero_float": 0.0,
        "false": False,
    }
    back = _roundtrip(obj)
    assert math.isnan(back["nan"])
    assert back["inf"] == float("inf")
    assert back["ninf"] == float("-inf")
    assert back["none"] is None
    assert back["zero_int"] == 0 and isinstance(back["zero_int"], int)
    assert back["zero_float"] == 0.0 and isinstance(back["zero_float"], float)
    assert back["false"] is False
    # and they are not the same bytes
    hashes = {
        key: content_hash(*encode(value)[:2])
        for key, value in obj.items()
    }
    assert len(set(hashes.values())) == len(hashes)


def test_float_roundtrip_is_exact_repr():
    values = [0.1, 1 / 3, 1e-308, 1.7976931348623157e308, 123456.789012345]
    back = _roundtrip(values)
    assert back == values
    assert [repr(v) for v in back] == [repr(v) for v in values]


def test_enum_roundtrip_keeps_the_enum_member():
    obj = {"direction": OrderDirection.BUY, "interval": TimeInterval.D1}
    back = _roundtrip(obj)
    assert back["direction"] is OrderDirection.BUY
    assert back["interval"] is TimeInterval.D1
    # a str-Enum must NOT degrade to its str value
    assert type(back["direction"]) is OrderDirection


def test_datetime_date_and_decimal_roundtrip():
    aware_utc = datetime(2024, 3, 1, 14, 30, tzinfo=timezone.utc)
    aware_ny = pd.Timestamp("2024-03-01 09:30", tz=NY).to_pydatetime()
    naive = datetime(2024, 3, 1, 9, 30)
    obj = {
        "utc": aware_utc,
        "ny": aware_ny,
        "naive": naive,
        "date": date(2024, 3, 1),
        "dec": Decimal("123.4500"),
    }
    back = _roundtrip(obj)
    assert back["utc"] == aware_utc and back["utc"].tzinfo is not None
    assert back["ny"] == aware_ny
    assert back["ny"].utcoffset() == aware_ny.utcoffset()
    assert back["naive"] == naive and back["naive"].tzinfo is None
    assert back["date"] == date(2024, 3, 1) and not isinstance(back["date"], datetime)
    assert back["dec"] == Decimal("123.4500")
    assert str(back["dec"]) == "123.4500"


def test_pandas_timestamp_and_nat_keep_their_type():
    ts = pd.Timestamp("2024-03-01 09:30", tz=NY)
    back = _roundtrip({"ts": ts, "nat": pd.NaT})
    assert isinstance(back["ts"], pd.Timestamp)
    assert back["ts"] == ts
    assert str(back["ts"].tz) == NY
    assert back["nat"] is pd.NaT


def test_numpy_scalars_roundtrip_with_dtype():
    obj = {
        "i64": np.int64(7),
        "f64": np.float64(1.5),
        "f32": np.float32(1.5),
        "b": np.bool_(True),
        "nan": np.float64("nan"),
        "dt": np.datetime64("2024-03-01T14:30:00", "ns"),
    }
    back = _roundtrip(obj)
    for key in ("i64", "f64", "f32", "b", "dt"):
        assert back[key] == obj[key], key
        assert back[key].dtype == obj[key].dtype, key
    assert np.isnan(back["nan"]) and back["nan"].dtype == np.dtype("float64")


# --------------------------------------------------------------------------- frames


def _wide_frame():
    idx = pd.DatetimeIndex(
        ["2024-03-01 09:30", "2024-03-01 09:30", "2024-03-04 09:30"],
        tz=NY,
        name="ts",
    )
    return pd.DataFrame(
        {
            "i": np.array([1, 2, 3], dtype="int64"),
            "f": [1.5, float("nan"), 3.25],
            "s": ["a", "b", "c"],
            "d": pd.to_datetime(["2024-03-01", "2024-03-02", "2024-03-03"]).tz_localize("UTC"),
            "c": pd.Categorical(["x", "y", "x"], categories=["y", "x"], ordered=True),
        },
        index=idx,
    )


def test_dataframe_roundtrip_preserves_index_columns_dtypes_and_tz():
    df = _wide_frame()
    back = _roundtrip(df)
    pd.testing.assert_frame_equal(back, df)
    assert list(back.columns) == list(df.columns)
    assert str(back.index.tz) == NY
    assert back.index.name == "ts"
    assert not back.index.is_unique


def test_dataframe_is_stored_as_a_separate_arrow_object():
    df = _wide_frame()
    enc = encode({"symbol": "AAPL", "ohlcv": df})
    assert enc.kind == "json"
    assert len(enc.sides) == 1
    side = enc.sides[0]
    assert side.kind == "arrow"
    assert side.meta["row_count"] == len(df)
    # the JSON body only references the frame by content hash
    body = enc.data.decode("utf-8")
    assert content_hash(side.kind, side.data) in body
    assert "AAPL" in body
    back = decode(enc.kind, enc.data, enc.meta, frames=enc.frame_map())
    pd.testing.assert_frame_equal(back["ohlcv"], df)


def test_top_level_dataframe_encodes_as_an_arrow_object():
    df = _wide_frame()
    enc = encode(df)
    assert enc.kind == "arrow"
    assert enc.sides == ()
    pd.testing.assert_frame_equal(decode(enc.kind, enc.data, enc.meta), df)


def test_empty_dataframe_keeps_columns_and_dtypes():
    df = pd.DataFrame({"a": pd.Series(dtype="float64"), "b": pd.Series(dtype="object")})
    back = _roundtrip(df)
    pd.testing.assert_frame_equal(back, df)
    assert len(back) == 0


def test_series_roundtrip_named_and_unnamed():
    named = pd.Series([1.5, 2.5], index=pd.Index(["a", "b"], name="sym"), name="px")
    unnamed = pd.Series([1, 2, 2], index=[0, 0, 1], dtype="int64")
    back_named = _roundtrip(named)
    back_unnamed = _roundtrip(unnamed)
    pd.testing.assert_series_equal(back_named, named)
    pd.testing.assert_series_equal(back_unnamed, unnamed)
    assert back_unnamed.name is None


def test_row_count_mismatch_is_detected_on_decode():
    df = _wide_frame()
    enc = encode(df)
    tampered_meta = dict(enc.meta)
    tampered_meta["row_count"] = len(df) + 1
    with pytest.raises(ValueError, match="row_count"):
        decode(enc.kind, enc.data, tampered_meta)


def test_missing_frame_reference_is_an_error_not_a_none():
    enc = encode({"ohlcv": _wide_frame()})
    with pytest.raises(KeyError):
        decode(enc.kind, enc.data, enc.meta, frames={})


# --------------------------------------------------------------------------- refusals


class _Custom:
    pass


def test_unsupported_type_raises_with_the_path():
    with pytest.raises(UnsupportedCaptureType) as excinfo:
        encode({"bundle": {"rows": [1, _Custom()]}})
    assert excinfo.value.type_name == "_Custom"
    assert excinfo.value.path == '$["bundle"]["rows"][1]'
    assert "_Custom" in str(excinfo.value)


def test_unsupported_dict_key_raises_with_the_path():
    with pytest.raises(UnsupportedCaptureType) as excinfo:
        encode({"a": {3: "x"}})
    assert excinfo.value.path == '$["a"]'


def test_timedelta_is_refused_rather_than_coerced():
    with pytest.raises(UnsupportedCaptureType):
        encode({"age": timedelta(days=3)})


# --------------------------------------------------------------------------- freeze


def test_freeze_isolates_the_snapshot_from_later_mutation():
    df = _wide_frame()
    bundle = {"symbol": "AAPL", "ohlcv": df, "rows": [{"eps": 1.0}]}
    snapshot = freeze(bundle)

    bundle["symbol"] = "MSFT"
    bundle["rows"][0]["eps"] = 99.0
    bundle["rows"].append({"eps": 2.0})
    df.iloc[0, df.columns.get_loc("i")] = 999
    df["new"] = 1

    assert snapshot["symbol"] == "AAPL"
    assert snapshot["rows"] == [{"eps": 1.0}]
    assert list(snapshot["ohlcv"].columns) == ["i", "f", "s", "d", "c"]
    assert snapshot["ohlcv"]["i"].iloc[0] == 1


def test_freeze_result_encodes_identically_to_the_original():
    bundle = {"symbol": "AAPL", "ohlcv": _wide_frame()}
    before = encode(bundle)
    after = encode(freeze(bundle))
    assert content_hash(before.kind, before.data) == content_hash(after.kind, after.data)


# --------------------------------------------------------------------------- hashing


def test_content_hash_is_stable_and_content_addressed():
    obj = {"a": [1, 2.5, "x"]}
    first = encode(obj)
    second = encode({"a": [1, 2.5, "x"]})
    assert first.data == second.data
    assert content_hash(first.kind, first.data) == content_hash(second.kind, second.data)
    assert content_hash("json", b"{}") != content_hash("arrow", b"{}")
    assert len(content_hash(first.kind, first.data)) == 64


# --------------------------------------------------------------------------- tag safety


def test_user_dict_keys_that_look_like_tags_round_trip_as_plain_dicts():
    """A provider payload may legitimately contain a key named like a codec tag."""
    payloads = [
        {"$decimal": "1.5"},
        {"$float": "nan"},
        {"$date": "2024-01-01"},
        {"$enum": "os.system"},
        {"$frame": {"hash": "deadbeef"}},
        {"$np": {"dtype": "int64", "value": 1}},
        {"$$decimal": 1},
        {"$decimal": "1.5", "other": 2},
        {"nested": [{"$enum": "os.system"}]},
    ]
    for payload in payloads:
        back = _roundtrip(payload)
        assert back == payload, payload
        assert type(back) is dict


def test_escaped_keys_do_not_collide_across_levels():
    obj = {"$$$x": 1, "$x": 2, "x": 3}
    back = _roundtrip(obj)
    assert back == obj
    assert list(back.keys()) == ["$$$x", "$x", "x"]


def _raw_json_object(payload: dict) -> bytes:
    import json as _json

    return _json.dumps(
        {"codec_version": CODEC_VERSION, "object_kind": "json", "payload": payload}
    ).encode("utf-8")


def test_an_enum_payload_cannot_import_an_arbitrary_module():
    data = _raw_json_object({"$enum": "os.path.join"})
    with pytest.raises(UnsafeEnumReference) as excinfo:
        decode("json", data, {"codec_version": CODEC_VERSION})
    assert "os.path" in str(excinfo.value)


def test_capturing_an_enum_outside_the_allowlist_is_a_capture_gap():
    import enum as _enum

    Outside = _enum.Enum("Outside", ["A"])  # __module__ is this test module
    with pytest.raises(UnsupportedCaptureType) as excinfo:
        encode({"e": Outside.A})
    assert excinfo.value.path == '$["e"]'
    assert "Enum(" in excinfo.value.type_name


def test_an_unknown_tag_in_stored_bytes_is_refused():
    data = _raw_json_object({"$whatever": 1})
    with pytest.raises(ValueError, match="unknown capture tag"):
        decode("json", data, {"codec_version": CODEC_VERSION})


# --------------------------------------------------------------------------- numpy time


@pytest.mark.parametrize("unit", ["D", "s", "ms", "us", "ns"])
def test_numpy_datetime64_and_timedelta64_round_trip_in_every_unit(unit):
    stamp = np.datetime64("2024-03-01T14:30:00", unit) if unit != "D" else np.datetime64("2024-03-01", "D")
    delta = np.timedelta64(7, unit)
    back = _roundtrip({"t": stamp, "d": delta})
    assert back["t"] == stamp and back["t"].dtype == stamp.dtype
    assert back["d"] == delta and back["d"].dtype == delta.dtype


def test_numpy_nat_round_trips_as_nat():
    value = np.datetime64("NaT", "ns")
    back = _roundtrip({"t": value})
    assert np.isnat(back["t"])
    assert back["t"].dtype == value.dtype


# --------------------------------------------------------------------------- stability


def test_frame_hashes_do_not_depend_on_the_pyarrow_or_pandas_version():
    import json as _json

    from ba2_common.core.replay.codec import _strip_env_metadata

    enc = encode(_wide_frame())
    assert b'"creator"' not in enc.data
    assert b'"pandas_version"' not in enc.data

    base = _json.loads(
        _json.dumps({"columns": [], "index_columns": [], "creator": {}, "pandas_version": ""})
    )
    first = dict(base, creator={"library": "pyarrow", "version": "24.0.0"}, pandas_version="2.3.3")
    second = dict(base, creator={"library": "pyarrow", "version": "9.9.9"}, pandas_version="1.0.0")
    stripped = [
        _strip_env_metadata({b"pandas": _json.dumps(meta).encode("utf-8")})[b"pandas"]
        for meta in (first, second)
    ]
    assert stripped[0] == stripped[1]


def test_datetime_decode_restores_the_zone_name_so_re_encoding_is_byte_stable():
    values = {
        "ny": pd.Timestamp("2024-03-01 09:30", tz=NY).to_pydatetime(),
        "utc": datetime(2024, 3, 1, 14, 30, tzinfo=timezone.utc),
        "naive": datetime(2024, 3, 1, 9, 30),
    }
    first = encode(values)
    back = decode(first.kind, first.data, first.meta)
    assert str(back["ny"].tzinfo) == NY
    second = encode(back)
    assert second.data == first.data


def test_arrow_objects_carry_and_check_their_codec_version():
    enc = encode(_wide_frame())
    from ba2_common.core.replay.codec import extract_meta

    assert extract_meta(enc.kind, enc.data)["codec_version"] == CODEC_VERSION


def test_caller_meta_that_contradicts_the_object_is_an_error():
    enc = encode(_wide_frame())
    with pytest.raises(ValueError, match="pandas_type"):
        decode(enc.kind, enc.data, {**enc.meta, "pandas_type": "series"})


def test_freeze_handles_shared_and_cyclic_references():
    shared = {"n": 1}
    cyclic: dict = {"shared_a": shared, "shared_b": shared}
    cyclic["self"] = cyclic
    snapshot = freeze(cyclic)
    assert snapshot["shared_a"] is snapshot["shared_b"]
    assert snapshot["self"] is snapshot
    shared["n"] = 2
    assert snapshot["shared_a"]["n"] == 1
