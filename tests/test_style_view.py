"""Pure style-summary builder (ba2_trade_platform.core.style_view); shapes copied from a deployed O_STYLE expert."""
from ba2_trade_platform.core.style_view import build_style_summary


def _leaf(field, op=None, value=None):
    d = {"event_type": field}
    if op is not None:
        d["operator"] = op
    if value is not None:
        d["value"] = value
    return d


def _trig(*leaves):
    return {f"cond_{i}": leaf for i, leaf in enumerate(leaves)}


STRD = {"action_type": "open_straddle", "strike_method": "percent_otm", "strike_param": 0.0,
        "dte_min": 40, "dte_max": 60, "sizing": 8.0, "min_volume": 25, "entry_cross": 0.9,
        "entry_tag": 12}
ENTER = [
    {"name": "O_STRD-entry-hold", "continue_processing": False,
     "triggers": _trig(_leaf("current_rating_neutral"), _leaf("has_no_position"),
                       _leaf("iv_rank", "<", 40.0),
                       _leaf("underlying_trend_slope_50_atr14", ">", -0.15000000000000002),
                       _leaf("channel_pos_20", "<", 0.7000000000000001)),
     "actions": {"a0": STRD}},
    {"name": "O_STRD-entry-low_confidence", "continue_processing": False,
     "triggers": _trig(_leaf("confidence", "<=", 15.0), _leaf("iv_rank", "<", 40.0)),
     "actions": {"a0": dict(STRD, dte_min=45, dte_max=65, entry_cross=0.8)}},
    {"name": "O_LC-entry", "continue_processing": False,
     "triggers": _trig(_leaf("rec_direction", ">", 0.0)),
     "actions": {"a0": {"action_type": "buy_call", "option_entry_tag": 13, "dte_min": 90,
                        "dte_max": 120, "strike_method": "delta", "strike_param": 0.6}}},
]
OPEN = [
    {"name": "x0", "continue_processing": False, "actions": {"a0": {"action_type": "close_option"}},
     "triggers": _trig(_leaf("profit_loss_percent", ">", 100.0), _leaf("entry_tag", "==", 13.0))},
    {"name": "x1", "continue_processing": False, "actions": {"a0": {"action_type": "close_option"}},
     "triggers": _trig(_leaf("days_opened", ">", 30.0), _leaf("entry_tag", "==", 12.0))},
    {"name": "x2", "continue_processing": False, "actions": {"a0": {"action_type": "close_option"}},
     "triggers": _trig(_leaf("days_to_expiry", "<=", 7.0))},
    {"name": "x3", "continue_processing": False, "actions": {"a0": {"action_type": "close_option"}},
     "triggers": _trig(_leaf("days_opened", ">", 30.0), _leaf("entry_tag", "==", 99.0))},
]


def test_members_in_priority_order_with_grouped_gates():
    s = build_style_summary(ENTER, OPEN, {"id": 12})
    assert s["header"] == {"id": 12}
    assert [(m["rank"], m["structure"], m["tag"]) for m in s["members"]] == [
        (1, "O_STRD", 12), (2, "O_LC", 13)]
    strd = s["members"][0]
    assert [e["precedence"] for e in strd["entries"]] == [1, 2]
    gates = strd["entries"][0]["gates"]
    assert gates["signal"] == ["current_rating_neutral", "has_no_position"]
    assert gates["iv"] == ["iv_rank < 40"]
    assert gates["market"] == ["underlying_trend_slope_50_atr14 > -0.15", "channel_pos_20 < 0.7"]
    assert s["members"][1]["entries"][0]["precedence"] == 3
    assert strd["entries"][1]["gates"]["signal"] == ["confidence <= 15"]


def test_action_params_readable():
    params = {p["label"]: p["value"] for p in build_style_summary(ENTER, OPEN)["members"][0]["entries"][1]["params"]}
    assert params["structure"] == "open_straddle"
    assert params["DTE"] == "45-65"
    assert params["strike"] == "percent_otm 0"
    assert params["sizing %"] == "8"
    assert params["entry cross"] == "0.8"
    assert params["min volume"] == "25"


def test_option_prefixed_param_keys_and_bare_tag():
    rule = {"name": "O_BF-entry", "continue_processing": False, "triggers": {},
            "actions": {"a0": {"action_type": "open_butterfly", "option_entry_tag": 4,
                               "option_dte_min": 30, "option_dte_max": 45,
                               "option_strike_method": "delta", "option_strike_param": 0.3}}}
    m = build_style_summary([rule], [])["members"][0]
    labels = {p["label"]: p["value"] for p in m["entries"][0]["params"]}
    assert (m["tag"], m["structure"]) == (4, "O_BF")
    assert labels["DTE"] == "30-45" and labels["strike"] == "delta 0.3"


def test_exits_scoped_unscoped_and_orphan():
    s = build_style_summary(ENTER, OPEN)
    by_tag = {m["tag"]: [e["name"] for e in m["exits"]] for m in s["members"]}
    assert by_tag == {12: ["x1"], 13: ["x0"]}
    assert [e["name"] for e in s["all_member_exits"]] == ["x2"]
    assert [e["name"] for e in s["orphan_exits"]] == ["x3"]
    x0 = s["members"][1]["exits"][0]
    assert x0["gates"] == ["profit_loss_percent > 100"]  # entry_tag leaf scopes, not shown as a gate
    assert x0["actions"] == ["close_option"]


def test_non_style_instance_is_none():
    plain = [{"name": "buy", "continue_processing": False, "triggers": {},
              "actions": {"a0": {"action_type": "buy"}}}]
    assert build_style_summary(plain, OPEN) is None
    assert build_style_summary([], []) is None


def test_unknown_leaf_shape_is_shown_not_dropped():
    rule = dict(ENTER[0], triggers={"cond_0": {"weird": 1}})
    s = build_style_summary([rule], [])
    assert s["members"][0]["entries"][0]["unreadable"] == ["cond_0: {'weird': 1}"]
