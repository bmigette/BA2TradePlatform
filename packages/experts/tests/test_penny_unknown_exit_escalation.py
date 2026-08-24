"""A stop that cannot be measured must not be a stop that did not fire.

Every ``ConditionEvaluator`` handler answered ``False`` when it could not measure --
no OHLCV bar, no RVOL history, no entry price, an unknown condition type, any
exception at all. ``evaluate`` then ran ``all()``/``any()`` over those and returned a
plain bool, so on the exact tick the feed breaks the hard stop silently does not
fire and the position is held blind. That is a LIVE exit path on penny stocks.

The asymmetry is the point:

* an **entry** that cannot be measured must not fire -- unknown stays False there,
  which is the safe direction and is preserved;
* an **exit** that cannot be measured must not be silently skipped -- it escalates,
  and after N consecutive unmeasurable ticks the position is flattened.

Three-valued logic has to be right for that to work. ``all()`` over ``[True, None]``
is not True and ``any()`` over ``[False, None]`` is not False -- Python's builtins
answer False to both, because ``None`` is falsy, which is precisely how an
unmeasurable leg used to turn into a measured one.
"""
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
import pytz

from ba2_experts.PennyMomentumTrader.conditions import (
    DEF_MAX_UNKNOWN_EXIT_TICKS, ConditionEvaluator, ExitEvaluation, kleene_all,
    kleene_any,
)
from ba2_experts.PennyMomentumTrader.monitoring import MonitoringPhasesMixin


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------

def _df(closes, volumes=None):
    n = len(closes)
    return pd.DataFrame({
        "open": [c - 0.1 for c in closes],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": volumes if volumes is not None else [1000] * n,
    })


class _Provider:
    """Returns a fixed frame, or nothing at all (the broken-feed case)."""

    def __init__(self, df=None, raises=False):
        self._df = df
        self._raises = raises

    def get_ohlcv_data(self, symbol, interval="1d", lookback_days=30, **kw):
        if self._raises:
            raise RuntimeError("feed down")
        if self._df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return self._df.copy()


def _live(closes=(10.0,) * 60):
    return ConditionEvaluator(_Provider(_df(list(closes))))


def _blind():
    return ConditionEvaluator(_Provider(None))


def _short(bars=5):
    """Price READABLE, indicators not: enough bars for a last close, far too few
    for a 9-period EMA/SMA or a 14-period RSI. Without this shape a handler that
    coerces its indicator to 0.0 hides behind the price also being None."""
    return ConditionEvaluator(_Provider(_df([10.0] * bars)))


# ==========================================================================
# A. the handlers: unknown is None, measured is a bool
# ==========================================================================

_BLIND_CASES = [
    {"type": "price_above", "value": 5.0},
    {"type": "price_below", "value": 5.0},
    {"type": "price_above_ema", "period": 9, "timeframe": "5m"},
    {"type": "price_below_ema", "period": 9, "timeframe": "5m"},
    {"type": "price_above_sma", "period": 9, "timeframe": "5m"},
    {"type": "price_below_sma", "period": 9, "timeframe": "5m"},
    {"type": "price_above_vwap", "timeframe": "5m"},
    {"type": "price_below_vwap", "timeframe": "5m"},
    {"type": "rsi_above", "threshold": 70, "period": 14, "timeframe": "5m"},
    {"type": "rsi_below", "threshold": 30, "period": 14, "timeframe": "5m"},
    {"type": "rsi_between", "min": 30, "max": 70, "period": 14, "timeframe": "5m"},
    {"type": "rvol_above", "threshold": 2.0},
    {"type": "volume_above_avg", "multiplier": 1.5, "window": 20},
    {"type": "volume_spike", "multiplier": 2.0, "minutes": 5},
    {"type": "opening_range_breakout", "minutes": 5},
    {"type": "macd_bullish_cross", "timeframe": "5m"},
    {"type": "macd_bearish_cross", "timeframe": "5m"},
    {"type": "ema_cross_above", "fast_period": 9, "slow_period": 21, "timeframe": "5m"},
    {"type": "ema_cross_below", "fast_period": 9, "slow_period": 21, "timeframe": "5m"},
    {"type": "percent_above_entry", "percent": 5},
    {"type": "percent_below_entry", "percent": 5},
]


