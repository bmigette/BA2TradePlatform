"""GRID 2, END TO END THROUGH ``DailyBacktestEngine.run()`` (plan Task 10, amendment 6).

Everything else about grid 2 is pinned at the unit level: the gene tables in
``tests/test_option_grid_foundations.py``, the stamp contract in
``packages/common/tests/test_earnings_stamp.py``, the builders in
``packages/common/tests/test_backspread_builders.py``, the BS mark fallback in
``tests/backtest/test_bs_mark_fallback.py``. Each of those proves ONE link. Neither of the
two chains below had ever been run END TO END, and both are chains where every link can be
individually correct while the whole thing does nothing:

  (a) ``O_ERN``. The expert stamps ``days_to_earnings``/``event_date`` onto a recommendation
      -> the ENTRY rule's ``rec_days_to_earnings <= X`` leaf reads that stamp back and fires
      -> a straddle is submitted, and the submit path carries the EVENT DATE forward onto the
      entry order's ``data`` -> the EXIT rule's ``days_after_event >= Y`` reads it off THAT
      ORDER (never off the recommendation in hand, which by exit time is a later one) and
      closes the position. Four different modules, three different persisted rows, one
      contract. A unit test on any one of them passes with the chain broken.

  (b) ``O_LEAPC`` at LEAPS-range bar SPARSITY. Design section 1 measured bar density at
      50-65% of trading days out at LEAPS range -- so on roughly half the bars the position
      has NO premium bar to mark against, and the Black-Scholes fallback (plan Task 3) is
      what stops the mark going stale. Nothing tested that fallback on a DTE >= 365 expiry
      inside a real run, and nothing tested that the DTE-floor exit -- whose band this task
      moved from 0-21 to 90-240 precisely because a LEAPS position can never reach 21 DTE
      inside a grid window -- actually fires.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_grid2_engine_paths.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation

# tests/backtest/ -> tests/ -> backend/ -> testplatform/, then the launcher beside backend/.
_LAUNCHER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_grid2_engine", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_grid2_engine"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}


# --------------------------------------------------------------------------- #
# Shared harness
# --------------------------------------------------------------------------- #
def _bars(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1_000_000}
            for (d, o, h, lo, c) in rows]


class _StampingExpert(MarketExpertInterface):
    """A BUY expert that stamps an earnings payload the way ``FMPEarningsEvent`` does.

    Deliberately NOT the real expert: this test is about the ENGINE chain, and the real
    expert would drag in the FMP disk cache, its feature math and its own coverage gates --
    three more ways for the test to fail for reasons that are not the chain. What it must
    reproduce EXACTLY is the stamp contract (``ba2_common.core.earnings_stamp``), because
    that is the interface the chain is built on; it does so through the module's own
    constants rather than by repeating the strings, so a rename cannot leave this test
    passing against a contract nothing else uses.

    ``raw_outputs`` rather than ``data``: ``daily_engine`` copies raw_outputs wholesale onto
    the persisted ``ExpertRecommendation.data``, which is where the conditions read.
    """

    bypasses_classic_rm = False

    def __init__(self, id: int, event_day: date, symbol: str):
        super().__init__(id)
        self._settings_cache = {}
        self._event_day = event_day
        self._symbol = symbol

    @classmethod
    def description(cls) -> str:
        return "Stub earnings-event expert for the grid-2 engine chain test."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        from ba2_common.core.earnings_stamp import (
            DAYS_TO_EARNINGS_KEY, EARNINGS_STAMP_NAMESPACE, EVENT_DATE_KEY)
        today = as_of.date() if hasattr(as_of, "date") else as_of
        try:
            price = context.account.get_instrument_current_price(self._symbol)
        except Exception:  # noqa: BLE001
            price = None
        return Recommendation(
            signal=OrderRecommendation.BUY,
            confidence=90.0,
            current_price=price if price is not None else 100.0,
            details="earnings event ahead",
            expected_profit_percent=25.0,
            raw_outputs={EARNINGS_STAMP_NAMESPACE: {
                DAYS_TO_EARNINGS_KEY: (self._event_day - today).days,
                EVENT_DATE_KEY: self._event_day.isoformat(),
            }},
        )


class _PlainBuyExpert(MarketExpertInterface):
    """A BUY expert with no earnings stamp -- the LEAPS arm's driver."""

    bypasses_classic_rm = False

    def __init__(self, id: int, symbol: str):
        super().__init__(id)
        self._settings_cache = {}
        self._symbol = symbol

    @classmethod
    def description(cls) -> str:
        return "Stub BUY expert for the grid-2 LEAPS engine chain test."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        try:
            price = context.account.get_instrument_current_price(self._symbol)
        except Exception:  # noqa: BLE001
            price = None
        return Recommendation(
            signal=OrderRecommendation.BUY, confidence=90.0,
            current_price=price if price is not None else 100.0,
            details="buy", expected_profit_percent=25.0, raw_outputs={},
        )


