"""``relative_volume`` and ``iv_to_realized_vol``: the two volume/volatility entry gates.

Three properties are non-negotiable and each is a real bug in this codebase's history:

1. **The trailing average EXCLUDES the current bar.** Include it and the baseline absorbs the
   very spike the gate exists to detect, and the distortion grows exactly as the signal does.
2. **The trailing window does NOT reach forward.** The backtest's ``MemoizedOHLCVProvider``
   DISCARDS ``lookback_days`` and, unclamped, returns the whole ``[start, run end]`` window --
   which is how lookahead got into ``percent_below_recent_high``.
3. **Insufficient history, a zero-volume average, a missing IV or a flat tape is UNKNOWN, not
   a plausible-looking number.** A relative volume that defaults to 1.0 is silently
   free-passing on every newly-listed symbol, and 1.0 looks right enough that nobody checks.

Frozen clock by construction: the simulated bar is in 2021, the run window ends 2021-12, the
wall clock is years later. Nothing here can pass by accidentally agreeing with today.
"""
import math
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from ba2_common.core import TradeConditions
from ba2_common.core.TradeConditions import create_condition
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.types import ExpertEventType

SIM_BAR = date(2021, 3, 15)
RUN_START = "2021-01-01"
RUN_END = "2021-12-31"

WINDOW = 20                 # both conditions' declared window
BASE_VOLUME = 1_000_000.0   # every baseline bar
SPIKE_VOLUME = 5_000_000.0  # the as-of bar -> a clean 5.00x
FUTURE_VOLUME = 100.0       # the simulated future: reading it drags the average DOWN visibly
OLD_VOLUME = 9_000_000.0    # before the window: reading it drags the average UP visibly


def _vol_df(spike=SPIKE_VOLUME, base=BASE_VOLUME):
    """Daily bars where volume is three visibly different regimes, so every distinct way of
    being wrong produces a DIFFERENT number (a flat fixture would let all of them pass)."""
    idx = pd.date_range(RUN_START, RUN_END, freq="D")
    window_start = SIM_BAR - timedelta(days=WINDOW)
    vols = []
    for d in idx.date:
        if d > SIM_BAR:
            vols.append(FUTURE_VOLUME)
        elif d == SIM_BAR:
            vols.append(spike)
        elif d >= window_start:
            vols.append(base)
        else:
            vols.append(OLD_VOLUME)
    return pd.DataFrame({"Date": idx, "Open": 100.0, "High": 100.0, "Low": 100.0,
                         "Close": 100.0, "Volume": vols})


class _MemoizedLikeProvider:
    """Stands in for the backtest's ``MemoizedOHLCVProvider``: DISCARDS ``lookback_days`` and
    honours only ``start_date``/``end_date`` -- exactly the shape that made the unclamped
    fetch return the simulated future."""

    def __init__(self, df):
        self._df = df
        self.calls = []

    def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d", **kw):
        self.calls.append({"symbol": symbol, "end_date": end_date, **kw})
        df = self._df
        if start_date is not None:
            df = df[df["Date"] >= pd.Timestamp(start_date).tz_localize(None)]
        if end_date is not None:
            df = df[df["Date"] <= pd.Timestamp(end_date).tz_localize(None)]
        return df.reset_index(drop=True)


class _BacktestAccount:
    id = 1

    def _as_of_date(self):
        return SIM_BAR

    def get_instrument_current_price(self, symbol, *a, **k):
        return 100.0


class _LiveAccount:
    """No ``_as_of_date`` -- the live shape. Behaviour here must stay byte-identical."""
    id = 1

    def get_instrument_current_price(self, symbol, *a, **k):
        return 100.0


class _OptionsBacktestAccount(_BacktestAccount, OptionsAccountInterface):
    """Options-capable, with a settable ATM IV. Only ``get_atm_implied_volatility`` is
    exercised; the rest of the abstract surface is stubbed so the class is instantiable (the
    condition's ``isinstance`` check needs real inheritance, not a duck type)."""

    def __init__(self, iv):
        self._iv = iv

    def get_atm_implied_volatility(self, underlying):
        return self._iv

    def get_option_chain(self, *a, **k):
        raise AssertionError("iv_to_realized_vol must not fetch a chain")

    def get_option_quote(self, *a, **k):
        raise AssertionError("iv_to_realized_vol must not fetch a quote")

    def get_option_positions(self):
        return []

    def close_option_position(self, *a, **k):
        raise AssertionError("not used")

    def _submit_option_order_impl(self, *a, **k):
        raise AssertionError("not used")


