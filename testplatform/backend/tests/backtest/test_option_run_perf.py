"""PERF GUARD: an OPTION backtest must REUSE the in-memory order cache and must NOT add
per-bar DB churn.

The 11.4x in-memory-order-cache speedup (see the perf pass) rests on a single discipline in
``DailyBacktestEngine.run``: ``account.invalidate_order_cache()`` is called ONLY on EVENT bars
— bars that actually change the order book (a cadence-scheduled analysis/management pass that
may create orders, or a bar whose fills rolled into transactions) — NOT on every bar. On the
common no-event bar (positions held, nothing analysed/managed, nothing filled) the order cache
is left byte-identical and the fill engine does ZERO order-DB reads.

This test proves that contract for an OPTION run end-to-end. It reuses the rule-driven options
e2e harness (the fixture CALL chain / premium bars, the options-capable ``BacktestAccount``, the
seeded held-equity position and the ``buy_call`` OPEN_POSITIONS rule from
``test_options_rule_e2e``), but:

  * extends the date window to ~26 weekday bars (the shared 5-bar fixture is too short to make
    the assertion meaningful),
  * keeps a held EQUITY position the whole run so the engine steps EVERY bar (its flat-bar skip
    only fires when nothing is held / no order is working — an option-only hold gets jumped to
    the next analysis bar, which would hide the no-op bars we want to measure), and
  * pins the expert's run cadence to THURSDAYS only, so the (book-dirtying) analysis +
    open-position management pass runs on a handful of bars while the option/equity are HELD,
    untouched, on every other bar.

It wraps the account's ``invalidate_order_cache`` with a counter and counts total bars via
``snapshot_equity`` (called exactly once per processed bar).

THE assertion: ``invalidate_calls < total_bars`` with a REAL gap (a clear majority of bars are
no-op bars that did NOT invalidate) — proving the cache is NOT invalidated every bar. A
regression to an unconditional per-bar ``invalidate_order_cache`` would make
``invalidate_calls >= total_bars`` and this fails.

Run from the backend dir:
    ~/ba2-venvs/test/bin/python -m pytest tests/backtest/test_option_run_perf.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# Reuse the rule-driven options harness: the HOLD expert, the held-equity seeder, and the fixture
# cache/chain seeding verbatim.
from tests.backtest.test_options_rule_e2e import (  # type: ignore
    CFG,
    _OCC_180,
    _HoldExpert,
    _seed_cache,
    _seed_held_equity_position,
    _underlying_rows,
)

# Window: Thu 2024-02-01 .. Fri 2024-03-08. The fixture chain (seeded at 2024-02-01) carries a
# 2024-03-15 expiry — AFTER this window's END — so an option opened early is HELD for the whole
# run (never expires mid-run). The seeded equity long is likewise held the whole run, so the
# engine steps EVERY weekday bar (no flat-bar jumps), giving a long stretch of no-event bars.
_START = datetime(2024, 2, 1)
_END = datetime(2024, 3, 8)


def _weekday_bars(start: date, end: date):
    """A daily OHLCV climb on weekdays only over [start, end] (the simulated trading clock)."""
    rows = []
    d = start
    px = 180.0
    while d <= end:
        if d.weekday() < 5:  # Mon..Fri
            rows.append((d, px, px + 1.0, px - 1.0, px + 0.2))
            px += 0.2
        d += timedelta(days=1)
    return rows


_PERF_BARS = _weekday_bars(_START.date(), _END.date())

# Run cadence: THURSDAYS only. The (book-dirtying) analysis + open-position management pass runs
# only on Thursdays; every other weekday is a pure no-event bar (positions held, nothing
# analysed/managed, no fill) where the order cache must NOT be invalidated.
_THURSDAY_ONLY_SCHEDULE = {
    "days": {
        "monday": False, "tuesday": False, "wednesday": False, "thursday": True,
        "friday": False, "saturday": False, "sunday": False,
    },
    "times": [],
}


class _CadencedHoldExpert(_HoldExpert):
    """Same HOLD expert, but pinned to a Thursday-only run cadence so most bars are no-event."""

    def __init__(self, id: int):
        super().__init__(id)
        # Read by DailyBacktestEngine._entry_schedule via
        # get_setting_with_interface_default("execution_schedule_enter_market").
        self.settings["execution_schedule_enter_market"] = _THURSDAY_ONLY_SCHEDULE
        # Management now runs on its OWN cadence (execution_schedule_open_positions), separate
        # from entry — it defaults to daily (mirrors live), which would defeat this test's
        # "most bars are no-event" premise. Pin it to the SAME Thursday-only cadence so the
        # perf-guard still isolates "book-dirtying only on schedule days".
        self.settings["execution_schedule_open_positions"] = _THURSDAY_ONLY_SCHEDULE


def _build_perf_engine():
    """Wire the engine/account/cache/expert like the rule-driven options e2e harness, but over the
    longer window + Thursday-only cadence, with an invalidate-counting account and a bar counter.
    Returns (engine, account, ctx, expert_id, counters). Caller MUST close ctx."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
        seed_expert_instance,
    )
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_enter_long_ruleset,
        seed_open_positions_ruleset,
    )
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    account_id = 83
    expert_id = 83

    import tempfile, os
    tmpdir = tempfile.mkdtemp(prefix="opt-perf-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    _seed_cache(cache_db)  # chain @ 2024-02-01, expiry 2024-03-15, early-Feb premium bars
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db("options-perf")
    ctx.__enter__()

    seed_account_definition(account_id, CFG)
    enter_ruleset_id = seed_enter_long_ruleset(name=f"opt-perf-enter-{account_id}")

    # The buy_call OPEN_POSITIONS rule fires on each MANAGEMENT bar (Thursdays) against the held
    # name — those are the book-dirtying EVENT bars. (~ATM, in the DTE window from the simulated
    # clock, 5% sizing — same selection as the rule-driven e2e.)
    exit_rules = [{
        "id": "opt-buy-call", "conditions": None, "action_type": "buy_call",
        "option_strike_method": "percent_otm", "option_strike_param": 0.0,
        "option_dte_min": 30, "option_dte_max": 60, "option_sizing": 5.0, "enabled": True,
    }]
    open_ruleset_id = seed_open_positions_ruleset(exit_rules, name=f"opt-perf-open-{account_id}")

    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_CadencedHoldExpert",
        enter_market_ruleset_id=enter_ruleset_id,
        open_positions_ruleset_id=open_ruleset_id,
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _underlying_rows(_PERF_BARS))
    ps.set_clock(_START)

    # Invalidate-counting account: a thin subclass that tallies every invalidate_order_cache call
    # WITHOUT altering behaviour (it always delegates to the real implementation). snapshot_equity
    # is called exactly ONCE per processed bar (after fills roll), so it is the bar counter.
    counters = {"invalidate": 0, "bars": 0}

    class _CountingAccount(BacktestAccount):
        def invalidate_order_cache(self) -> None:  # type: ignore[override]
            counters["invalidate"] += 1
            return super().invalidate_order_cache()

        def snapshot_equity(self, as_of):  # type: ignore[override]
            counters["bars"] += 1
            return super().snapshot_equity(as_of)

    account = _CountingAccount(account_id, ps, CFG, options_provider=provider)
    resolver.register_account(account_id, account)

    # Held EQUITY long (filled entry + OPENED txn) so the engine steps every bar (no flat-skip)
    # AND _manage_open_positions has a position to evaluate the buy_call rule against.
    _seed_held_equity_position(account, expert_id, qty=100, entry_px=180.0, open_date=_START)

    expert = _CadencedHoldExpert(expert_id)
    resolver.register_expert(expert_id, expert)

    config = {
        "start_date": _START,
        "end_date": _END,
        "enabled_instruments": ["AAPL"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, expert.settings, enter_ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=object(),
    )
    return engine, account, ctx, expert_id, counters


def test_option_run_reuses_order_cache_no_per_bar_churn():
    """An OPTION run invalidates the in-memory order cache only on EVENT bars, NOT every bar —
    protecting the in-memory-order-cache speedup for option runs."""
    engine, account, ctx, expert_id, counters = _build_perf_engine()
    try:
        engine.run()

        total_bars = counters["bars"]
        invalidate_calls = counters["invalidate"]

        # The run must have been MEANINGFUL: an option actually opened (so the run exercised the
        # real option fill/mark/hold path, not an empty loop) — otherwise the perf assertion is
        # vacuous.
        opt_positions = account.get_option_positions()
        assert opt_positions, (
            "expected the buy_call rule to have FIRED + FILLED an option off the cache; "
            "without a held option the perf assertion would be vacuous."
        )
        occ = {p.contract_symbol for p in opt_positions}
        assert _OCC_180 in occ, f"expected the ~ATM strike-180 call selected, got {occ}"

        # The window must be long enough that the assertion is non-trivial: clearly more bars
        # than the handful of cadence (Thursday) event bars.
        assert total_bars >= 15, (
            f"perf fixture too short to be meaningful (only {total_bars} bars); widen the window"
        )

        # THE CONTRACT: the order cache is invalidated STRICTLY FEWER times than there are bars —
        # i.e. it is NOT invalidated on every bar. A regression to an unconditional per-bar
        # invalidate would make invalidate_calls >= total_bars and this fails.
        assert invalidate_calls < total_bars, (
            f"order cache invalidated on (nearly) every bar: {invalidate_calls} invalidate calls "
            f"for {total_bars} bars — per-bar DB churn regression (lost the in-memory cache reuse)."
        )

        # And the gap must be REAL: a clear majority of bars were no-event bars that did NOT
        # invalidate, so the guard cannot pass trivially.
        no_op_bars = total_bars - invalidate_calls
        assert no_op_bars >= total_bars // 2, (
            f"expected a clear majority of bars to be no-op (no invalidate); got {no_op_bars} "
            f"no-op of {total_bars} bars ({invalidate_calls} invalidates) — gap too small."
        )
    finally:
        ctx.__exit__(None, None, None)