@pytest.mark.parametrize("cond", _BLIND_CASES, ids=lambda c: c["type"])
def test_every_handler_reports_unknown_when_it_cannot_measure(cond):
    assert _blind().evaluate_single(cond, "X", entry_price=10.0) is None


@pytest.mark.parametrize("cond", [
    {"type": "price_above_ema", "period": 9, "timeframe": "5m"},
    {"type": "price_below_ema", "period": 9, "timeframe": "5m"},
    {"type": "price_above_sma", "period": 9, "timeframe": "5m"},
    {"type": "price_below_sma", "period": 9, "timeframe": "5m"},
    {"type": "rsi_above", "threshold": 70, "period": 14, "timeframe": "5m"},
    {"type": "rsi_below", "threshold": 30, "period": 14, "timeframe": "5m"},
    {"type": "rsi_between", "min": 30, "max": 70, "period": 14, "timeframe": "5m"},
    {"type": "macd_bullish_cross", "timeframe": "5m"},
    {"type": "ema_cross_above", "fast_period": 9, "slow_period": 21, "timeframe": "5m"},
], ids=lambda c: c["type"])
def test_a_readable_price_with_an_unreadable_indicator_is_still_unknown(cond):
    """The nastier half of the blind case: the quote arrives, the history does
    not. An indicator coerced to 0.0 here compares against a real price and
    produces a confident, fabricated verdict."""
    assert _short().evaluate_single(cond, "X", entry_price=10.0) is None


def test_rvol_with_no_same_slot_history_is_unknown():
    idx = pd.date_range("2024-03-15 09:30", periods=2, freq="30min", tz="US/Eastern")
    frame = _df([10.0, 10.0]).set_index(idx)
    ev = ConditionEvaluator(_Provider(frame))
    assert ev.evaluate_single({"type": "rvol_above", "threshold": 2.0}, "X") is None


def test_vwap_over_a_zero_volume_session_is_unknown():
    """No volume means no volume-weighted price -- not a VWAP of zero, which
    every quoted price is trivially above."""
    frame = _df([10.0] * 30, volumes=[0] * 30)
    ev = ConditionEvaluator(_Provider(frame))
    assert ev.evaluate_single({"type": "price_above_vwap", "timeframe": "5m"},
                              "X") is None


def test_a_measurable_cross_answers_a_plain_python_bool():
    """The crossover helpers build their verdict out of numpy scalars, so the
    result can be a numpy.bool_ -- which is NOT ``False``/``True`` by identity and
    would slip past a three-valued check that tests for the singletons."""
    rising = ConditionEvaluator(_Provider(_df([10.0 + i * 0.5 for i in range(80)])))
    got = rising.evaluate_single({"type": "macd_bullish_cross", "timeframe": "5m"}, "X")
    assert got is True or got is False
    assert isinstance(got, bool) and not isinstance(got, np.bool_)


def test_an_unknown_condition_type_is_unmeasurable_not_false():
    """An exit rule we cannot even parse is a stop that will never fire."""
    assert _live().evaluate_single({"type": "no_such_condition"}, "X") is None


def test_a_handler_that_raises_is_unmeasurable_not_false():
    ev = ConditionEvaluator(_Provider(raises=True))
    assert ev.evaluate_single({"type": "price_above", "value": 5.0}, "X") is None


def test_percent_conditions_without_an_entry_price_are_unmeasurable():
    ev = _live()
    assert ev.evaluate_single({"type": "percent_below_entry", "percent": 5},
                              "X", entry_price=None) is None
    assert ev.evaluate_single({"type": "percent_above_entry", "percent": 5},
                              "X", entry_price=None) is None


# ---- the inverse: a measurable condition still answers a plain bool -------

def test_measurable_conditions_still_answer_true_or_false():
    ev = _live()
    assert ev.evaluate_single({"type": "price_above", "value": 5.0}, "X") is True
    assert ev.evaluate_single({"type": "price_above", "value": 50.0}, "X") is False
    assert ev.evaluate_single({"type": "price_below", "value": 50.0}, "X") is True


