from types import SimpleNamespace as R

from ba2_common.core.interfaces.ExtendableSettingsInterface import decode_setting_rows
from ba2_common.core.interfaces.MarketExpertInterface import enabled_instruments_config

DEFS = {"flag": {"type": "bool"}, "n": {"type": "int"}, "x": {"type": "float"},
        "s": {"type": "str"}, "j": {"type": "json"}, "unset": {"type": "str"}}


def _r(key, value_str=None, value_json=None, value_float=None):
    return R(key=key, value_str=value_str, value_json=value_json, value_float=value_float)


def test_typed_decoding_and_legacy_spellings():
    out = decode_setting_rows([
        _r("flag", value_json="1"), _r("n", value_float=5.0), _r("x", value_float=0.5),
        _r("s", value_str="None"), _r("j", value_json={"a": 1}),
    ], DEFS)
    assert out == {"flag": True, "n": 5, "x": 0.5, "s": None, "j": {"a": 1}, "unset": None}


def test_legacy_int_in_value_str_and_unreadable_bool():
    out = decode_setting_rows([_r("n", value_str="7"), _r("flag", value_json="maybe")], DEFS)
    assert out["n"] == 7 and out["flag"] is False


def test_undefined_keys_infer_type_from_storage():
    out = decode_setting_rows([_r("extra_j", value_json=[1]), _r("extra_f", value_float=2.0),
                               _r("extra_s", value_str="hi")], {})
    assert out == {"extra_j": [1], "extra_f": 2.0, "extra_s": "hi"}


def test_enabled_instruments_config():
    assert enabled_instruments_config({"enabled_instruments": {"AAPL": {}}}) == {"AAPL": {}}
    assert enabled_instruments_config({"enabled_instruments": '{"MSFT": {"w": 1}}'}) == {"MSFT": {"w": 1}}
    assert enabled_instruments_config({"enabled_instruments": "not json"}) == {}
    assert enabled_instruments_config({}) == {}
