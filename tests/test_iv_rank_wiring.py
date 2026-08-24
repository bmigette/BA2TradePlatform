"""IV-rank wiring: daily cadence, honest-or-nothing recording, fail-closed gating.

Before this suite, ``IVRankCondition`` could never return True in production: nothing
ever called ``record_atm_iv``, so ``option_iv_snapshot`` was empty, ``get_iv_rank``
returned None and the condition failed closed. Wiring a recorder is only safe if three
properties hold, and each is pinned here:

1. ONE sample per (account, underlying) per UTC day. The natural hook (the 5-minute
   account refresh) would otherwise write ~288 rows/day and silently turn a "1-year
   percentile" into a "last-few-days percentile".
2. A missing IV is recorded as NOTHING, loudly — never as a fabricated number, because
   the row feeds a live trading gate.
3. An unavailable rank NEVER satisfies a gate, whichever direction the operator points.
"""
from datetime import datetime, timedelta, timezone

import pytest

from ba2_trade_platform.core.db import add_instance, get_all_instances
from ba2_trade_platform.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_trade_platform.core.models import OptionIVSnapshot
from ba2_trade_platform.core.types import ExpertEventType
from ba2_trade_platform.core.TradeConditions import create_condition


def _snapshots(underlying="AAPL"):
    return [r for r in get_all_instances(OptionIVSnapshot) if r.underlying == underlying]


def _seed_days_ago(account, underlying, samples):
    """Seed one sample per distinct past day: ``samples`` is [(days_ago, iv), ...]."""
    now = datetime.now(timezone.utc)
    for days_ago, iv in samples:
        add_instance(OptionIVSnapshot(
            account_id=account.id, underlying=underlying, atm_iv=iv,
            recorded_at=now - timedelta(days=days_ago),
        ))


def _capture_logs(monkeypatch, level="warning"):
    """Collect ba2_common logger messages. NOT caplog (logger.propagate is False)."""
    from ba2_common.logger import logger
    messages = []
    monkeypatch.setattr(logger, level, lambda msg, *a, **k: messages.append(str(msg)))
    return messages


# ---------------------------------------------------------------------------
# 1. Cadence: one row per underlying per UTC day
# ---------------------------------------------------------------------------

def test_record_atm_iv_is_idempotent_within_the_utc_day(mock_account):
    """Drive the hook twice in one day -> exactly ONE row.

    This is the whole reason a recorder can be hung off a scheduler at all. Without
    it, any cadence faster than daily silently reweights the trailing window toward
    the last few days.
    """
    first = mock_account.record_atm_iv("AAPL", 0.20)
    second = mock_account.record_atm_iv("AAPL", 0.55)

    rows = _snapshots()
    assert len(rows) == 1, "a second same-day sample must not create a second row"
    assert second == first, "the same-day call must return the EXISTING row id"
    assert rows[0].atm_iv == 0.20, "the first sample of the day wins; it is not overwritten"


def test_record_atm_iv_dedups_before_fetching_so_a_rerun_costs_no_broker_call(mock_account):
    """The guard must short-circuit BEFORE ``get_atm_implied_volatility``.

    Ordering matters operationally, not just logically: the fetch is a full option-chain
    request per symbol. A guard placed after it would still dedup the row while paying
    the entire API bill on every re-run.
    """
    mock_account.record_atm_iv("AAPL", 0.20)

    calls = []
    orig = mock_account.get_atm_implied_volatility
    mock_account.get_atm_implied_volatility = lambda u: (calls.append(u), orig(u))[1]

    assert mock_account.record_atm_iv("AAPL") is not None
    assert calls == [], "same-day re-run must not re-fetch the chain"


def test_record_atm_iv_records_again_on_a_new_day(mock_account):
    """Dedup is per-DAY, not per-underlying-forever — the series must still grow."""
    _seed_days_ago(mock_account, "AAPL", [(1, 0.20)])

    new_id = mock_account.record_atm_iv("AAPL", 0.30)

    assert new_id is not None
    assert len(_snapshots()) == 2, "yesterday's sample must not block today's"


