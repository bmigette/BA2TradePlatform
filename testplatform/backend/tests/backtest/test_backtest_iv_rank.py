"""BacktestAccount.get_iv_rank — computed from the as-of-clamped options provider.

The base mixin's ``get_iv_rank`` reads ``option_iv_snapshot``, a LIVE table keyed by an
``account_id`` FK into ``accountdefinition``. A backtest inheriting it reads a table
nothing in the backtest ever writes, so the rank was always None and every iv_rank rule
was permanently False — with no error anywhere.

The override does NOT start writing that table. Three reasons, in order of weight:

1. DETERMINISM. A persisted, accumulating series makes GA trial N depend on trials
   1..N-1. Per-trial reproducibility is the entire point of the test platform.
2. LOOK-AHEAD. Computing from the provider keeps every sample inside the as-of clamp by
   construction; a shared SQL table has no as-of notion at all.
3. It would be a write plus a read to recover a number the memoized provider already
   holds — against the direction of the sql-less backtest store.

The percentile math itself stays SHARED (``_iv_rank_from_series``), so live and backtest
cannot drift apart on the definition of the statistic — only on the sample grid, which
is pinned below.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 1.0,
    "slippage_bps": 5.0,
    "fill_model": "next_bar_open",
}
_BARS = [{"Date": datetime(2024, 3, 5), "Open": 180, "High": 182, "Low": 178,
          "Close": 181, "Volume": 1000}]
CLOCK = date(2024, 3, 5)


class _StubProvider:
    """Returns a scripted ATM IV per date and records what was asked for."""

    def __init__(self, by_date, default=None):
        self.by_date = by_date
        self.default = default
        self.asked = []

    def get_atm_iv(self, underlying, as_of):
        self.asked.append(as_of)
        return self.by_date.get(as_of, self.default)


def _account(provider, account_id):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    wire_backtest_seams()
    ctx = backtest_trading_db(f"ivrank-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _BARS)
    ps.set_clock(datetime(2024, 3, 5))
    acct = BacktestAccount(account_id, ps, CFG, options_provider=provider)
    wire_backtest_seams().register_account(account_id, acct)
    return acct, ctx


@pytest.fixture
def make_account():
    ctxs = []

    def _make(provider, account_id=1):
        acct, ctx = _account(provider, account_id)
        ctxs.append(ctx)
        return acct

    yield _make
    for ctx in ctxs:
        ctx.__exit__(None, None, None)


def _flat_series(value, days=400):
    """`value` on every calendar day up to and including the clock."""
    from datetime import timedelta
    return {CLOCK - timedelta(days=i): value for i in range(days)}


def test_rank_is_computed_from_the_provider_series(make_account):
    """Six trailing weekdays below today's IV -> rank 100."""
    from datetime import timedelta
    hist = {CLOCK - timedelta(days=i): 0.10 for i in range(1, 20)}
    hist[CLOCK] = 0.40
    acct = make_account(_StubProvider(hist))

    assert acct.get_iv_rank("AAPL", min_samples=5) == 100.0


def test_the_as_of_sample_is_excluded_from_its_own_percentile(make_account):
    """``_iv_rank_from_series`` counts strictly ``<``. The memoized provider returns a
    BIT-IDENTICAL value for the as-of date, so including it would guarantee one sample
    that is never below current — biasing every rank down by 100/N, i.e. 20 whole points
    at the production min_samples of 5. Live has the same shape by construction (today's
    sample is written after the close, current is a fresh fetch)."""
    from datetime import timedelta
    hist = {CLOCK - timedelta(days=i): 0.10 for i in range(1, 6)}   # 5 weekday-ish samples
    hist[CLOCK] = 0.40
    acct = make_account(_StubProvider(hist, default=0.10))

    assert acct.get_iv_rank("AAPL", min_samples=5) == 100.0, \
        "every trailing sample is below current; the as-of sample must not dilute it"
    assert CLOCK not in acct._options.asked[1:], \
        "the as-of date must not appear in the trailing series"


