"""``O_CC`` END TO END THROUGH ``DailyBacktestEngine.run()``: the written call gets closed.

WHY THIS CHAIN NEEDED ITS OWN RUN (2026-09-03). Until this date the covered call O_CC writes
had NO exit in the backtest at all -- S2's exit list is entirely equity rules, so the call
rode to expiry or assignment -- while the LIVE lifecycle pass closed it at the roll window
(``LIFECYCLE_ROLL_DTE``). Live and backtest therefore ran different strategies, and the grid
systematically understated how a live O_CC sleeve behaves.

The fix is one owner in both runtimes: ``option_lifecycle.decide`` no longer emits a close
for a single-expiry structure at its expiry, and ``O_CC`` emits a ``cc_dte`` rule
(``covered_call_days_to_expiry <= N`` -> ``close_option(close_target='covered_call')``) at the
FRONT of its exit list.

IT COULD NOT BE ``opt_dte``, and this file is where that was measured. ``days_to_expiry``
reads the EVALUATED transaction, which on an equity-entry overlay key is the STOCK -- both
runtimes evaluate once per symbol against the oldest entry order -- so it is not merely wrong
here, it is INERT. The first version of this test emitted ``opt_dte`` on O_CC and the run was
byte-for-byte identical to the run with the rule DELETED: the call expired worthless both
times. Hence a repository-resolved condition (see ``CoveredCallDaysToExpiryCondition``).
Every link of that is unit-pinned -- the launcher emission and its reachability in
``test_launcher_overlay_reachability.py``, the decider in
``packages/common/tests/test_option_lifecycle.py``, the live pass in
``tests/test_option_lifecycle_service.py`` -- and every one of them can be individually
correct while the chain does nothing:

  the launcher emits an equity entry + a ``sell_covered_call`` overlay + an ``opt_dte`` close
    -> the engine buys a ROUND LOT (the overlay sizes as floor(shares/100), so an odd lot
       silently writes no contract at all)
    -> ``cc_sell`` writes ONE call against it and ``cc_guard`` stops it re-firing
    -> ``covered_call_days_to_expiry`` finds the written call THROUGH THE REPOSITORY and, at
       the DTE floor, ``close_option(close_target='covered_call')`` buys back that same
       contract -- reached only because ``cc_dte`` sits in front of ``cc_guard``, which
       halts the ruleset for the whole time a call is open
    -> the SHARES are untouched: the cover is not released by closing the call.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_covered_call_engine.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, OrderStatus, Recommendation

_LAUNCHER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_cc_engine", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_cc_engine"] = m
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

SYMBOL = "CCX"
START = datetime(2024, 1, 2)
END = datetime(2024, 3, 15)
#: A CHEAP underlying on purpose: the overlay needs a whole 100-share lot, and 100 x $20 is
#: comfortably inside any per-instrument cap the classic RM applies to a $100k account.
SPOT = 20.0

#: 35 days out — inside the [25, 45] window ``_option_overlay_action`` authors for O_CC.
CALL_EXPIRY = START.date() + timedelta(days=35)
#: 5% OTM of a $20 spot, which is the strike_param the builder authors.
CALL_STRIKE = 21.0
CALL = f"CCX{CALL_EXPIRY:%y%m%d}C00021000"
#: ``covered_call_days_to_expiry <= 7`` is O_CC's authored default, so the call is bought
#: back 28 days after it is written. Left at the default deliberately: the exit this test is
#: about must work in the ruleset a DEFAULT genome produces, not only in a tuned one.
DTE_FLOOR = 7


def _weekdays(start: date, end: date):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(START.date(), END.date())
UNDERLYING = [(d, SPOT, SPOT + 0.2, SPOT - 0.2, SPOT) for d in DAYS]


def _bars(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1_000_000}
            for (d, o, h, lo, c) in rows]


def _premium_on(day: date) -> float:
    """A flat 0.60 until the last 20 days, then a terminal decay ramp.

    Flat first for the same fill-model reason the PMCC fixture states: a premium that moves
    on day one makes the entry's own quote move under it. The decay after is what makes the
    buy-back cheaper than the credit, i.e. the covered call actually earning something."""
    left = (CALL_EXPIRY - day).days
    if left > 20:
        return 0.60
    return round(max(0.05, 0.60 * left / 20.0), 4)


def _chain_rows():
    return [{"occ_symbol": CALL, "option_type": "call", "strike": CALL_STRIKE,
             "expiry": CALL_EXPIRY.isoformat(), "bid": 0.60, "ask": 0.60, "last": 0.60,
             "iv": 0.30, "delta": 0.25, "open_interest": 5000}]


def _bar_rows():
    rows = []
    for d in DAYS:
        if d > CALL_EXPIRY:
            continue
        px = _premium_on(d)
        rows.append({"occ_symbol": CALL, "date": d.isoformat(), "open": px,
                     "high": px + 0.05, "low": max(0.01, px - 0.05), "close": px,
                     "volume": 500, "underlying": SYMBOL, "option_type": "call",
                     "strike": CALL_STRIKE, "expiry": CALL_EXPIRY.isoformat(),
                     "iv": 0.30, "delta": 0.25})
    return rows


class _PlainBuyExpert(MarketExpertInterface):
    """A BUY expert with nothing else to say — O_CC's entry gate is directional only."""

    bypasses_classic_rm = False

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "Stub BUY expert for the covered-call engine chain test."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        try:
            price = context.account.get_instrument_current_price(SYMBOL)
        except Exception:  # noqa: BLE001
            price = None
        return Recommendation(
            signal=OrderRecommendation.BUY, confidence=95.0,
            current_price=price if price is not None else SPOT,
            details="buy", expected_profit_percent=25.0, raw_outputs={})