def test_the_boundary_is_the_calendar_day_not_a_rolling_24_hours(mock_account):
    """A sample from one microsecond before midnight UTC belongs to YESTERDAY.

    Rolling-24h dedup passes every other test in this file and is wrong: the recorder
    fires on a market-time cron, so consecutive runs are 23h or 25h apart twice a year
    (DST), and a coalesced or late run can land early. Under a 24h rule that silently
    SKIPS a trading day, leaving a hole in the window; under calendar-day semantics
    every trading day contributes exactly one sample. The DB index enforces the same
    rule (``date(recorded_at)``), so the two must agree or writes start raising.
    """
    boundary = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    add_instance(OptionIVSnapshot(account_id=mock_account.id, underlying="AAPL",
                                  atm_iv=0.99, recorded_at=boundary - timedelta(microseconds=1)))

    assert mock_account.record_atm_iv("AAPL", 0.20) is not None
    assert len(_snapshots()) == 2, "yesterday's 23:59:59 sample must not absorb today's"


def test_the_first_instant_of_today_counts_as_today(mock_account):
    boundary = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    add_instance(OptionIVSnapshot(account_id=mock_account.id, underlying="AAPL",
                                  atm_iv=0.99, recorded_at=boundary))

    mock_account.record_atm_iv("AAPL", 0.20)
    assert len(_snapshots()) == 1


def test_record_atm_iv_dedup_is_per_underlying(mock_account):
    mock_account.record_atm_iv("AAPL", 0.20)
    mock_account.record_atm_iv("MSFT", 0.25)
    assert len(get_all_instances(OptionIVSnapshot)) == 2


def test_record_atm_iv_dedup_is_per_account(mock_account, mock_account_def):
    """Two accounts keep independent series (the rank is per-account by design)."""
    from ba2_trade_platform.core.models import AccountDefinition
    other_id = add_instance(AccountDefinition(name="other", provider="Mock", description=None))

    mock_account.record_atm_iv("AAPL", 0.20)
    add_instance(OptionIVSnapshot(account_id=other_id, underlying="AAPL", atm_iv=0.99))

    assert len(_snapshots()) == 2
    # ...and the other account's sample does not leak into this account's rank.
    _seed_days_ago(mock_account, "AAPL", [(1, 0.10), (2, 0.11), (3, 0.12), (4, 0.13), (5, 0.14)])
    assert mock_account.get_iv_rank("AAPL", min_samples=5) == 100.0  # current 0.30 > all 5


# ---------------------------------------------------------------------------
# 2. No IV -> no row, loudly
# ---------------------------------------------------------------------------

def test_record_atm_iv_writes_nothing_and_warns_when_no_iv_is_available(mock_account, monkeypatch):
    """A missing IV must never become a number. Recording a fabricated value into a
    table a trading gate reads is strictly worse than the gate staying shut."""
    monkeypatch.setattr(type(mock_account), "get_atm_implied_volatility",
                        lambda self, u: None, raising=True)
    warnings = _capture_logs(monkeypatch)

    assert mock_account.record_atm_iv("AAPL") is None
    assert _snapshots() == []
    assert any("AAPL" in m for m in warnings), \
        "an unavailable IV must be visible in the log, not a silent no-op"


# ---------------------------------------------------------------------------
# 3. get_iv_rank plumbing
# ---------------------------------------------------------------------------

def test_get_iv_rank_accepts_a_precomputed_current(mock_account):
    """``current`` lets a caller that already holds today's IV skip the refetch.

    ``get_iv_rank`` otherwise issues a full option-chain request on EVERY rule
    evaluation, purely to recompute a value already on disk.
    """
    _seed_days_ago(mock_account, "AAPL", [(1, 0.10), (2, 0.20), (3, 0.30), (4, 0.40), (5, 0.50)])

    calls = []
    orig = mock_account.get_atm_implied_volatility
    mock_account.get_atm_implied_volatility = lambda u: (calls.append(u), orig(u))[1]

    # current=0.45 -> 4 of 5 strictly below -> 80.0 (the mock's own 0.30 would give 40.0)
    assert mock_account.get_iv_rank("AAPL", min_samples=5, current=0.45) == 80.0
    assert calls == [], "an explicit current must not trigger a chain fetch"


def test_get_iv_rank_still_fetches_current_when_not_supplied(mock_account):
    _seed_days_ago(mock_account, "AAPL", [(1, 0.10), (2, 0.20), (3, 0.30), (4, 0.40), (5, 0.50)])
    assert mock_account.get_iv_rank("AAPL", min_samples=5) == 40.0  # mock current = 0.30