def test_weekends_are_not_sampled(make_account):
    """The provider clamps to the latest snapshot on or before a date, so a Saturday
    lookup silently returns Friday's value again. Sampling them would triple-count every
    Friday and pull the percentile toward whatever the week ended on."""
    acct = make_account(_StubProvider(_flat_series(0.20)))
    acct.get_iv_rank("AAPL", lookback_days=30, min_samples=5)

    weekend = [d for d in acct._options.asked if d.weekday() >= 5]
    assert weekend == [], f"weekend dates sampled: {weekend[:5]}"


@pytest.mark.parametrize("lookback", [30, 90, 252])
def test_the_window_is_the_requested_lookback(make_account, lookback):
    """Including the 252 default: a hardcoded window would make ``lookback_days``
    decorative and quietly change what "IV rank" measures."""
    from datetime import timedelta
    acct = make_account(_StubProvider(_flat_series(0.20, days=400)), account_id=lookback)
    acct.get_iv_rank("AAPL", lookback_days=lookback, min_samples=5)

    trailing = [d for d in acct._options.asked if d != CLOCK]
    assert min(trailing) >= CLOCK - timedelta(days=lookback)
    assert min(trailing) <= CLOCK - timedelta(days=lookback - 3), \
        "the window must reach back the FULL lookback (weekend slack only)"
    assert max(trailing) < CLOCK


def test_min_samples_is_enforced_and_none_means_unknown(make_account):
    """A rank over two observations is noise with a percentage sign. None ("no rank")
    must stay distinct from 0.0 ("cheapest IV of the year")."""
    from datetime import timedelta
    hist = {CLOCK: 0.40}
    for i in (1, 4, 5):                      # only 3 dated samples; the rest are None
        hist[CLOCK - timedelta(days=i)] = 0.10
    acct = make_account(_StubProvider(hist))

    assert acct.get_iv_rank("AAPL", min_samples=5) is None
    assert acct.get_iv_rank("AAPL", min_samples=3) == 100.0


def test_a_dataless_cache_yields_none_not_zero(make_account):
    """TODAY'S REALITY on the shipped 10 GB cache: option_chain.iv is NULL on all
    6.7M rows and option_bar has no iv column at all, so get_atm_iv returns None for
    every symbol and date. The rank must therefore be None — which keeps
    IVRankCondition failing closed — and must NOT be 0.0, which would open every
    "IV is low" gate on a cache that simply has no IV in it."""
    acct = make_account(_StubProvider({}))
    assert acct.get_iv_rank("AAPL", min_samples=5) is None


def test_no_provider_means_no_rank(make_account):
    """The equity-only backtest path has no options provider at all."""
    acct = make_account(None)
    assert acct.get_iv_rank("AAPL", min_samples=5) is None


def test_a_precomputed_current_is_honoured(make_account):
    from datetime import timedelta
    hist = {CLOCK - timedelta(days=i): 0.10 for i in range(1, 20)}
    hist[CLOCK] = 0.05
    acct = make_account(_StubProvider(hist))

    assert acct.get_iv_rank("AAPL", min_samples=5) == 0.0        # current 0.05, all above
    assert acct.get_iv_rank("AAPL", min_samples=5, current=0.40) == 100.0


def test_percentile_math_is_the_shared_live_implementation(make_account):
    """Live and backtest must never disagree on what "IV rank 60" means. Only the
    sample GRID may differ; the arithmetic is one function."""
    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface

    # CLOCK is Tuesday 2024-03-05; these are the five preceding WEEKDAYS.
    weekdays = [date(2024, 3, 4), date(2024, 3, 1), date(2024, 2, 29),
                date(2024, 2, 28), date(2024, 2, 27)]
    ivs = [0.10, 0.20, 0.30, 0.40, 0.50]
    hist = {CLOCK: 0.30, **dict(zip(weekdays, ivs))}
    acct = make_account(_StubProvider(hist))

    expected = OptionsAccountInterface._iv_rank_from_series(ivs, 0.30, min_samples=5)
    assert expected == 40.0, "2 of 5 strictly below 0.30"
    assert acct.get_iv_rank("AAPL", min_samples=5) == expected