def _strip_unanswerable_gates(rules, keep_fields):
    """Drop every entry leaf this fixture cannot answer (see the PMCC engine test's copy).

    Each of them is ``toggle_optimize=True``, i.e. a real genome may switch it off, and
    switching one off is a DELETION of the node — so this is a point in the searched space,
    not a bypass."""
    import copy
    out = copy.deepcopy(rules)
    for rule in out:
        conds = (rule.get("conditions") or {}).get("conditions")
        if not conds:
            continue
        rule["conditions"]["conditions"] = [c for c in conds if c.get("field") in keep_fields]
    return out


def _rules(m, *, drop_dte: bool = False):
    """O_CC's OWN launcher-built rules. ``drop_dte`` is the mutation lever."""
    strat = m._build_strategy("O_CC", "cc-engine", "FMPRating")
    entry = _strip_unanswerable_gates(
        list(strat.entry_rules or []), keep_fields={"has_no_position", "bullish"})
    exits = list(strat.exit_rules or [])
    # Keep the overlay pair and the DTE close. The equity closes and the stop-adjusters each
    # carry their own on/off gene (except the floor stop, which is left in place because it
    # is the list's always-matching tail and dropping it would change the walk this test is
    # about), so a genome with only these live is a real point in the searched space.
    keep = {"cc_guard", "cc_sell", "cc_dte", "exit_stoploss"}
    exits = [r for r in exits if r.get("id") in keep]
    if drop_dte:
        exits = [r for r in exits if r.get("id") != "cc_dte"]
    else:
        assert [r["id"] for r in exits][:3] == ["cc_dte", "cc_guard", "cc_sell"], (
            f"cc_dte must precede the guard or it can never fire: "
            f"{[r['id'] for r in exits]}")
    return entry, exits