def test_a_price_exactly_at_the_stop_is_a_measured_false_not_unknown():
    """price == entry is a real reading: the stop has NOT been hit."""
    ev = _live()
    assert ev.evaluate_single({"type": "percent_below_entry", "percent": 0},
                              "X", entry_price=10.0) is True
    assert ev.evaluate_single({"type": "percent_below_entry", "percent": 5},
                              "X", entry_price=10.0) is False


def test_a_zero_reading_is_measured_not_unknown():
    """A flat series really does have an RSI/EMA; nothing here is missing."""
    ev = _live()
    assert ev.evaluate_single({"type": "price_above_ema", "period": 9,
                               "timeframe": "5m"}, "X") is False
    assert ev.evaluate_single({"type": "price_below_ema", "period": 9,
                               "timeframe": "5m"}, "X") is False


def test_a_time_condition_is_always_measurable(monkeypatch):
    """The clock never breaks, so a time-based stop must never escalate."""
    tz = pytz.timezone("US/Eastern")
    frozen = tz.localize(datetime(2024, 3, 15, 14, 30))

    class _Clock:
        @staticmethod
        def now(t=None):
            return frozen

    import sys
    conditions_mod = sys.modules[ConditionEvaluator.__module__]
    monkeypatch.setattr(conditions_mod, "datetime", _Clock)
    ev = _blind()          # no market data at all
    assert ev.evaluate_single({"type": "time_after", "time": "10:00"}, "X") is True
    assert ev.evaluate_single({"type": "time_before", "time": "10:00"}, "X") is False


def test_a_malformed_time_threshold_is_unmeasurable():
    assert _live().evaluate_single({"type": "time_after", "time": "not-a-time"},
                                   "X") is None


def test_a_zero_volume_baseline_is_unmeasurable_not_false():
    """A halted name has no average volume to compare against; the ratio is
    undefined, which is not the same as 'the volume test failed'."""
    ev = ConditionEvaluator(_Provider(_df([10.0] * 30, volumes=[0] * 30)))
    assert ev.evaluate_single({"type": "volume_above_avg", "multiplier": 1.5,
                               "window": 20}, "X") is None


# ==========================================================================
# B. three-valued composites
# ==========================================================================

def test_kleene_all_is_not_pythons_all():
    assert all([True, None]) is False          # the builtin's answer -- wrong here
    assert kleene_all([True, None]) is None    # ours


def test_kleene_any_is_not_pythons_any():
    assert any([False, None]) is False         # the builtin's answer -- wrong here
    assert kleene_any([False, None]) is None   # ours


@pytest.mark.parametrize("values,expected", [
    ([], True),
    ([True], True),
    ([True, True], True),
    ([True, None], None),
    ([None, True], None),
    ([None], None),
    ([False, None], False),        # a definite False decides regardless
    ([None, False], False),
    ([True, False], False),
    ([True, None, False], False),
])
def test_kleene_all_truth_table(values, expected):
    assert kleene_all(values) is expected


@pytest.mark.parametrize("values,expected", [
    ([], False),
    ([False], False),
    ([False, False], False),
    ([False, None], None),
    ([None, False], None),
    ([None], None),
    ([True, None], True),          # a definite True decides regardless
    ([None, True], True),
    ([True, False], True),
])
def test_kleene_any_truth_table(values, expected):
    assert kleene_any(values) is expected


def test_an_unmeasurable_leg_makes_an_all_composite_unknown():
    """The live shape: "price below VWAP AND below the 9 EMA" with no feed."""
    ev = _blind()
    conds = {"all": [{"type": "price_below_vwap", "timeframe": "5m"},
                     {"type": "price_below_ema", "period": 9, "timeframe": "5m"}]}
    assert ev.evaluate_tristate(conds, "X") is None


def test_a_met_leg_still_decides_an_any_composite_despite_an_unknown_one():
    """A stop that HAS triggered fires even though a sibling leg is blind."""
    ev = _live()
    conds = {"any": [{"type": "price_above", "value": 5.0},          # True
                     {"type": "rvol_above", "threshold": 2.0}]}      # unmeasurable
    assert ev.evaluate_tristate(conds, "X") is True


def test_an_unmet_leg_still_decides_an_all_composite_despite_an_unknown_one():
    ev = _live()
    conds = {"all": [{"type": "price_above", "value": 500.0},        # False
                     {"type": "rvol_above", "threshold": 2.0}]}      # unmeasurable
    assert ev.evaluate_tristate(conds, "X") is False