def _rec():
    return SimpleNamespace(created_at=datetime(*SIM_BAR.timetuple()[:3], tzinfo=timezone.utc),
                           instance_id=1, symbol="AAPL", data={})


def _install(monkeypatch, df):
    p = _MemoizedLikeProvider(df)
    monkeypatch.setattr(TradeConditions, "_provider_resolver",
                        lambda category, name, **kw: p, raising=False)
    return p


def _cond(event_type, account, op, value):
    return create_condition(event_type, account, "AAPL", _rec(),
                            operator_str=op, value=value)


def _relvol(account=None, op=">", value=0.0):
    return _cond(ExpertEventType.N_RELATIVE_VOLUME, account or _BacktestAccount(), op, value)


# =====================================================================================
# relative_volume
# =====================================================================================
def test_the_ratio_is_the_current_bar_over_the_preceding_average(monkeypatch):
    _install(monkeypatch, _vol_df())
    cond = _relvol()
    cond.evaluate()
    assert cond.get_calculated_value() == pytest.approx(5.0), (
        "expected 5,000,000 / 1,000,000 = 5.00x"
    )


def test_the_current_bar_is_excluded_from_its_own_baseline(monkeypatch):
    """Including it gives 5,000,000 / ((19 x 1M + 5M) / 20) = 4.17x, not 5.00x -- and the
    understatement grows exactly as the spike does, which is the whole point of the gate."""
    _install(monkeypatch, _vol_df())
    cond = _relvol()
    cond.evaluate()
    contaminated = SPIKE_VOLUME / (((WINDOW - 1) * BASE_VOLUME + SPIKE_VOLUME) / WINDOW)
    assert contaminated == pytest.approx(4.1666, abs=1e-3)   # the wrong answer, pinned
    assert cond.get_calculated_value() != pytest.approx(contaminated)
    assert cond.get_calculated_value() == pytest.approx(5.0)


def test_the_baseline_never_reaches_past_the_evaluation_bar(monkeypatch):
    """Unclamped, the memoized provider returns through 2021-12 and the last bar would be a
    100-share December day, giving a ratio near zero instead of 5x."""
    provider = _install(monkeypatch, _vol_df())
    cond = _relvol()
    cond.evaluate()
    assert cond.get_calculated_value() == pytest.approx(5.0)
    (call,) = provider.calls
    assert call["end_date"] is not None, "the fetch was not clamped to the as-of bar"
    assert call["end_date"].date() == SIM_BAR


def test_the_baseline_is_exactly_the_window_and_not_older_bars(monkeypatch):
    """Bars before the window carry 9x the volume: reading them would give ~0.6x, not 5x."""
    _install(monkeypatch, _vol_df())
    cond = _relvol()
    cond.evaluate()
    too_wide = SPIKE_VOLUME / OLD_VOLUME
    assert cond.get_calculated_value() != pytest.approx(too_wide, abs=0.01)


def test_a_live_account_still_gets_an_unclamped_latest_fetch(monkeypatch):
    """``end_date=None`` is what the provider recognises as "give me the latest" and is what
    permits the parquet top-up. Live behaviour must not change."""
    provider = _install(monkeypatch, _vol_df())
    cond = _relvol(account=_LiveAccount())
    cond.evaluate()
    (call,) = provider.calls
    assert call["end_date"] is None


# --- unknown is never 1.0 ------------------------------------------------------------
def test_insufficient_history_is_unknown_not_normal(monkeypatch):
    """A newly-listed symbol. Defaulting to 1.0 would make the gate free-passing on exactly
    the names with no track record."""
    df = _vol_df()
    df = df[df["Date"] >= pd.Timestamp(SIM_BAR - timedelta(days=5))].reset_index(drop=True)
    _install(monkeypatch, df)
    cond = _relvol(op=">", value=1.5)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_exactly_enough_history_is_measurable(monkeypatch):
    """The boundary the test above stands on: W baseline bars + the current one is ENOUGH, so
    the guard is a genuine minimum and not an off-by-one that silently widens it."""
    df = _vol_df()
    df = df[df["Date"] <= pd.Timestamp(SIM_BAR)].tail(WINDOW + 1).reset_index(drop=True)
    _install(monkeypatch, df)
    cond = _relvol()
    assert cond.evaluate() is True
    assert cond.get_calculated_value() == pytest.approx(5.0)