def test_todays_own_sample_is_history_for_nobody(mock_account):
    """The series is the trailing days; today's sample is not one of them.

    ``_iv_rank_from_series`` counts strictly ``<``, so today's sample — recorded from
    the same ATM IV that becomes ``current`` — can never count as below it. Leaving it
    in would drag every rank down by 100/N (20 points at min_samples=5) and ONLY for
    evaluations after the 16:30 ET recorder run, so the same rule on the same tape
    would score differently morning and evening. It also keeps the live definition
    identical to BacktestAccount.get_iv_rank's.
    """
    _seed_days_ago(mock_account, "AAPL", [(1, 0.10), (2, 0.11), (3, 0.12), (4, 0.13), (5, 0.14)])
    assert mock_account.get_iv_rank("AAPL", min_samples=5) == 100.0

    mock_account.record_atm_iv("AAPL")           # today's sample == current (0.30)
    assert len(_snapshots()) == 6
    assert mock_account.get_iv_rank("AAPL", min_samples=5) == 100.0, \
        "recording today's sample must not change today's rank"
    assert mock_account.iv_sample_count("AAPL") == 5, \
        "the readiness count must match what the rank actually uses"


def test_get_iv_rank_returns_none_below_min_samples(mock_account):
    """None means "not enough data" and is DISTINCT from 0.0 ("lowest IV of the year")."""
    _seed_days_ago(mock_account, "AAPL", [(1, 0.40), (2, 0.50), (3, 0.60), (4, 0.70)])
    assert mock_account.get_iv_rank("AAPL", min_samples=5) is None
    _seed_days_ago(mock_account, "AAPL", [(5, 0.80)])
    assert mock_account.get_iv_rank("AAPL", min_samples=5) == 0.0  # 5 samples, all above 0.30


def test_iv_sample_count_reports_window_depth(mock_account):
    """Readiness reporting needs the sample count, not just the rank."""
    assert mock_account.iv_sample_count("AAPL") == 0
    _seed_days_ago(mock_account, "AAPL", [(1, 0.1), (2, 0.2), (400, 0.9)])
    assert mock_account.iv_sample_count("AAPL") == 2, "out-of-window samples must not count"
    assert mock_account.iv_sample_count("AAPL", lookback_days=100000) == 3


def test_the_live_window_is_the_shared_one_year_constant(mock_account):
    """The DEFAULT window, not just an explicitly-passed one.

    ``lookback_days`` is CALENDAR days here (that is what ``recorded_at`` supports) and
    the recorder's weekday cron is what turns it into sessions. 252 calendar days — the
    old default — is ~173 sessions, about 8.5 months, while the rule names ("Rich IV",
    "Cheap IV") and every docstring said one year. Pinned against the shared constant so
    the live and backtest windows cannot drift apart, and so a silent narrowing of the
    default cannot quietly turn the gate into a short-term momentum filter.
    """
    assert OptionsAccountInterface.IV_RANK_LOOKBACK_DAYS == 365, "a true one-year window"

    _seed_days_ago(mock_account, "AAPL", [(40, 0.1), (200, 0.2), (300, 0.3), (400, 0.9)])
    assert mock_account.iv_sample_count("AAPL") == 3, \
        "everything inside a year counts; the 400-day-old sample does not"


# ---------------------------------------------------------------------------
# 4. Fail-closed: an unknown rank never satisfies a gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("operator_str,value", [
    ("<=", 35.0), ("<=", 40.0), ("<=", 50.0), ("<", 100.0),
    (">=", 50.0), (">", 0.0), ("==", 0.0), ("!=", 0.0),
])
def test_iv_rank_condition_fails_closed_for_every_operator(
        mock_account, sample_recommendation, operator_str, value):
    """With no history the rank is None. EVERY operator must evaluate False.

    ``<=`` is the dangerous direction: 7 of the 9 live iv_rank rules read "IV is low",
    and a None-as-zero slip would make an unknown IV look like the calmest tape of the
    year and open real positions.
    """
    cond = create_condition(ExpertEventType.N_IV_RANK, mock_account, "AAPL",
                            sample_recommendation, operator_str=operator_str, value=value)
    assert cond.evaluate() is False
    assert cond.calculated_value is None, "an unknown rank must not be displayed as a number"


def test_iv_rank_condition_min_samples_is_five(mock_account, sample_recommendation):
    """The condition deliberately relaxes the account default (20) to 5. Pinned because
    silently raising it re-inerts 9 live rules, and lowering it makes the rank noise."""
    from ba2_common.core.TradeConditions import IVRankCondition
    assert IVRankCondition.IV_RANK_MIN_SAMPLES == 5

    cond = create_condition(ExpertEventType.N_IV_RANK, mock_account, "AAPL",
                            sample_recommendation, operator_str="<=", value=99.0)
    _seed_days_ago(mock_account, "AAPL", [(1, 0.1), (2, 0.2), (3, 0.3), (4, 0.4)])
    assert cond.evaluate() is False, "4 samples is below the floor -> no rank -> closed"

    _seed_days_ago(mock_account, "AAPL", [(5, 0.5)])
    assert cond.evaluate() is True, "the 5th sample makes the rank computable"