def test_unknown_propagates_through_nested_composites():
    ev = _live()
    conds = {"any": [{"all": [{"type": "price_above", "value": 5.0},     # True
                              {"type": "rvol_above", "threshold": 2.0}]}]}  # unknown
    assert ev.evaluate_tristate(conds, "X") is None


# ==========================================================================
# C. entry policy: unknown stays False (deliberately)
# ==========================================================================

def test_entry_conditions_treat_unknown_as_do_not_enter():
    ev = _blind()
    conds = {"all": [{"type": "price_above", "value": 1.0}]}
    assert ev.evaluate_tristate(conds, "X") is None
    assert ev.evaluate(conds, "X") is False


def test_entry_conditions_still_fire_when_they_are_measurably_met():
    assert _live().evaluate({"all": [{"type": "price_above", "value": 5.0}]},
                            "X") is True


def test_entry_conditions_do_not_fire_when_they_are_measurably_unmet():
    """The other half of the entry policy: collapsing unknown to False must not
    be done by collapsing everything-that-is-not-unknown to True."""
    assert _live().evaluate({"all": [{"type": "price_above", "value": 500.0}]},
                            "X") is False
    assert _live().evaluate({"any": [{"type": "price_above", "value": 500.0}]},
                            "X") is False


# ==========================================================================
# D. the exit escalation
# ==========================================================================

_STOP = {"any": [{"type": "percent_below_entry", "percent": 5}]}


def test_a_met_stop_fires_at_once_and_is_not_an_escalation():
    ev = ConditionEvaluator(_Provider(_df([9.0] * 60)))     # entry 10 -> -10%
    out = ev.evaluate_exit(_STOP, "X", entry_price=10.0, unknown_streak=2)
    assert isinstance(out, ExitEvaluation)
    assert out.verdict is True
    assert out.fire is True
    assert out.escalated is False
    assert out.unknown_streak == 0          # a measurable bar clears the streak


def test_an_unmet_stop_does_not_fire_and_clears_the_streak():
    ev = _live()                                            # 10.0, entry 10.0
    out = ev.evaluate_exit(_STOP, "X", entry_price=10.0, unknown_streak=2)
    assert out.verdict is False
    assert out.fire is False
    assert out.escalated is False
    assert out.unknown_streak == 0


def test_a_blind_tick_does_not_fire_immediately_but_counts():
    out = _blind().evaluate_exit(_STOP, "X", entry_price=10.0, unknown_streak=0,
                                 max_unknown_ticks=3)
    assert out.verdict is None
    assert out.fire is False
    assert out.unknown_streak == 1


def test_the_stop_is_forced_after_N_consecutive_blind_ticks():
    ev = _blind()
    streak, fired_on = 0, None
    for tick in range(1, 6):
        out = ev.evaluate_exit(_STOP, "X", entry_price=10.0,
                               unknown_streak=streak, max_unknown_ticks=3)
        streak = out.unknown_streak
        if out.fire:
            fired_on = tick
            assert out.escalated is True
            assert out.verdict is None
            assert "3" in out.detail
            break
    assert fired_on == 3, "the position must not be held blind indefinitely"


def test_it_cannot_deadlock_for_any_window():
    """For every N, the escalation fires by tick N. There is no path that holds
    an unmeasurable position forever."""
    for n in (1, 2, 3, 7, 20):
        ev = _blind()
        streak = 0
        fired_on = None
        for tick in range(1, n + 3):
            out = ev.evaluate_exit(_STOP, "X", entry_price=10.0,
                                   unknown_streak=streak, max_unknown_ticks=n)
            streak = out.unknown_streak
            if out.fire:
                fired_on = tick
                break
        assert fired_on == n, f"max_unknown_ticks={n} fired on {fired_on}"