def test_a_zero_volume_average_is_unknown_not_normal(monkeypatch):
    """A halted/unlisted name, or a feed that zero-filled the column. 1.0 here would read as
    "trading normally" on a name that did not trade at all."""
    _install(monkeypatch, _vol_df(base=0.0))
    cond = _relvol(op=">", value=1.5)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_a_nan_in_the_window_is_unknown_not_dropped(monkeypatch):
    """pandas' mean() drops NaNs silently, so a window with most bars missing would average
    the survivors and report a confident ratio against them."""
    df = _vol_df()
    df.loc[df["Date"] == pd.Timestamp(SIM_BAR - timedelta(days=3)), "Volume"] = float("nan")
    _install(monkeypatch, df)
    cond = _relvol()
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_a_genuinely_zero_current_bar_is_a_measurement_not_a_gap(monkeypatch):
    """0 volume TODAY against a real baseline is a real 0.00x (the name did not trade), which
    a "> 1.5" gate must REJECT -- distinct from unknown, which it must also reject but for a
    different reason and with a different calculated_value."""
    _install(monkeypatch, _vol_df(spike=0.0))
    cond = _relvol(op=">", value=1.5)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() == 0.0


def test_a_broken_simulated_clock_is_unknown(monkeypatch):
    _install(monkeypatch, _vol_df())

    class _Broken(_BacktestAccount):
        def _as_of_date(self):
            raise RuntimeError("clock")

    cond = _relvol(account=_Broken())
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


@pytest.mark.parametrize("op", [">", ">=", "<", "<=", "==", "!="])
def test_unknown_refuses_for_every_operator(monkeypatch, op):
    """A "!=" or "<" gate must not pass on unknown just because the operator's polarity would
    make it convenient."""
    _install(monkeypatch, _vol_df(base=0.0))
    cond = _relvol(op=op, value=1.0)
    assert cond.evaluate() is False


def test_the_operator_actually_gates(monkeypatch):
    _install(monkeypatch, _vol_df())
    assert _relvol(op=">", value=4.0).evaluate() is True
    assert _relvol(op=">", value=6.0).evaluate() is False
    assert _relvol(op="<", value=6.0).evaluate() is True


# =====================================================================================
# iv_to_realized_vol
# =====================================================================================
def _trend_df(daily_move=0.01):
    """Closes that alternate +/- ``daily_move`` so realised vol is a known, non-zero number."""
    idx = pd.date_range(RUN_START, RUN_END, freq="D")
    closes, price = [], 100.0
    for i in range(len(idx)):
        price = price * (1 + daily_move) if i % 2 == 0 else price / (1 + daily_move)
        closes.append(price)
    return pd.DataFrame({"Date": idx, "Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": BASE_VOLUME})


def _expected_rv(df, window=WINDOW):
    closes = [float(c) for c in
              df[df["Date"] <= pd.Timestamp(SIM_BAR)]["Close"].iloc[-(window + 1):]]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def _ivrv(monkeypatch, iv, df=None, op=">", value=0.0):
    df = _trend_df() if df is None else df
    _install(monkeypatch, df)
    return _cond(ExpertEventType.N_IV_TO_REALIZED_VOL, _OptionsBacktestAccount(iv), op, value), df


def test_the_ratio_is_iv_over_realised_vol(monkeypatch):
    cond, df = _ivrv(monkeypatch, iv=0.40)
    cond.evaluate()
    rv = _expected_rv(df)
    assert rv > 0.05
    assert cond.get_calculated_value() == pytest.approx(0.40 / rv, rel=1e-9)


def test_realised_vol_never_reaches_past_the_evaluation_bar(monkeypatch):
    """The whole point of clamping: an unclamped window would measure the December tape."""
    provider = _install(monkeypatch, _trend_df())
    cond = _cond(ExpertEventType.N_IV_TO_REALIZED_VOL,
                 _OptionsBacktestAccount(0.40), ">", 0.0)
    cond.evaluate()
    (call,) = provider.calls
    assert call["end_date"] is not None and call["end_date"].date() == SIM_BAR


def test_a_richer_iv_gives_a_higher_ratio(monkeypatch):
    """Guards a constant-returning mutation: the ratio must move with its numerator."""
    cheap, _ = _ivrv(monkeypatch, iv=0.15)
    cheap.evaluate()
    rich, _ = _ivrv(monkeypatch, iv=0.60)
    rich.evaluate()
    assert rich.get_calculated_value() > cheap.get_calculated_value()


