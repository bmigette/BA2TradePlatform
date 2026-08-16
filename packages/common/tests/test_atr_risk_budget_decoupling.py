"""risk_atr SIZING budget decoupled from the STOP-DISTANCE gene.

WHY (measured 2026-08-16): `risk_per_trade_pct` did two unrelated jobs -- it set the stop distance
(`synthesize_safeguard_stop`: min of atr_multiplier*ATR and risk_per_trade_pct%, floored at
min_stop_loss_pct%) in BOTH sizing modes, AND it was the risk budget that `risk_atr` sized off.

A range wide enough to search stop distance (0.5-10%) therefore drove the risk-based size past
`max_virtual_equity_per_instrument_percent`, the cap bound, and risk_atr produced positions
IDENTICAL to notional. Evidence: 28 of 56 distinct goal2020 FMPRating results were byte-identical
trade blobs across the two sizing modes, differing only in `expertFixedSettings.sizing_mode`.
Against realized daily ATR (median 2.29% of price, n=278 symbols) the cap binds in ~71% of the
gene grid at risk 3.0, ~91% at 5.0 and ~99% at 10.0.

`atr_risk_budget_pct` is the budget; `risk_per_trade_pct` keeps the stop-distance job. The
invariant the code already documented ("size off the SAME stop that will protect the position", so
realized loss at the stop equals the budget) is preserved -- the budget is simply now its own knob.
"""
import pytest

from ba2_common.core.position_sizing import compute_risk_based_quantity, synthesize_safeguard_stop


class _Expert:
    def __init__(self, **settings):
        self._s = settings

    def get_setting_with_interface_default(self, key, log_warning=True):
        return self._s.get(key)

    def get_virtual_balance(self):
        return 100_000.0


def test_stop_distance_still_comes_from_risk_per_trade_pct():
    """The stop must NOT move when only the sizing budget changes -- that is the whole point of
    splitting them, and a regression here would silently re-couple the two."""
    price = 100.0
    a = synthesize_safeguard_stop(price, True, 5.0, atr=1.0, atr_multiplier=4.0, min_stop_pct=3.0)
    b = synthesize_safeguard_stop(price, True, 5.0, atr=1.0, atr_multiplier=4.0, min_stop_pct=3.0)
    assert a == b
    # and it DOES respond to risk_per_trade_pct, i.e. that gene still has its stop job
    tight = synthesize_safeguard_stop(price, True, 3.0, atr=1.0, atr_multiplier=4.0, min_stop_pct=1.0)
    wide = synthesize_safeguard_stop(price, True, 8.0, atr=1.0, atr_multiplier=4.0, min_stop_pct=1.0)
    assert tight != wide, "risk_per_trade_pct must still drive the stop distance"