def test_a_measurable_bar_in_between_resets_the_streak():
    """N CONSECUTIVE blind ticks, not N in total -- a feed that recovers must not
    leave the position one hiccup away from being flattened."""
    blind, live = _blind(), _live()
    streak = 0
    for _ in range(2):
        streak = blind.evaluate_exit(_STOP, "X", entry_price=10.0,
                                     unknown_streak=streak,
                                     max_unknown_ticks=3).unknown_streak
    assert streak == 2
    good = live.evaluate_exit(_STOP, "X", entry_price=10.0, unknown_streak=streak,
                              max_unknown_ticks=3)
    assert good.fire is False
    assert good.unknown_streak == 0
    nxt = blind.evaluate_exit(_STOP, "X", entry_price=10.0,
                              unknown_streak=good.unknown_streak, max_unknown_ticks=3)
    assert nxt.fire is False
    assert nxt.unknown_streak == 1


def test_the_escalation_never_fires_on_a_measurable_bar():
    """The inverse error, and the expensive one: escalating a HEALTHY position
    would exit good trades. A measurable 'not hit' can repeat forever."""
    ev = _live()
    streak = 0
    for _ in range(50):
        out = ev.evaluate_exit(_STOP, "X", entry_price=10.0, unknown_streak=streak,
                               max_unknown_ticks=3)
        assert out.fire is False
        assert out.escalated is False
        streak = out.unknown_streak
    assert streak == 0


def test_a_carried_over_streak_cannot_fire_a_measurable_bar():
    """Even arriving with a streak far past the window, a bar we CAN measure
    decides on its merits."""
    out = _live().evaluate_exit(_STOP, "X", entry_price=10.0, unknown_streak=99,
                                max_unknown_ticks=3)
    assert out.fire is False
    assert out.escalated is False
    assert out.unknown_streak == 0


def test_the_window_is_clamped_to_at_least_one_tick():
    """A misconfigured 0 must not mean 'never escalate' (a silent hold again)."""
    for bad in (0, -5):
        out = _blind().evaluate_exit(_STOP, "X", entry_price=10.0,
                                     unknown_streak=0, max_unknown_ticks=bad)
        assert out.fire is True
        assert out.escalated is True


def test_the_detail_names_the_condition_that_could_not_be_measured():
    out = _blind().evaluate_exit(_STOP, "X", entry_price=10.0, unknown_streak=2,
                                 max_unknown_ticks=3)
    assert out.fire is True
    assert "percent_below_entry" in out.detail


def test_the_detail_reports_how_long_we_have_been_blind():
    out = _blind().evaluate_exit(_STOP, "X", entry_price=10.0, unknown_streak=6,
                                 max_unknown_ticks=3)
    assert "7 consecutive ticks" in out.detail


def test_an_absent_streak_is_read_as_a_fresh_position_not_a_crash():
    """Callers persist the streak in a JSON blob, where the key is simply absent
    the first time. That must be a zero, not a TypeError that kills the tick."""
    for start in (None, 0):
        out = _blind().evaluate_exit(_STOP, "X", entry_price=10.0,
                                     unknown_streak=start, max_unknown_ticks=3)
        assert out.unknown_streak == 1
        assert out.fire is False


def test_the_decision_is_immutable():
    """A decision a caller can edit is a decision no test can pin."""
    out = _live().evaluate_exit(_STOP, "X", entry_price=10.0)
    with pytest.raises(Exception):
        out.fire = True


def test_the_default_window_is_three_ticks():
    assert DEF_MAX_UNKNOWN_EXIT_TICKS == 3


# ==========================================================================
# E. what the UI/debug layer sees
# ==========================================================================

def test_the_condition_status_map_reports_unknown_as_none():
    status = _blind().get_condition_status(_STOP, "X", entry_price=10.0)
    assert list(status.values()) == [None]


def test_the_detail_strings_say_unknown_rather_than_unmet():
    details = _blind().get_condition_details(_STOP, "X", entry_price=10.0)
    assert all("unknown" in v.lower() for v in details.values()), details


def test_the_detail_strings_still_say_met_and_unmet_when_measured():
    ev = ConditionEvaluator(_Provider(_df([9.0] * 60)))
    met = ev.get_condition_details(_STOP, "X", entry_price=10.0)
    assert all(v.startswith("met") for v in met.values()), met
    ev2 = _live()
    unmet = ev2.get_condition_details(_STOP, "X", entry_price=10.0)
    assert all(v.startswith("unmet") for v in unmet.values()), unmet


# ==========================================================================
# F. the monitor wiring
# ==========================================================================