def _harness(*, entry_rules, exit_rules, account_id):
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

    tmpdir = tempfile.mkdtemp(prefix="cc-engine-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows(SYMBOL, START.date().isoformat(), _chain_rows())
    cache.write_bar_rows(_bar_rows())
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"cc-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, CFG)
    enter_id = seed_entry_ruleset_from_rules(entry_rules, name=f"cc-enter-{account_id}")
    open_id = seed_exit_ruleset_from_rules(exit_rules, name=f"cc-open-{account_id}")
    seed_expert_instance(account_id=account_id, expert_class_name="_CcExpert",
                         enter_market_ruleset_id=enter_id,
                         open_positions_ruleset_id=open_id, instance_id=account_id)

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(SYMBOL, _bars(UNDERLYING))
    ps.set_clock(START)

    account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
    resolver.register_account(account_id, account)
    expert = _PlainBuyExpert(account_id)
    # The classic RM gates on these and the interface defaults are False/True, so without
    # them the equity entry is never sized and the overlay has nothing to write against.
    # (The real handler forces them through ``BACKTEST_FORCED_SETTINGS``.)
    expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                          "allow_automated_trade_modification": (True, "bool"),
                          "enable_buy": (True, "bool")})
    resolver.register_expert(account_id, expert)

    # No ``entry_action``: O_CC's entry is the EQUITY leg, and the overlay is an
    # OPEN_POSITIONS rule. Passing an option entry action here would put the engine on the
    # pure-option entry path, which is a different strategy.
    config = {"start_date": START, "end_date": END, "enabled_instruments": [SYMBOL],
              "seed": 42}
    engine = DailyBacktestEngine(
        account=account, experts=[(expert, account_id, expert.settings, enter_id)],
        # None, not object(): this run goes through the CLASSIC RM (an equity entry), and
        # ``_ensure_safeguard_stop`` always attempts an ATR lookup with ``use_atr_stop`` on.
        # ``get_latest_atr`` treats None as the documented no-ATR-available path; a bare
        # ``object()`` raises inside the sizing pass and every candidate is silently dropped.
        price_source=ps, config=config, indicator_provider=None)
    return engine, account, ctx


def _orders(account_id):
    from ba2_common.core.trade_store import orders_where
    return orders_where(account_id=account_id)


def _opens(calls):
    """The written call. Identified by SIDE, not by ``position_intent``: the intent field is
    stamped by the entry action and left blank by some close paths, so keying on it would
    make this test answer about a label rather than about a trade."""
    from ba2_common.core.types import OrderDirection
    return [o for o in calls if o.side is OrderDirection.SELL]


def _closes(calls):
    from ba2_common.core.types import OrderDirection
    return [o for o in calls if o.side is OrderDirection.BUY]


def _call_orders(account_id):
    return [o for o in _orders(account_id)
            if getattr(o, "contract_symbol", None) == CALL
            and o.status in OrderStatus.get_executed_statuses()]


def _run(account_id, *, drop_dte=False):
    m = _launcher()
    entry_rules, exit_rules = _rules(m, drop_dte=drop_dte)
    engine, account, ctx = _harness(entry_rules=entry_rules, exit_rules=exit_rules,
                                    account_id=account_id)
    try:
        engine.run()
        trips = [t for t in account.get_round_trip_trades()
                 if t.get("contract_symbol") == CALL]
        return account, list(_orders(account_id)), list(_call_orders(account_id)), trips
    finally:
        ctx.__exit__(None, None, None)


@pytest.fixture(scope="module")
def run_result():
    """ONE engine run over ~50 simulated bars; each assertion reads a different link."""
    return _run(861)


def test_the_engine_buys_a_ROUND_LOT_and_writes_ONE_call_against_it(run_result):
    account, orders, calls, trips = run_result
    equity = [o for o in orders
              if getattr(o, "contract_symbol", None) is None
              and o.status in OrderStatus.get_executed_statuses()]
    assert equity, "no equity fill: the overlay has no shares to be written against"
    assert (equity[0].filled_qty or equity[0].quantity) % 100 == 0, (
        f"the equity entry is not a whole 100-share lot ({equity[0].filled_qty}), so "
        f"floor(shares/100) writes no contract and O_CC is a plain equity run")

    opens = _opens(calls)
    assert len(opens) == 1, (
        f"expected exactly one written call (cc_guard stops it re-firing), got {len(opens)}")