def test_a_livelier_tape_gives_a_lower_ratio(monkeypatch):
    """...and with its denominator."""
    quiet, _ = _ivrv(monkeypatch, iv=0.40, df=_trend_df(daily_move=0.002))
    quiet.evaluate()
    wild, _ = _ivrv(monkeypatch, iv=0.40, df=_trend_df(daily_move=0.03))
    wild.evaluate()
    assert wild.get_calculated_value() < quiet.get_calculated_value()


# --- unknown is never a number -------------------------------------------------------
def test_a_missing_iv_is_unknown(monkeypatch):
    cond, _ = _ivrv(monkeypatch, iv=None, op="<", value=1.0)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


@pytest.mark.parametrize("iv", [0.0, 0.0001, float("nan"), 30.0, -0.3, True, "0.30"])
def test_an_implausible_iv_is_unknown(monkeypatch, iv):
    """Delegates to the SHARED ``plausible_atm_iv`` bound so live and backtest cannot fork on
    what counts as an IV. 0.0 is the dangerous one: it would make every "< 1.0" gate pass
    precisely when the feed is broken."""
    cond, _ = _ivrv(monkeypatch, iv=iv, op="<", value=1.0)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_a_flat_tape_is_unknown_not_infinite(monkeypatch):
    """A near-zero denominator does not mean "options are infinitely rich"; it means the tape
    was flat or the feed repeated a close. Dividing by it hands a ">" gate a spectacular pass
    on exactly the names with no usable data."""
    flat = _trend_df(daily_move=0.0)
    _install(monkeypatch, flat)
    cond = _cond(ExpertEventType.N_IV_TO_REALIZED_VOL,
                 _OptionsBacktestAccount(0.40), ">", 1.2)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_a_non_options_account_is_unknown(monkeypatch):
    _install(monkeypatch, _trend_df())
    cond = _cond(ExpertEventType.N_IV_TO_REALIZED_VOL, _BacktestAccount(), "<", 1.0)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


def test_insufficient_price_history_is_unknown(monkeypatch):
    df = _trend_df()
    df = df[df["Date"] >= pd.Timestamp(SIM_BAR - timedelta(days=5))].reset_index(drop=True)
    _install(monkeypatch, df)
    cond = _cond(ExpertEventType.N_IV_TO_REALIZED_VOL,
                 _OptionsBacktestAccount(0.40), "<", 1.0)
    assert cond.evaluate() is False
    assert cond.get_calculated_value() is None


@pytest.mark.parametrize("op", [">", ">=", "<", "<=", "==", "!="])
def test_iv_rv_unknown_refuses_for_every_operator(monkeypatch, op):
    cond, _ = _ivrv(monkeypatch, iv=None, op=op, value=1.0)
    assert cond.evaluate() is False


def test_the_iv_rv_operator_actually_gates(monkeypatch):
    cond, df = _ivrv(monkeypatch, iv=0.40)
    cond.evaluate()
    ratio = cond.get_calculated_value()
    hi, _ = _ivrv(monkeypatch, iv=0.40, op=">", value=ratio - 0.1)
    lo, _ = _ivrv(monkeypatch, iv=0.40, op=">", value=ratio + 0.1)
    assert hi.evaluate() is True
    assert lo.evaluate() is False


# =====================================================================================
# Registration -- a condition that evaluates but is not reachable is the defect
# =====================================================================================
@pytest.mark.parametrize("field", ["relative_volume", "iv_to_realized_vol"])
def test_the_field_is_reachable_from_a_rule_leaf(field):
    from ba2_common.core.rule_builders import triggers_from_condition_tree

    triggers = triggers_from_condition_tree(
        {"type": "AND", "conditions": [{"id": "x", "field": field, "op": ">", "value": 1}]})
    assert triggers
    (only,) = triggers.values()
    assert only == {"event_type": field, "operator": ">", "value": 1}


@pytest.mark.parametrize("field", ["relative_volume", "iv_to_realized_vol"])
def test_the_field_is_in_every_registry(field):
    from ba2_common.core.rule_builders import FIELD_EVENT
    from ba2_common.core.rules_documentation import get_event_type_documentation
    from ba2_common.core.rules_export_import import _FIELD_ABBR
    from ba2_common.core.TradeConditions import CONDITION_MAP
    from ba2_common.core.types import get_numeric_event_values, is_numeric_event

    assert field in FIELD_EVENT
    assert FIELD_EVENT[field] in CONDITION_MAP
    assert field in get_numeric_event_values() and is_numeric_event(field)
    assert field in _FIELD_ABBR
    assert get_event_type_documentation().get(field, {}).get("type") == "numeric"