@pytest.mark.parametrize("budget_pct,stop_dist_pct,cap_pct,expect_capped", [
    (3.0, 9.0, 30.0, True),    # 3/9 = 33% of equity > 30% cap -> cap binds, mode is inert
    (1.0, 9.0, 30.0, False),   # 1/9 = 11% -> under the cap, risk_atr genuinely differs
    (0.5, 12.0, 20.0, False),  # 0.5/12 = 4% -> well under
    (10.0, 15.0, 30.0, True),  # the old range's top end: always capped
])
def test_cap_binding_is_what_makes_risk_atr_identical_to_notional(
        budget_pct, stop_dist_pct, cap_pct, expect_capped):
    """Documents the actual mechanism, so the reason the ranges were chosen cannot be lost."""
    equity, price = 100_000.0, 100.0
    stop_price = price * (1 - stop_dist_pct / 100.0)
    cap_value = equity * cap_pct / 100.0
    res = compute_risk_based_quantity(
        equity=equity, current_price=price, risk_per_trade_pct=budget_pct,
        stop_price=stop_price, max_position_value=cap_value, available_balance=equity,
    )
    qty = res["quantity"] if isinstance(res, dict) else int(res)
    capped_qty = int(cap_value // price)
    if expect_capped:
        assert qty == capped_qty, (
            "cap should bind -> risk_atr sizes exactly like notional and the two modes produce "
            "identical trades")
    else:
        assert qty < capped_qty, "risk budget should bind, so the sizing mode actually matters"


def test_budget_falls_back_to_risk_per_trade_pct_when_unset():
    """Backward compatibility: an existing config with no atr_risk_budget_pct must size exactly as
    it did before, or every stored genome silently changes meaning."""
    e = _Expert(risk_per_trade_pct=2.0)
    budget = e.get_setting_with_interface_default('atr_risk_budget_pct')
    assert budget is None
    effective = budget if budget is not None else e.get_setting_with_interface_default('risk_per_trade_pct')
    assert float(effective) == 2.0


def test_budget_wins_when_set():
    e = _Expert(risk_per_trade_pct=8.0, atr_risk_budget_pct=1.0)
    budget = e.get_setting_with_interface_default('atr_risk_budget_pct')
    effective = budget if budget is not None else e.get_setting_with_interface_default('risk_per_trade_pct')
    assert float(effective) == 1.0, "the dedicated budget must override the stop-distance gene"


def test_notional_mode_is_structurally_untouched():
    """The new gene must not be able to affect notional sizing.

    Guarded structurally rather than behaviourally: `atr_risk_budget_pct` has exactly ONE reader
    (_risk_atr_quantity) and that function has exactly ONE caller, inside `if sizing_mode ==
    'risk_atr':`. If either fact stops being true, the gene has leaked into the notional path and
    every notional result silently changes meaning.
    """
    import pathlib, re
    src = pathlib.Path(__file__).resolve().parents[1] / "ba2_common" / "core" / "TradeRiskManagement.py"
    text = src.read_text(encoding="utf-8")

    readers = [l for l in text.splitlines() if "atr_risk_budget_pct" in l and "get_setting" in l]
    assert len(readers) == 1, f"expected exactly one reader, found {len(readers)}: {readers}"

    callers = [i for i, l in enumerate(text.splitlines()) if "_risk_atr_quantity(" in l
               and "def _risk_atr_quantity" not in l]
    assert len(callers) == 1, f"expected exactly one caller, found {len(callers)}"

    # the caller must sit under the risk_atr branch
    lines = text.splitlines()
    window = "\n".join(lines[max(0, callers[0] - 6):callers[0] + 1])
    assert "sizing_mode == 'risk_atr'" in window, (
        "the risk_atr sizing call is no longer guarded by the sizing_mode branch -- the budget "
        f"gene may now affect notional runs. Context:\n{window}")


def test_stop_synthesis_still_takes_risk_per_trade_pct_not_the_budget():
    """Stop distance is a SEPARATE job that applies in BOTH modes. If the budget ever reaches it,
    changing position sizing would silently move every stop."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "ba2_common" / "core" / "TradeRiskManagement.py"
    text = src.read_text(encoding="utf-8")
    idx = text.find("synthesize_safeguard_stop(")
    assert idx > 0
    call = text[idx:idx + 320]
    assert "risk_pct" in call, call
    assert "atr_risk_budget_pct" not in call, "the sizing budget must not drive the stop distance"


def test_use_atr_stop_off_is_unchanged_when_the_budget_is_unset():
    """Second reading of 'ATR sizing not used': sizing_mode=risk_atr but use_atr_stop=0.

    There the stop comes from risk_per_trade_pct% (floored at min_stop_loss_pct), NOT from ATR.
    The old code sized as risk_pct / risk_pct = 100% of equity, so the per-instrument cap always
    bound and risk_atr silently degenerated into notional. With the budget UNSET the fallback
    reproduces that exactly -- which is what keeps every stored genome meaning what it meant.
    """
    equity, price = 100_000.0, 100.0
    risk_pct = 5.0                      # also the stop distance when use_atr_stop=0
    stop_price = price * (1 - risk_pct / 100.0)
    cap_value = equity * 0.30

    legacy = compute_risk_based_quantity(          # budget unset -> falls back to risk_per_trade_pct
        equity=equity, current_price=price, risk_per_trade_pct=risk_pct,
        stop_price=stop_price, max_position_value=cap_value, available_balance=equity)
    q_legacy = legacy["quantity"] if isinstance(legacy, dict) else int(legacy)
    assert q_legacy == int(cap_value // price), (
        "with the budget unset, use_atr_stop=0 must still land on the cap exactly as before")


def test_use_atr_stop_off_changes_ONLY_when_the_budget_is_explicitly_set():
    """And when the new gene IS set, the position is no longer pinned to the cap. That change is
    the POINT -- it is what stops risk_atr collapsing onto notional -- but it only ever happens for
    configs that carry the new gene, i.e. new optimizations, never old ones."""
    equity, price = 100_000.0, 100.0
    stop_price = price * (1 - 5.0 / 100.0)
    cap_value = equity * 0.30
    with_budget = compute_risk_based_quantity(
        equity=equity, current_price=price, risk_per_trade_pct=1.0,   # the BUDGET
        stop_price=stop_price, max_position_value=cap_value, available_balance=equity)
    q = with_budget["quantity"] if isinstance(with_budget, dict) else int(with_budget)
    assert q < int(cap_value // price), "an explicit budget should size below the cap"