def test_the_written_call_is_BOUGHT_BACK_at_the_dte_floor(run_result):
    """The exit that did not exist in this runtime before 2026-09-03."""
    account, orders, calls, trips = run_result
    closes = _closes(calls)
    assert len(closes) == 1, (
        f"the written call was never bought back ({[(o.side, o.status) for o in calls]}) — "
        f"it rode to expiry, which is exactly the asymmetry opt_dte closes")

    # It closed BECAUSE of the DTE floor, not because the contract expired: the buy-back
    # lands with the floor's worth of life still on the contract.
    # WALL-CLOCK dates are useless here (``TradingOrder.created_at`` is stamped with the
    # real clock, not the simulated one), so the SIMULATED exit comes off the round trip.
    trip = trips[0]
    left = (CALL_EXPIRY - trip["exit_time"].date()).days
    assert left >= DTE_FLOOR - 4, (
        f"the buy-back landed {left} days from expiry, which is not the DTE floor "
        f"({DTE_FLOOR}) firing — an exit AT expiry means the contract settled, not that the "
        f"rule closed it")
    assert trip["exit_price"] > 0, (
        "the exit price is 0.0, i.e. the contract expired worthless rather than being "
        "bought back")


def test_closing_the_call_does_NOT_release_the_stock_cover(run_result):
    """THE COVER INVARIANT. ``close_option`` closes the OPTION; the shares stay put."""
    account, orders, calls, trips = run_result
    shares = sum((o.filled_qty or 0) * (1 if o.side.value.lower().startswith("buy") else -1)
                 for o in orders
                 if getattr(o, "contract_symbol", None) is None
                 and o.status in OrderStatus.get_executed_statuses())
    assert shares >= 100, (
        f"the shares were sold along with the call ({shares} held) — closing an overlay "
        f"must never release its cover")
    assert account.get_option_positions() == [], "the call is still open at the end"


def test_the_engine_never_held_the_call_without_the_shares(run_result):
    """A naked short call is the one option position with genuinely unbounded loss.

    Measured through the SAME ``uncovered_short_calls`` the live pass uses — but on the
    SHARE cover, which that function cannot see (it reads option legs only): here the pair
    is the equity lot and the written call, so the check is the share count directly.
    """
    account, orders, calls, trips = run_result
    from ba2_common.core.types import OrderDirection

    running_shares, worst = 0.0, None
    events = sorted((o for o in orders
                     if o.status in OrderStatus.get_executed_statuses()
                     and o.created_at is not None),
                    key=lambda o: (o.created_at, o.id or 0))
    for o in events:
        if getattr(o, "contract_symbol", None) is None:
            running_shares += (o.filled_qty or 0) * (
                1 if o.side is OrderDirection.BUY else -1)
        elif o.side is OrderDirection.SELL:
            if running_shares < 100:
                worst = (o.created_at, running_shares)
    assert worst is None, (
        f"a call was written while only {worst[1]} share(s) were held @ {worst[0]}: NAKED")


def test_WITHOUT_the_dte_rule_the_call_is_never_closed(run_result):
    """THE MUTATION, executed as a test rather than by editing the launcher.

    Same fixture, same engine, same everything — with ``opt_dte`` removed from the emitted
    ruleset the written call is never bought back. That is what O_CC did in this runtime
    before 2026-09-03, and it is what the live pass used to paper over.
    """
    _account, _orders_, calls, trips = _run(862, drop_dte=True)
    opens = _opens(calls)
    assert opens, "the fixture stopped writing a call at all — the mutation proves nothing"
    assert trips and trips[0]["exit_price"] == 0.0, (
        f"with no cc_dte rule the call must ride to expiry and settle worthless; it exited "
        f"at {trips[0]['exit_price'] if trips else None} — find what else closed it")
    assert (CALL_EXPIRY - trips[0]["exit_time"].date()).days == 0, (
        "the mutant run exited before expiry, so this mutation does not pin cc_dte as the "
        "owner of that exit")