def test_iv_rank_condition_can_finally_return_true_end_to_end(mock_account, sample_recommendation):
    """The point of the whole exercise: record daily, then a gate actually opens."""
    now = datetime.now(timezone.utc)
    for days_ago, iv in [(5, 0.10), (4, 0.12), (3, 0.14), (2, 0.16), (1, 0.18)]:
        add_instance(OptionIVSnapshot(account_id=mock_account.id, underlying="AAPL",
                                      atm_iv=iv, recorded_at=now - timedelta(days=days_ago)))
    # mock current ATM IV = 0.30, above all 5 samples -> rank 100
    cond = create_condition(ExpertEventType.N_IV_RANK, mock_account, "AAPL",
                            sample_recommendation, operator_str=">=", value=50.0)
    assert cond.evaluate() is True
    assert cond.calculated_value == 100.0


# ---------------------------------------------------------------------------
# 5. Plausibility: an impossible IV is UNKNOWN, never a number
# ---------------------------------------------------------------------------
#
# ``record_atm_iv`` used to guard only ``if iv is None``. Everything else the broker
# handed back became a real row. The two shapes a broken options feed actually returns
# are 0.0 (an un-populated float field) and NaN (a failed Black-Scholes inversion), and
# both are catastrophic HERE specifically:
#
#   * 0.0 ranks below every stored sample, so ``get_iv_rank`` returns 0.0 — and SIX of
#     the nine live gated rules are ``iv_rank <= 35/40/50``. A zero-filled field would
#     open every one of them, on real option orders, at the exact moment the feed is
#     broken. "IV is unknown" and "IV is the cheapest of the year" are opposite trading
#     instructions and the old guard could not tell them apart.
#   * NaN propagates: ``v < nan`` is False for every v, so the rank silently becomes
#     0.0 as well, and one NaN in the SERIES poisons nothing visibly while inflating
#     the denominator.
#
# The bound therefore lives at the boundary where a raw IV enters the statistic, and a
# rejected value produces NO ROW — never a substituted 0.0. Same distinction the rest of
# this file pins for a missing IV.