class _Self:
    """The slice of the expert the stop helper touches."""

    def __init__(self):
        self.logger = logging.getLogger("test.penny.escalation")


def test_the_monitor_helper_persists_the_streak_and_forces_the_exit():
    me, info = _Self(), {}
    ev = _blind()
    fired = []
    for tick in range(1, 5):
        out = MonitoringPhasesMixin._evaluate_stop(
            me, ev, _STOP, "X", 10.0, info, max_blind_ticks=3)
        assert info["stop_unknown_streak"] == out.unknown_streak
        if out.fire:
            fired.append(tick)
            break
    assert fired == [3]


def test_the_monitor_helper_resets_the_streak_on_a_readable_tick():
    me, info = _Self(), {"stop_unknown_streak": 2}
    out = MonitoringPhasesMixin._evaluate_stop(
        me, _live(), _STOP, "X", 10.0, info, max_blind_ticks=3)
    assert out.fire is False
    assert info["stop_unknown_streak"] == 0


def test_the_monitor_helper_starts_a_fresh_position_at_zero():
    me, info = _Self(), {}
    out = MonitoringPhasesMixin._evaluate_stop(
        me, ConditionEvaluator(_Provider(_df([9.0] * 60))), _STOP, "X", 10.0, info,
        max_blind_ticks=3)
    assert out.fire is True
    assert out.escalated is False
    assert info["stop_unknown_streak"] == 0


def test_the_monitor_loop_escalates_the_stop_and_only_the_stop():
    """Take-profit is opportunistic, not protective: a blind tick must not burn a
    tier at an unknown price (a tier fires exactly once, and the stop escalation
    already flattens the position if the feed really is dead). Entry is the same
    -- unknown must not open a position."""
    import inspect
    src = inspect.getsource(MonitoringPhasesMixin)
    # BOTH protective-stop call sites (the grace-period hard stop and the normal
    # signal stop) go through the escalating helper...
    assert src.count("self._evaluate_stop(") == 2, \
        "both stop-loss call sites must use the escalating helper"
    assert "evaluator.evaluate(stop_loss" not in src
    assert "evaluator.evaluate(grace_sl" not in src
    # ... and the take-profit / entry branches keep the plain unknown->False API.
    assert "evaluator.evaluate(\n                                tp_condition" in src
    assert "evaluator.evaluate(entry_conds" in src
    assert "evaluate_exit(tp_condition" not in src
    assert "evaluate_exit(entry_conds" not in src


def test_the_monitor_loop_acts_on_fire_and_honours_the_configured_window():
    """Two ways to neuter the escalation without touching conditions.py: branch on
    the raw ``verdict`` (None is falsy, so the escalation is dropped on the floor
    and we are back to holding forever), or forget to pass the configured window
    so the helper's default silently wins."""
    import inspect
    src = inspect.getsource(MonitoringPhasesMixin)
    assert "if stop_eval.fire:" in src
    assert "if grace_eval.fire:" in src
    assert "if stop_eval.verdict:" not in src
    assert "if grace_eval.verdict:" not in src
    assert src.count("max_blind_ticks=blind_ticks") == 2


def test_a_log_price_that_could_not_be_read_does_not_blow_up_the_exit():
    """The escalated exit fires BECAUSE the price is unreadable, and the log line
    it writes formats that price. A bare {None:.4f} raises TypeError inside the
    monitor's per-symbol try/except -- swallowing the exit and reinstating the
    exact defect. This one-liner is load-bearing."""
    from ba2_experts.PennyMomentumTrader.monitoring import _fmt_px
    assert _fmt_px(None) == "n/a"
    assert _fmt_px(1.23456) == "1.2346"
    assert _fmt_px(0.0) == "0.0000"        # a real zero price still prints


def test_the_configured_escalation_window_defaults_to_three_ticks():
    """0 would escalate on the first blind tick (clamped to 1) and flatten healthy
    positions on ordinary API noise; a huge value is the silent hold again."""
    from ba2_experts.PennyMomentumTrader.settings import SETTINGS_DEFINITIONS
    spec = SETTINGS_DEFINITIONS["exit_blind_max_ticks"]
    assert spec["type"] == "int"
    assert spec["default"] == 3
    assert spec["default"] >= 1
