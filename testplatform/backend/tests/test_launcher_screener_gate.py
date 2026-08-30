"""Gate-only screener mode wiring (options grid max-stock-price, 2026-07-29).

Asserts the WIRING, not the mechanism (the metric store / engine gate have their own tests):
the launcher's screener_opt block carries gate_only + the configured price cap, honors the
precedence chain, and refuses to combine with full --screener mode.
"""
import sys, os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "testplatform"))

import pytest

import ba2test_launcher as L


def _args(**over):
    base = dict(
        screener_gate_store="/tmp/store.parquet",
        max_stock_price=100.0,
        screener=False,
        screener_base_json=None,
        screener_cadence_days=7,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_no_flag_no_block():
    assert L._screener_gate_opt_block(_args(screener_gate_store=None), "O_IC") is None


def test_block_is_gate_only_with_default_cap():
    blk = L._screener_gate_opt_block(_args(), "O_IC")
    assert blk["gate_only"] is True
    assert blk["apply_to_expert_settings"] is False
    assert blk["store"] == "/tmp/store.parquet"
    assert blk["cadence_days"] == 7
    # Default: everything most-admitting except the $100 price cap.
    assert blk["base_settings"]["price_max"] == 100.0
    assert blk["base_settings"]["market_cap_min"] == 0.0
    assert blk["base_settings"]["relative_volume_min"] == 0.0
    assert blk["base_settings"]["price_drop_pct"] == 0.0


def test_max_stock_price_configurable_and_zero_disables():
    blk = L._screener_gate_opt_block(_args(max_stock_price=60.0), "O_IC")
    assert blk["base_settings"]["price_max"] == 60.0
    blk0 = L._screener_gate_opt_block(_args(max_stock_price=0.0), "O_IC")
    assert "price_max" not in blk0["base_settings"]


def test_per_strategy_override_wins(monkeypatch):
    monkeypatch.setitem(L._OPTION_STRATS["O_CSP"], "screener_gate_base", {"price_max": 40.0})
    blk = L._screener_gate_opt_block(_args(), "O_CSP")
    assert blk["base_settings"]["price_max"] == 40.0
    # A strategy without an override keeps the CLI value.
    assert L._screener_gate_opt_block(_args(), "O_IC")["base_settings"]["price_max"] == 100.0


def test_group_merges_active_member_overrides(monkeypatch):
    # _OPTION_GROUPS (post affordability filter) trims OS2 to [O_SSTG, O_SSTD, O_IC] --
    # O_CSP is a full-notional kind and excluded from the DEFAULT grouped search (it only runs
    # as an explicit single-strategy job). O_SSTG/O_SSTD now carry REAL $300 caps
    # (F4, 2026-08-30), so overriding the FIRST member alone no longer proves the merge -- a
    # later real value would still win. Override the LAST live member (O_IC, uncapped by
    # default) to prove the merge order against real neighbors too.
    assert L._OPTION_GROUPS["OS2"] == ["O_SSTG", "O_SSTD", "O_IC"]
    monkeypatch.setitem(L._OPTION_STRATS["O_IC"], "screener_gate_base", {"price_max": 55.0})
    assert L._screener_gate_base_for_strategy("OS2")["price_max"] == 55.0


def test_base_json_beats_cli_default_and_loses_to_strategy(tmp_path, monkeypatch):
    import json
    p = tmp_path / "base.json"
    p.write_text(json.dumps({"price_max": 77.0, "relative_volume_min": 1.5}))
    blk = L._screener_gate_opt_block(_args(screener_base_json=str(p)), "O_IC")
    assert blk["base_settings"]["price_max"] == 77.0
    assert blk["base_settings"]["relative_volume_min"] == 1.5
    monkeypatch.setitem(L._OPTION_STRATS["O_IC"], "screener_gate_base", {"price_max": 33.0})
    blk2 = L._screener_gate_opt_block(_args(screener_base_json=str(p)), "O_IC")
    assert blk2["base_settings"]["price_max"] == 33.0


def test_combining_with_full_screener_mode_is_a_hard_error():
    with pytest.raises(SystemExit):
        L._screener_gate_opt_block(_args(screener=True, screener_store="/tmp/x"), "O_IC")


def test_equity_and_unknown_keys_have_no_strategy_override():
    assert L._screener_gate_base_for_strategy("O_STK") == {}
    assert L._screener_gate_base_for_strategy("S1") == {}


# --------------------------------------------------------------------------------------------
# F4 (option-program-review-findings.md, 2026-08-30): stage 1's universe caps, wired as REAL
# _OPTION_STRATS[].screener_gate_base entries (not just the monkeypatched examples above) --
# the "depending on strategy" knob the 2026-07-29 design built and stage1_run.sh never used.
# Grid design §6: the full-notional three (assignment-notional reserve) are unreachable above
# spot ~$100 at $20k even with the per-instrument cap gene raised to 50%; the two naked-vol
# structures (Reg-T bracket, not notional) are unreachable above spot ~$300. Every other
# structure is defined-risk (reserve is a function of wing width, not spot) and stays uncapped.
# --------------------------------------------------------------------------------------------
def test_full_notional_structures_are_capped_at_100():
    for kind in ("O_CSP", "O_JL", "O_RS"):
        assert L._OPTION_STRATS[kind]["screener_gate_base"] == {"price_max": 100.0}, kind


def test_naked_vol_structures_are_capped_at_300():
    for kind in ("O_SSTD", "O_SSTG"):
        assert L._OPTION_STRATS[kind]["screener_gate_base"] == {"price_max": 300.0}, kind


def test_full_notional_set_matches_the_named_constant():
    # The cap targets exactly the kinds the reserve-table comment above _FULL_NOTIONAL_OPTION_KINDS
    # already names as unaffordable full-notional structures -- not a second, drifting list.
    assert {"O_CSP", "O_JL", "O_RS"} == L._FULL_NOTIONAL_OPTION_KINDS


def test_defined_risk_structures_stay_uncapped():
    for kind in ("O_IC", "O_BF", "O_VERT", "O_BULLCS", "O_BEARCS", "O_BULLPS", "O_LC", "O_LP",
                 "O_STRD", "O_STRG"):
        assert L._OPTION_STRATS[kind].get("screener_gate_base") is None, kind


def test_real_caps_flow_through_the_gate_block_with_the_cli_default_disabled():
    """The launcher-level wiring stage1_run.sh actually exercises: --max-stock-price 0 disables
    the blanket default so only the five capped structures see a price_max at all."""
    args = _args(max_stock_price=0.0)
    assert L._screener_gate_opt_block(args, "O_CSP")["base_settings"]["price_max"] == 100.0
    assert L._screener_gate_opt_block(args, "O_SSTG")["base_settings"]["price_max"] == 300.0
    assert "price_max" not in L._screener_gate_opt_block(args, "O_IC")["base_settings"]