def _strip_unanswerable_gates(rules, keep_fields):
    """Drop every entry leaf whose field this fixture cannot answer.

    The grid's entry rule carries iv_rank / iv_to_realized_vol / relative_volume /
    expected_profit / a directional signal flag beside the leaf under test. All of them are
    ``toggle_optimize=True`` -- i.e. the GA may switch them off -- and every one of them
    fails CLOSED on a fixture with 5 option bars and no IV history, so leaving them in would
    make the run trade nothing for reasons that have nothing to do with the chain being
    tested. Dropping them is exactly what a genome that disables them does
    (``strategy_param_space._apply_to_tree`` DELETES the node), so this is a real point in
    the searched space, not a bypass: the leaf under test and the structural
    ``has_no_position`` guard both stay.
    """
    import copy
    out = copy.deepcopy(rules)
    for rule in out:
        conds = (rule.get("conditions") or {}).get("conditions")
        if not conds:
            continue
        rule["conditions"]["conditions"] = [
            c for c in conds if c.get("field") in keep_fields]
    return out


def _harness(*, symbol, underlying_rows, chain_rows, bar_rows, entry_rules, exit_rules,
             entry_action, expert_factory, start, end, account_id, cfg=None):
    """Wire a full engine over a fixture options cache. Returns (engine, account, ctx)."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_entry_ruleset_from_rules, seed_exit_ruleset_from_rules)
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    cfg = cfg or CFG
    tmpdir = tempfile.mkdtemp(prefix="grid2-engine-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows(symbol, start.date().isoformat(), chain_rows)
    cache.write_bar_rows(bar_rows)
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"grid2-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, cfg)
    enter_id = seed_entry_ruleset_from_rules(entry_rules, name=f"grid2-enter-{account_id}")
    open_id = seed_exit_ruleset_from_rules(exit_rules, name=f"grid2-open-{account_id}")
    seed_expert_instance(account_id=account_id, expert_class_name="_Grid2Expert",
                         enter_market_ruleset_id=enter_id,
                         open_positions_ruleset_id=open_id, instance_id=account_id)

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(symbol, _bars(underlying_rows))
    ps.set_clock(start)

    account = BacktestAccount(account_id, ps, cfg, options_provider=provider)
    resolver.register_account(account_id, account)
    expert = expert_factory(account_id)
    resolver.register_expert(account_id, expert)

    config = {"start_date": start, "end_date": end, "enabled_instruments": [symbol],
              "seed": 42, "entry_action": entry_action}
    engine = DailyBacktestEngine(
        account=account, experts=[(expert, account_id, expert.settings, enter_id)],
        price_source=ps, config=config, indicator_provider=object())
    return engine, account, ctx


# --------------------------------------------------------------------------- #
# (a) O_ERN: stamp -> entry gate -> straddle -> days_after_event exit
# --------------------------------------------------------------------------- #
_ERN_SYMBOL = "ERNX"
_ERN_START = datetime(2024, 2, 1)
_ERN_END = datetime(2024, 2, 16)
_ERN_EVENT = date(2024, 2, 9)          # the print, a Friday
_ERN_EXPIRY = date(2024, 2, 23)        # 22 days out at the run start -> inside [7,21]+ window
_ERN_CALL = "ERNX240223C00100000"
_ERN_PUT = "ERNX240223P00100000"

# One bar per weekday. Flat at 100 so the ATM straddle picks the 100 strike on every bar.
_ERN_DAYS = [date(2024, 2, d) for d in
             (1, 2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]
_ERN_UNDERLYING = [(d, 100.0, 101.0, 99.0, 100.0) for d in _ERN_DAYS]


def _ern_chain():
    return [
        {"occ_symbol": _ERN_CALL, "option_type": "call", "strike": 100.0,
         "expiry": _ERN_EXPIRY.isoformat(), "bid": 3.0, "ask": 3.0, "last": 3.0,
         "iv": 0.60, "delta": 0.50, "open_interest": 5000},
        {"occ_symbol": _ERN_PUT, "option_type": "put", "strike": 100.0,
         "expiry": _ERN_EXPIRY.isoformat(), "bid": 3.0, "ask": 3.0, "last": 3.0,
         "iv": 0.60, "delta": -0.50, "open_interest": 5000},
    ]


def _ern_bars():
    rows = []
    for occ, right in ((_ERN_CALL, "call"), (_ERN_PUT, "put")):
        for d in _ERN_DAYS:
            rows.append({"occ_symbol": occ, "date": d.isoformat(), "open": 3.0, "high": 3.2,
                         "low": 2.8, "close": 3.0, "volume": 500, "underlying": _ERN_SYMBOL,
                         "option_type": right, "strike": 100.0,
                         "expiry": _ERN_EXPIRY.isoformat()})
    return rows


def _ern_rules(m):
    """O_ERN's OWN launcher-built entry/exit rules, with the fixture-unanswerable gates
    dropped and the two timing thresholds pinned to concrete searched levels."""
    from ba2_common.core.rule_models import normalize_trade_rules, trade_rules_from_legacy

    entry = _strip_unanswerable_gates(
        normalize_trade_rules([m._option_entry_rule("O_ERN")]),
        keep_fields={"has_no_position", "rec_days_to_earnings"})
    # X = 3 days before the print (searched band 1-5).
    for leaf in entry[0]["conditions"]["conditions"]:
        if leaf.get("field") == "rec_days_to_earnings":
            leaf["value"] = 3
    exits = trade_rules_from_legacy(
        exit_conditions=m._option_exit_rules("O_ERN"))["exit_rules"]
    # Keep ONLY the event exit: the point is that IT closes the position, and leaving the
    # TP / elapsed-time / DTE exits armed would let one of them close it first and the test
    # would pass without the chain working. Each of those carries its own on/off gene, so a
    # genome with only the event exit live is a real point in the searched space.
    exits = [r for r in exits if r.get("id") == "opt_event"]
    assert exits, "O_ERN must emit the opt_event exit rule"
    # Y = 1 day after the print (searched band 0-2).
    for leaf in exits[0]["conditions"]["conditions"]:
        if leaf.get("field") == "days_after_event":
            leaf["value"] = 1
    return entry, exits


def test_o_ern_runs_the_whole_chain_through_the_engine():
    """Stamp -> entry gate -> straddle submitted -> days_after_event exit closes it."""
    m = _launcher()
    entry_rules, exit_rules = _ern_rules(m)
    entry_action = entry_rules[0]["actions"][0]
    engine, account, ctx = _harness(
        symbol=_ERN_SYMBOL, underlying_rows=_ERN_UNDERLYING, chain_rows=_ern_chain(),
        bar_rows=_ern_bars(), entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_action,
        expert_factory=lambda eid: _StampingExpert(eid, _ERN_EVENT, _ERN_SYMBOL),
        start=_ERN_START, end=_ERN_END, account_id=771)
    try:
        engine.run()

        from ba2_common.core.earnings_stamp import ORDER_EVENT_DATE_KEY
        from ba2_common.core.trade_store import orders_where
        from ba2_common.core.types import OrderStatus

        # A multi-leg structure is persisted as a PARENT order (no contract_symbol of its
        # own, carrying the strategy name) plus one CHILD per leg -- the shape
        # test_multileg_net_limit_and_close pins.
        all_orders = orders_where(account_id=771)
        parents = [o for o in all_orders
                   if o.parent_order_id is None and not o.contract_symbol
                   and o.option_strategy]
        assert parents, (
            "the O_ERN entry never fired: the stamped rec_days_to_earnings gate did not "
            "admit a bar, or the straddle was never submitted")
        parent = parents[0]
        assert parent.option_strategy == "straddle", (
            f"expected the straddle builder, got {parent.option_strategy!r}")

        # LINK 1: the structure is a STRADDLE -- both rights, filled.
        children = [o for o in all_orders if o.parent_order_id == parent.id]
        rights = {str(c.option_type.value if hasattr(c.option_type, "value")
                      else c.option_type).lower() for c in children}
        assert {"call", "put"} <= rights, f"expected a straddle (call+put), got {rights}"
        assert any(c.status == OrderStatus.FILLED for c in children), (
            "the straddle was submitted but never filled off the fixture bars")

        # LINK 2: the ENTRY carried the event date forward onto its own row. Without this
        # the exit has nothing to read -- the recommendation in hand at exit time is a
        # later one, stamped with a different distance.
        assert isinstance(parent.data, dict), "entry order carries no data dict"
        assert parent.data.get(ORDER_EVENT_DATE_KEY) == _ERN_EVENT.isoformat(), (
            f"entry order must carry the event date forward for the exit; got "
            f"{parent.data.get(ORDER_EVENT_DATE_KEY)!r}")

        # LINK 3: the position is CLOSED, and by the event exit -- the only exit left armed.
        assert not account.get_option_positions(), (
            "days_after_event >= 1 should have closed the straddle after the 2024-02-09 "
            "print; the position is still open")
    finally:
        ctx.__exit__(None, None, None)


def test_o_ern_entry_never_fires_without_the_stamp():
    """THE OTHER HALF of link 1, and the reason the gate is worth having: the SAME rules,
    the same chain, an expert that stamps NOTHING -> no entry at all.

    Absence must not read as zero. An unstamped ``days_to_earnings`` treated as 0 would
    satisfy ``<= 3`` for every symbol on every bar, and the strategy would buy a straddle on
    everything while looking timed -- passing the test above for entirely the wrong reason.
    """
    m = _launcher()
    entry_rules, exit_rules = _ern_rules(m)
    engine, account, ctx = _harness(
        symbol=_ERN_SYMBOL, underlying_rows=_ERN_UNDERLYING, chain_rows=_ern_chain(),
        bar_rows=_ern_bars(), entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_rules[0]["actions"][0],
        expert_factory=lambda eid: _PlainBuyExpert(eid, _ERN_SYMBOL),
        start=_ERN_START, end=_ERN_END, account_id=772)
    try:
        engine.run()
        from ba2_common.core.trade_store import orders_where
        opts = [o for o in orders_where(account_id=772) if o.contract_symbol]
        assert not opts, (
            f"an expert that stamps no earnings payload must never satisfy "
            f"rec_days_to_earnings <= 3, but {len(opts)} option order(s) were placed")
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# (b) O_LEAPC: sparse bars -> BS-fallback marks -> the DTE-floor exit fires
# --------------------------------------------------------------------------- #
_LEAP_SYMBOL = "LEAPX"
_LEAP_START = datetime(2024, 1, 2)
_LEAP_END = datetime(2024, 3, 15)
# Expiry 380 days past the START, so the entry sits inside O_LEAPC's decoded DTE window and
# the position crosses a DTE floor of 320 partway through the run.
_LEAP_EXPIRY = _LEAP_START.date() + timedelta(days=380)
_LEAP_CALL = "LEAPX250116C00080000"


def _leap_days():
    d, out = _LEAP_START.date(), []
    while d <= _LEAP_END.date():
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


_LEAP_DAYS = _leap_days()
_LEAP_UNDERLYING = [(d, 100.0, 101.0, 99.0, 100.0) for d in _LEAP_DAYS]

#: ~50% BAR DENSITY, the density design section 1 MEASURED at LEAPS range ("50-65% of
#: trading days"). Bars come in CONSECUTIVE PAIRS (days 0,1 then 4,5 then 8,9 ...) rather
#: than on alternate days, and that is not cosmetic: the default ``next_bar_open`` fill
#: model needs the bar AFTER the one the order was placed on, and a strict every-other-day
#: pattern gives it one exactly never -- the entry is placed and DAY-expires unfilled on
#: every single bar, which is a real (and worth knowing) sparsity failure mode but not the
#: MARKING path this test exists to cover. Half the days still carry no bar at all, so the
#: mark falls through to Black-Scholes on the bar iv on every one of them.
_LEAP_BAR_DAYS = [d for i, d in enumerate(_LEAP_DAYS) if i % 4 in (0, 1)]


def _leap_chain():
    return [{"occ_symbol": _LEAP_CALL, "option_type": "call", "strike": 80.0,
             "expiry": _LEAP_EXPIRY.isoformat(), "bid": 25.0, "ask": 25.0, "last": 25.0,
             "iv": 0.30, "delta": 0.80, "open_interest": 5000}]


def _leap_bars():
    return [{"occ_symbol": _LEAP_CALL, "date": d.isoformat(), "open": 25.0, "high": 25.5,
             "low": 24.5, "close": 25.0, "volume": 500, "underlying": _LEAP_SYMBOL,
             "option_type": "call", "strike": 80.0, "expiry": _LEAP_EXPIRY.isoformat(),
             "iv": 0.30, "delta": 0.80}
            for d in _LEAP_BAR_DAYS]


def _leap_rules(m, *, dte_floor):
    from ba2_common.core.rule_models import normalize_trade_rules, trade_rules_from_legacy

    entry = _strip_unanswerable_gates(
        normalize_trade_rules([m._option_entry_rule("O_LEAPC")]),
        keep_fields={"has_no_position"})
    # The DELTA method with the chain's own 0.80 delta, and a DTE window the fixture expiry
    # falls inside. Both are decoded values a real genome produces (delta band 0.70-0.90,
    # DTE centres 410-500 with a +/-45 half-width); pinned here so the test does not depend
    # on which level the GA happens to sample.
    action = entry[0]["actions"][0]
    action["option_strike_param"] = 0.80
    action["option_dte_min"], action["option_dte_max"] = 300, 420
    # ENTRY rule per ARM (O_LEAPC), EXIT rules per LAUNCHED KEY (O_LEAP): that split is the
    # group shape itself -- two toggleable entry rules over one shared exit ruleset -- and
    # asking _option_exit_rules for the member would silently hand back the platform DEFAULT
    # 0..21 DTE band instead of the design's 90-240.
    exits = trade_rules_from_legacy(
        exit_conditions=m._option_exit_rules("O_LEAP"))["exit_rules"]
    exits = [r for r in exits if r.get("id") == "opt_dte"]
    assert exits, "O_LEAP must emit the opt_dte exit rule"
    for leaf in exits[0]["conditions"]["conditions"]:
        if leaf.get("field") == "days_to_expiry":
            leaf["value"] = dte_floor
    return entry, exits


def _run_leap(dte_floor, account_id):
    m = _launcher()
    entry_rules, exit_rules = _leap_rules(m, dte_floor=dte_floor)
    return _harness(
        symbol=_LEAP_SYMBOL, underlying_rows=_LEAP_UNDERLYING, chain_rows=_leap_chain(),
        bar_rows=_leap_bars(), entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_rules[0]["actions"][0],
        expert_factory=lambda eid: _PlainBuyExpert(eid, _LEAP_SYMBOL),
        start=_LEAP_START, end=_LEAP_END, account_id=account_id)


def test_o_leapc_is_marked_on_bars_the_chain_does_not_cover():
    """A LEAPS position at ~50% bar density is MARKED on every bar of the run.

    The equity curve is sampled on every trading day; if a barless day left the option
    unmarked the curve would carry a hole (a NaN, or a silent carry of a stale value that
    ``_clamp_premium_to_no_arb`` never saw). The DTE floor is set BELOW anything the run can
    reach, so the position stays open for the whole window and every barless day is one the
    mark had to answer for.
    """
    engine, account, ctx = _run_leap(dte_floor=10, account_id=773)
    try:
        engine.run()
        assert account.get_option_positions(), (
            "the O_LEAPC entry never fired: the 0.80-delta pick or the 300-420 DTE window "
            "found no contract on the fixture chain")
        # One net-liquidating-value snapshot per simulated bar -- the same series the golden
        # equity run fingerprints, read off the account rather than a results key.
        hist = account.get_balance_history()
        assert len(hist) >= len(_LEAP_BAR_DAYS), (
            f"expected an equity snapshot per simulated bar, got {len(hist)}")
        vals = [float(h["net_liquidating_value"]) for h in hist]
        assert all(v == v for v in vals), "an unmarked option left a NaN in the equity curve"
        assert all(v > 0 for v in vals), "an unmarked option zeroed the equity curve"
        # THE SPARSITY ASSERTION: more than a third of the snapshots land on a day the chain
        # has NO bar for, so the mark on those days came from somewhere other than a bar.
        barless = [h for h in hist
                   if (h["date"].date() if hasattr(h["date"], "date") else h["date"])
                   not in set(_LEAP_BAR_DAYS)]
        assert len(barless) > len(hist) // 3, (
            f"the fixture is not actually sparse: only {len(barless)}/{len(hist)} snapshots "
            f"fall on a barless day, so this proves nothing about the mark fallback")

        # AND THE MARK ON A BARLESS DAY IS THE BLACK-SCHOLES ONE, not the intrinsic floor.
        # "The curve has no holes" is satisfied by a stale carry or by the
        # max(intrinsic, entry) floor the fallback chain ends in, so the value itself has to
        # be checked against the number the BS stage produces from the SAME inputs the bar
        # carries (spot 100, strike 80, iv 0.30, r 0). The floor here is 20.00 and BS is
        # ~23.7, so the two are far enough apart that the account's own net liquidating
        # value tells them apart at 2 contracts.
        from app.services.backtest.options_store import default_options_risk_free_rate
        from ba2_common.core.option_bs import bs_price
        pos = account.get_option_positions()[0]
        probe = barless[len(barless) // 2]
        probe_day = (probe["date"].date() if hasattr(probe["date"], "date")
                     else probe["date"])
        dte = (_LEAP_EXPIRY - probe_day).days
        # The SAME rate seam the mark path uses (``BacktestAccount._bs_mark_rate``), not a
        # literal: a rate written twice is a rate that can disagree with itself.
        bs = bs_price(100.0, 80.0, dte, 0.30, "call", r=default_options_risk_free_rate())
        intrinsic = 20.0
        mv = float(probe["net_liquidating_value"]) - float(probe["cash_balance"])
        per_contract = mv / (pos.quantity * 100.0)
        assert abs(per_contract - bs) < 0.05, (
            f"a barless day marked at {per_contract:.4f}/contract, but BS on the bar's own "
            f"iv says {bs:.4f} -- the mark did not come from the Black-Scholes stage")
        assert abs(per_contract - intrinsic) > 1.0, (
            f"a barless day marked at the intrinsic floor ({intrinsic}) -- the BS stage was "
            f"skipped and the position is being carried at its worst case")
        # The fixture is deliberately FLAT (spot 100, premium 25 on every bar it has), so a
        # working BS fallback and a working bar read agree closely -- what would show up is
        # a mark that collapsed to intrinsic (20.0) or to zero on the barless days, which
        # over ~half the run would drag the curve visibly below its start.
        assert min(vals) > CFG["starting_cash"] * 0.90, (
            f"the marks moved the account by more than the flat fixture can justify "
            f"(min {min(vals):.2f} vs start {CFG['starting_cash']:.2f}) -- the barless "
            f"days are being marked at intrinsic or zero, not by the BS fallback")
    finally:
        ctx.__exit__(None, None, None)


def test_o_leapc_dte_floor_exit_fires_inside_the_grid_window():
    """THE POINT OF THE 90-240 BAND. The same run with a floor the position DOES cross
    closes it; the run above, with the OLD default band's ceiling (21), never can.

    A LEAPS position opened at ~380 DTE reaches 21 DTE more than a year later -- past the
    end of any grid job's window -- so on the launcher's default ``opt_dte`` band the exit
    was not a loose gene, it was an exit that could never fire. 320 is inside the band this
    task gave the key (90-240 in calendar days at the searched levels; the fixture uses a
    shorter horizon than a real job so the crossing happens inside a runnable window).
    """
    engine, account, ctx = _run_leap(dte_floor=320, account_id=774)
    try:
        engine.run()
        assert not account.get_option_positions(), (
            "days_to_expiry <= 320 should have closed the LEAPS call partway through the "
            "run; the position is still open")
        from ba2_common.core.trade_store import orders_where
        assert [o for o in orders_where(account_id=774) if o.contract_symbol], (
            "no option order at all -- the entry never fired, so this proves nothing "
            "about the exit")
    finally:
        ctx.__exit__(None, None, None)


# Keyed on the LAUNCHABLE key: _option_exit_rules is called with the launched kind, and the
# two LEAPS arms share ONE exit ruleset under the group key O_LEAP (merge, 2026-09-02).
@pytest.mark.parametrize("kind,expected", [("O_LEAP", (90, 240)),
                                           ("O_CBS", (20, 45)), ("O_PBS", (20, 45))])
def test_the_dte_floor_band_is_the_designs(kind, expected):
    """The band the exit above depends on, read off the emitted rule rather than the table."""
    m = _launcher()
    rule = next(r for r in m._option_exit_rules(kind) if r["id"] == "opt_dte")
    leaf = rule["conditions"]["conditions"][0]
    assert (leaf["value_min"], leaf["value_max"]) == expected