_IMPLAUSIBLE = [
    pytest.param(0.0, id="zero-an-unpopulated-field-not-a-calm-market"),
    pytest.param(-0.20, id="negative-volatility-does-not-exist"),
    pytest.param(float("nan"), id="nan-a-failed-bs-inversion"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(1e-9, id="epsilon-a-zero-fill-that-dodges-a-bare-gt-zero-test"),
    pytest.param(8.0, id="800pct-a-mis-scaled-or-junk-mid"),
    pytest.param(30.0, id="percent-scaled-30-meaning-3000pct"),
]


@pytest.mark.parametrize("iv", _IMPLAUSIBLE)
def test_an_implausible_iv_writes_no_row_and_is_named(mock_account, monkeypatch, iv):
    warnings = _capture_logs(monkeypatch)

    assert mock_account.record_atm_iv("AAPL", iv) is None
    assert _snapshots() == [], "a rejected sample must leave NO row, not a 0.0 row"
    assert any("AAPL" in m for m in warnings)


@pytest.mark.parametrize("iv", _IMPLAUSIBLE)
def test_an_implausible_iv_from_the_broker_feed_is_rejected_too(mock_account, monkeypatch, iv):
    """The bound must sit on the FETCHED value, not only on an explicitly passed one —
    the daily recorder never passes ``iv``; it lets ``record_atm_iv`` fetch."""
    monkeypatch.setattr(type(mock_account), "get_atm_implied_volatility",
                        lambda self, u: iv, raising=True)

    assert mock_account.record_atm_iv("AAPL") is None
    assert _snapshots() == []


@pytest.mark.parametrize("iv", [0.02, 0.05, 0.30, 1.50, 3.00, 4.99])
def test_the_bound_admits_the_real_market(mock_account, iv):
    """A bound that rejects real data is just a subtler fabrication. 5% is roughly the
    quietest ATM IV ever printed on a listed US name; 300-400% is a biotech into a
    binary readout. Both must survive."""
    assert mock_account.record_atm_iv("AAPL", iv) is not None
    assert [r.atm_iv for r in _snapshots()] == [iv]


def test_an_implausible_current_leaves_the_gate_CLOSED_not_wide_open(
        mock_account, monkeypatch, sample_recommendation):
    """THE money test. Five honest history points, then the feed returns 0.0 today.

    Old behaviour: 0 of 5 samples strictly below 0.0 -> rank 0.0 -> every "IV is low"
    rule fires. Required behaviour: the rank is None and the rule stays shut.
    """
    _seed_days_ago(mock_account, "AAPL", [(1, 0.10), (2, 0.11), (3, 0.12), (4, 0.13), (5, 0.14)])
    monkeypatch.setattr(type(mock_account), "get_atm_implied_volatility",
                        lambda self, u: 0.0, raising=True)

    assert mock_account.get_iv_rank("AAPL", min_samples=5) is None, \
        "an impossible current IV must be UNKNOWN, not the cheapest tape of the year"

    cond = create_condition(ExpertEventType.N_IV_RANK, mock_account, "AAPL",
                            sample_recommendation, operator_str="<=", value=40.0)
    assert cond.evaluate() is False
    assert cond.calculated_value is None


def test_a_nan_current_does_not_silently_score_zero(mock_account, monkeypatch):
    """``v < nan`` is False for every v, so NaN scored a clean 0.0 with no error."""
    _seed_days_ago(mock_account, "AAPL", [(1, 0.10), (2, 0.11), (3, 0.12), (4, 0.13), (5, 0.14)])
    monkeypatch.setattr(type(mock_account), "get_atm_implied_volatility",
                        lambda self, u: float("nan"), raising=True)
    assert mock_account.get_iv_rank("AAPL", min_samples=5) is None


def test_a_poisoned_stored_sample_is_dropped_from_the_series(mock_account):
    """Rows predating the bound (or written by a script that bypassed it) must not
    count. They are treated exactly like a None entry: dropped, never 0.0-valued."""
    _seed_days_ago(mock_account, "AAPL", [(1, 0.10), (2, 0.11), (3, 0.12), (4, 0.13)])
    _seed_days_ago(mock_account, "AAPL", [(5, 0.0), (6, float("inf")), (7, 12.0)])

    assert mock_account.iv_sample_count("AAPL") == 7, \
        "the raw row count is a separate fact from the usable one"
    assert mock_account.get_iv_rank("AAPL", min_samples=5) is None, \
        "4 usable samples is below the floor even though 7 rows exist"

    _seed_days_ago(mock_account, "AAPL", [(8, 0.14)])
    assert mock_account.get_iv_rank("AAPL", min_samples=5) == 100.0, \
        "the 5th USABLE sample is what arms the gate"


def test_the_bound_is_one_shared_definition(mock_account):
    """Live, backtest and PremiumSeller must not each invent their own idea of
    'a possible implied volatility'."""
    assert OptionsAccountInterface.plausible_atm_iv(0.30) == 0.30
    assert OptionsAccountInterface.plausible_atm_iv(0.0) is None
    assert OptionsAccountInterface.plausible_atm_iv(None) is None
    assert OptionsAccountInterface.plausible_atm_iv("0.3") is None, "a string is not an IV"
    assert OptionsAccountInterface.plausible_atm_iv(object()) is None
    assert OptionsAccountInterface.plausible_atm_iv(True) is None, \
        "bool is an int subclass; True must not become an IV of 1.0"

    # A numpy scalar off a parquet/pandas path IS a number. np.float32 is not a float
    # subclass, so an isinstance-based guard would silently discard every backtest
    # sample and leave the rank permanently None.
    np = pytest.importorskip("numpy")
    assert OptionsAccountInterface.plausible_atm_iv(np.float32(0.30)) == pytest.approx(0.30)
    assert OptionsAccountInterface.plausible_atm_iv(np.float64(0.30)) == 0.30
    assert OptionsAccountInterface.plausible_atm_iv(np.float64("nan")) is None
    assert OptionsAccountInterface.MIN_PLAUSIBLE_ATM_IV < 0.05
    assert OptionsAccountInterface.MAX_PLAUSIBLE_ATM_IV >= 4.0


def test_the_bound_is_inclusive_at_both_ends():
    """Pins WHICH side of each bound is open. An off-by-one here is invisible in normal
    operation and silently changes what the recorder will accept."""
    lo = OptionsAccountInterface.MIN_PLAUSIBLE_ATM_IV
    hi = OptionsAccountInterface.MAX_PLAUSIBLE_ATM_IV

    assert OptionsAccountInterface.plausible_atm_iv(lo) == lo
    assert OptionsAccountInterface.plausible_atm_iv(hi) == hi
    assert OptionsAccountInterface.plausible_atm_iv(lo * 0.99) is None
    assert OptionsAccountInterface.plausible_atm_iv(hi * 1.01) is None
