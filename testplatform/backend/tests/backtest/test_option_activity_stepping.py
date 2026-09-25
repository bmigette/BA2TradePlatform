"""A HELD OPTION POSITION IS ACTIVITY: the engine steps daily while an option lot is open.

THE BUG (findings 2026-09-24 section 5.1, bug 3)
-----------------------------------------------
``DailyBacktestEngine.run`` advances with ``i += 1`` while ``_has_activity()`` says a fill is
still possible, and otherwise JUMPS to the next analysis (entry-schedule) bar -- the
skip-flat-bars optimisation. ``_has_activity`` asked ``account.get_positions()``, which is the
EQUITY ledger only: option lots live in ``BacktestAccount._option_positions`` and never appear
there. So an option-only book with no working order looked FLAT, and under a non-daily entry
schedule the loop jumped straight from one entry day to the next while the lot was open:

  * the open-positions (manage) pass never ran on the skipped days, so an exit that was due
    on a Wednesday was only evaluated the following Monday;
  * the equity curve was sampled on entry days only (the Tue-Fri marks simply did not exist);
  * expiry settled LATE: a contract expiring on a Friday settled on the next visited bar, at
    THAT bar's spot (the AAPL 2020-08-28 put settled Mon 8/31 at the post-split price).

Live evaluates open positions on its own (daily) cadence, independent of the entry schedule,
so this was a backtest/live parity break for every option genome that does not enter daily
(the deployed 8082 genome enters Mon/Tue/Fri).

THE HARNESS is ``test_grid2_engine_paths._harness`` -- the real ``DailyBacktestEngine.run()``
over a fixture options cache, with the launcher's OWN O_LC entry/exit rules -- so what is
pinned is the real advance logic, not a re-implementation of it. The cadences are the
optimizer's own shape: ``run_schedule_override`` (entry, Monday-only here) and
``manage_schedule_override`` (open-positions management, every weekday -- what the GA drives
and what live schedules).

Run from the backend dir:
    python -m pytest tests/backtest/test_option_activity_stepping.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from tests.backtest.test_grid2_engine_paths import (
    _PlainBuyExpert, _harness, _launcher, _strip_unanswerable_gates)

_SYMBOL = "STEPX"

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
#: Entry on MONDAYS ONLY -- the cadence under which the old advance jumped Mon -> Mon.
_MONDAY_ONLY = {"days": {wd: wd == "monday" for wd in _WEEKDAYS}}
#: Open-positions management every weekday (the GA's manage override, and live's cadence).
_EVERY_WEEKDAY = {"days": {wd: wd not in ("saturday", "sunday") for wd in _WEEKDAYS}}


def _weekdays(start: date, end: date):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _o_lc_rules(*, dte_min, dte_max, days_opened_gt=None):
    """O_LC's OWN launcher-built entry rule (fixture-unanswerable gates dropped, exactly what a
    genome that switches them off produces) and -- when asked -- ONLY its ``opt_time`` exit at a
    concrete ``days_opened`` threshold. The TP / DTE / max-loss exits are dropped so nothing but
    the elapsed-time exit can close the position (each carries its own on/off gene, so this is
    a real point in the searched space)."""
    from ba2_common.core.rule_models import normalize_trade_rules, trade_rules_from_legacy

    m = _launcher()
    entry = _strip_unanswerable_gates(
        normalize_trade_rules([m._option_entry_rule("O_LC")]),
        keep_fields={"has_no_position"})
    action = entry[0]["actions"][0]
    action["option_dte_min"], action["option_dte_max"] = dte_min, dte_max
    exits = trade_rules_from_legacy(
        exit_conditions=m._option_exit_rules("O_LC"))["exit_rules"]
    exits = [r for r in exits if r.get("id") == "opt_time"]
    assert exits, "O_LC must emit the opt_time exit rule"
    if days_opened_gt is None:
        # Never fires inside the fixture window: the position can only leave by expiry.
        days_opened_gt = 365
    for leaf in exits[0]["conditions"]["conditions"]:
        if leaf.get("field") == "days_opened":
            leaf["value"] = days_opened_gt
    return entry, exits, action


def _run(*, start, end, underlying_rows, chain_rows, bar_rows, entry, exits, action,
         account_id):
    engine, account, ctx = _harness(
        symbol=_SYMBOL, underlying_rows=underlying_rows, chain_rows=chain_rows,
        bar_rows=bar_rows, entry_rules=entry, exit_rules=exits, entry_action=action,
        expert_factory=lambda eid: _PlainBuyExpert(eid, _SYMBOL),
        start=start, end=end, account_id=account_id)
    engine.config["run_schedule_override"] = _MONDAY_ONLY
    engine.config["manage_schedule_override"] = _EVERY_WEEKDAY
    return engine, account, ctx


def _day(x) -> date:
    return x.date() if hasattr(x, "date") else x


def _curve_days(account):
    return [_day(h["date"]) for h in account.get_balance_history()]


# --------------------------------------------------------------------------- #
# (1) An elapsed-time exit due mid-week closes mid-week, not the next Monday.
# --------------------------------------------------------------------------- #
_EX_START = datetime(2024, 4, 1)    # Monday (a holiday-free window: every weekday is a session)
_EX_END = datetime(2024, 4, 19)     # Friday, three weeks later
_EX_DAYS = _weekdays(_EX_START.date(), _EX_END.date())
_EX_EXPIRY = date(2024, 6, 21)      # ~81 DTE at the start: far from any expiry effect
_EX_CALL = "STEPX240621C00100000"


def _exit_fixture():
    under = [(d, 100.0, 101.0, 99.0, 100.0) for d in _EX_DAYS]
    chain = [{"occ_symbol": _EX_CALL, "option_type": "call", "strike": 100.0,
              "expiry": _EX_EXPIRY.isoformat(), "bid": 5.0, "ask": 5.0, "last": 5.0,
              "iv": 0.30, "delta": 0.50, "open_interest": 5000}]
    # A premium bar on EVERY day (so the fills and the marks are all real bars) and a premium
    # that moves day to day, so each held day's mark is a distinct number. Each bar OPENS at
    # the previous close: the entry is a DAY limit at the decision session's close, and it must
    # be marketable at the next open or it expires unfilled.
    closes = [round(5.0 + 0.1 * i, 4) for i in range(len(_EX_DAYS))]
    bars = [{"occ_symbol": _EX_CALL, "date": d.isoformat(),
             "open": closes[i - 1] if i else closes[i], "high": round(closes[i] + 0.3, 4),
             "low": round((closes[i - 1] if i else closes[i]) - 0.2, 4), "close": closes[i],
             "volume": 500, "underlying": _SYMBOL, "option_type": "call",
             "strike": 100.0, "expiry": _EX_EXPIRY.isoformat(), "iv": 0.30, "delta": 0.50}
            for i, d in enumerate(_EX_DAYS)]
    return under, chain, bars


def test_a_held_option_is_managed_on_non_entry_days():
    """Monday-only entry, daily management, ``days_opened > 1``: the exit is decided on the
    first session the position is more than a day old and closes THAT WEEK -- not on the next
    Monday, which is all the old advance logic ever visited."""
    under, chain, bars = _exit_fixture()
    entry, exits, action = _o_lc_rules(dte_min=30, dte_max=90, days_opened_gt=1)
    engine, account, ctx = _run(start=_EX_START, end=_EX_END, underlying_rows=under,
                                chain_rows=chain, bar_rows=bars, entry=entry, exits=exits,
                                action=action, account_id=9501)
    try:
        engine.run()
        trips = [t for t in account.get_round_trip_trades()
                 if t.get("contract_symbol") == _EX_CALL]
        assert trips, "the O_LC entry never fired on the fixture chain"
        first = sorted(trips, key=lambda t: t["entry_time"])[0]
        opened, closed = _day(first["entry_time"]), _day(first["exit_time"])
        assert first["exit_reason"] != "open_at_end", f"the time exit never fired: {first}"
        # THE ENGINE'S TIMELINE CONVENTION: an order decided on bar D fills during D's own
        # fill step at the NEXT session's open (``next_bar_open``) and is STAMPED D -- the
        # decision session label. So the Monday 4/1 entry carries open_date Mon 4/1.
        assert opened == date(2024, 4, 1), (
            f"fixture timeline moved: the Monday 4/1 entry should be stamped 4/1, got {opened}")
        # ``days_opened`` is (bar - open_date) in days: Tue 4/2 = 1.0 (not > 1), Wed 4/3 = 2.0.
        # So the exit is decided on WEDNESDAY and stamped Wednesday. The old advance logic
        # never visited Tue-Fri while the lot was held, so it closed on the NEXT MONDAY (4/8).
        assert closed == date(2024, 4, 3), (
            f"the days_opened > 1 exit closed on {closed}; expected Wed 2024-04-03. A close "
            "on the following Monday means the held option was not treated as activity and "
            "the engine jumped over Tue-Fri.")

        # The equity curve carries a point for EVERY session from the entry through the close,
        # not only the entry days (the bug's curve for this run was Mon 4/1, Mon 4/8, Mon 4/15).
        days = set(_curve_days(account))
        held = [d for d in _EX_DAYS if opened <= d <= closed]
        assert held == [date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)]
        missing = [d for d in held if d not in days]
        assert not missing, (
            f"no equity point on {missing} while the option lot was open -- the curve was "
            f"only sampled on entry days: {sorted(days)}")
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# (2) Expiry on a non-entry day settles ON that day, at that day's spot.
# --------------------------------------------------------------------------- #
_XP_START = datetime(2024, 3, 4)    # Monday
_XP_END = datetime(2024, 3, 22)     # Friday, two weeks after the expiry
_XP_DAYS = _weekdays(_XP_START.date(), _XP_END.date())
_XP_EXPIRY = date(2024, 3, 8)       # the FRIDAY of the entry week -- never an entry day
_XP_CALL = "STEPX240308C00100000"
#: Spot climbs a dollar a day, so every day's intrinsic is a distinct number and the day the
#: settlement read its spot on is recoverable from the settlement price.
_XP_SPOT = {d: 100.0 + i for i, d in enumerate(_XP_DAYS)}


def _expiry_fixture():
    under = [(d, _XP_SPOT[d], _XP_SPOT[d] + 0.5, _XP_SPOT[d] - 0.5, _XP_SPOT[d])
             for d in _XP_DAYS]
    chain = [{"occ_symbol": _XP_CALL, "option_type": "call", "strike": 100.0,
              "expiry": _XP_EXPIRY.isoformat(), "bid": 2.0, "ask": 2.0, "last": 2.0,
              "iv": 0.30, "delta": 0.50, "open_interest": 5000}]
    # Premium bars Mon-Thu of the expiry week only. NO bar on the expiry Friday: a long ITM
    # call is then sold to close at INTRINSIC off the settlement bar's spot
    # (``settle_single_leg_expiry``), which makes the settlement price a direct readout of
    # which day's spot was used.
    held = [d for d in _XP_DAYS if d < _XP_EXPIRY]
    # Each bar OPENS at the previous close, so the Monday DAY limit (at Monday's close) is
    # marketable at Tuesday's open and fills.
    bars = [{"occ_symbol": _XP_CALL, "date": d.isoformat(), "open": 2.0 + max(i - 1, 0),
             "high": 2.5 + i, "low": 1.8 + max(i - 1, 0), "close": 2.0 + i, "volume": 500,
             "underlying": _SYMBOL, "option_type": "call", "strike": 100.0,
             "expiry": _XP_EXPIRY.isoformat(), "iv": 0.30, "delta": 0.50}
            for i, d in enumerate(held)]
    return under, chain, bars


def test_an_option_expiring_on_a_non_entry_day_settles_that_day_at_that_days_spot():
    """A call expiring Friday under a Monday-only entry schedule settles ON Friday, at
    Friday's spot -- not on the next Monday at Monday's (the AAPL 2020-08-28 late-settlement
    class)."""
    under, chain, bars = _expiry_fixture()
    entry, exits, action = _o_lc_rules(dte_min=0, dte_max=10)
    engine, account, ctx = _run(start=_XP_START, end=_XP_END, underlying_rows=under,
                                chain_rows=chain, bar_rows=bars, entry=entry, exits=exits,
                                action=action, account_id=9502)
    try:
        engine.run()
        trips = [t for t in account.get_round_trip_trades()
                 if t.get("contract_symbol") == _XP_CALL]
        assert trips, "the O_LC entry never fired on the fixture chain"
        first = sorted(trips, key=lambda t: t["entry_time"])[0]
        # Stamped with the decision session (see test 1): the Monday entry carries Mon 3/4.
        assert _day(first["entry_time"]) == date(2024, 3, 4), (
            f"fixture timeline moved: the Monday entry should be stamped Mon 3/4: {first}")
        settled = _day(first["exit_time"])
        assert settled == _XP_EXPIRY, (
            f"the {_XP_EXPIRY} (Friday) expiry settled on {settled}. Under a Monday-only "
            "entry schedule that means the held option was not treated as activity and the "
            "engine jumped past the expiry to the next entry day.")
        friday_intrinsic = _XP_SPOT[_XP_EXPIRY] - 100.0
        next_monday_intrinsic = _XP_SPOT[date(2024, 3, 11)] - 100.0
        assert abs(float(first["exit_price"]) - friday_intrinsic) < 1e-9, (
            f"settled at {first['exit_price']}; Friday's intrinsic is {friday_intrinsic} "
            f"(next Monday's would be {next_monday_intrinsic})")
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# (3) Equity no-impact: the visited-bar sequence of an equity run is unchanged.
# --------------------------------------------------------------------------- #
def test_has_activity_is_unchanged_for_a_book_with_no_option_lots():
    """The added clause reads ONLY option lots, so an equity-only account (which never holds
    one) answers exactly as before. The full equity no-impact gate is the byte-identical
    ``test_equity_golden_run.py`` / ``test_engine_golden_regression.py`` pins; this pins the
    predicate itself on the three states that decide the advance."""
    from app.services.backtest.backtest_account import BacktestAccount, _OptionLot

    account = BacktestAccount.__new__(BacktestAccount)
    account._option_positions = {}
    assert account.has_open_option_positions() is False

    # A lot netted/settled to ZERO is retired in place (``_zero_option_lot`` keeps the object),
    # so a book of only zero lots is flat -- counting it would step densely forever after the
    # first option trade and cost every later flat stretch its skip.
    account._option_positions = {"X": _OptionLot(contract_symbol="X", qty=0.0)}
    assert account.has_open_option_positions() is False

    account._option_positions["Y"] = _OptionLot(contract_symbol="Y", qty=-1.0)  # a short
    assert account.has_open_option_positions() is True
