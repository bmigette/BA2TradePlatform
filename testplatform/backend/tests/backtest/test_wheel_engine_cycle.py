"""``O_WHEEL`` END TO END THROUGH ``DailyBacktestEngine.run()``: the FULL cycle, per rule.

The wheel is ONE ruleset over a position that changes identity halfway through — a short
cash-secured put, then assigned stock, then a covered call written over it — and both runtimes
evaluate that ruleset once per SYMBOL against the OLDEST entry order. So every rule's SUBJECT
is decided by which transaction happens to be oldest on the bar, and the five exits the wheel
inherits from ``O_CSP`` were all authored for the put.

WHAT THIS FILE PINS, and it is a claim about EVALUATION rather than about outcomes: on every
bar of a real run, each emitted rule either acts on its named subject or is **provably not
evaluated**. That is measured by wrapping ``TradeActionEvaluator._evaluate_conditions``, which
is called exactly once per rule per evaluation, and recording ``(bar, rule)``. A rule that
merely happened not to FIRE would pass an outcome assertion and fail this one.

The cycle the fixture drives:

  the launcher emits O_CSP's option entry + cc_dte/cc_guard/cc_sell + wheel_stock_guard + the
  five inherited closes
    -> the engine sells a cash-secured put
    -> the underlying closes below the strike at expiry, so the put is ASSIGNED and 100 shares
       are delivered (``hold_assigned_stock``, which O_WHEEL alone sets)
    -> ``cc_sell`` writes ONE covered call against them and ``cc_guard`` stops it re-firing
    -> ``wheel_stock_guard`` halts the walk before any put-phase rule can re-anchor onto the
       stock
    -> ``cc_dte`` buys the written call back at its DTE floor, through the repository-resolved
       lookup, leaving the shares held.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_wheel_engine_cycle.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, OrderStatus, Recommendation

_LAUNCHER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_wheel_cycle", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_wheel_cycle"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


SYMBOL = "WHLX"
START = datetime(2024, 1, 2)
END = datetime(2024, 4, 30)

#: The put is sold ~20 DTE and finishes ITM: spot starts at 20 and is 18 at its expiry.
PUT_STRIKE = 20.0
PUT_EXPIRY = START.date() + timedelta(days=21)
PUT = f"WHLX{PUT_EXPIRY:%y%m%d}P00020000"

#: The covered call: ~5 % OTM of the 18 spot the shares are worth once delivered, inside the
#: [25, 45] DTE window ``_option_overlay_action`` authors for the overlay.
CALL_STRIKE = 19.0
#: 55 days out, so at the bar it is WRITTEN (a few days after assignment) it sits inside
#: the [25, 45] DTE window ``_option_overlay_action`` authors for the overlay.
CALL_EXPIRY = START.date() + timedelta(days=55)
CALL = f"WHLX{CALL_EXPIRY:%y%m%d}C00019000"

#: ``covered_call_days_to_expiry <= 7``, O_WHEEL's authored default. Left at the default: the
#: exit must work in the ruleset a DEFAULT genome produces.
CC_DTE_FLOOR = 7

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
    # O_WHEEL is the one kind in _HOLDS_ASSIGNED_STOCK; without it the engine liquidates the
    # assigned shares on the next bar and every covered call is a NAKED short call.
    "hold_assigned_stock": True,
}


def _weekdays(start: date, end: date):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(START.date(), END.date())


def _spot_on(d: date) -> float:
    """20 until the put's expiry week, then 18 — so the put finishes ITM and the shares are
    delivered, and the 19-strike call written afterwards stays OTM (it must be BOUGHT BACK by
    ``cc_dte``, not called away, or the test would pass on the wrong mechanism)."""
    return 20.0 if d < PUT_EXPIRY - timedelta(days=3) else 18.0


UNDERLYING = [(d, _spot_on(d), _spot_on(d) + 0.1, _spot_on(d) - 0.1, _spot_on(d))
              for d in DAYS]


def _bars(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1_000_000}
            for (d, o, h, lo, c) in rows]


def _put_premium(d: date) -> float:
    """FLAT at the credit it was sold for, right up to expiry.

    Deliberate: a decaying put would hit ``opt_tp``'s profit band and be bought back, and the
    fixture would never reach the assignment this whole file is about. Flat premium means the
    only thing that can end the put phase is its own expiry — which is the path being pinned.
    """
    return 0.50


def _call_premium(d: date) -> float:
    left = (CALL_EXPIRY - d).days
    if left > 20:
        return 0.60
    return round(max(0.05, 0.60 * left / 20.0), 4)


def _chain_rows():
    return [
        {"occ_symbol": PUT, "option_type": "put", "strike": PUT_STRIKE,
         "expiry": PUT_EXPIRY.isoformat(), "bid": 0.50, "ask": 0.50, "last": 0.50,
         "iv": 0.30, "delta": -0.30, "open_interest": 5000},
        {"occ_symbol": CALL, "option_type": "call", "strike": CALL_STRIKE,
         "expiry": CALL_EXPIRY.isoformat(), "bid": 0.60, "ask": 0.60, "last": 0.60,
         "iv": 0.30, "delta": 0.25, "open_interest": 5000},
    ]


def _bar_rows():
    rows = []
    for occ, ot, strike, expiry, px_of in (
            (PUT, "put", PUT_STRIKE, PUT_EXPIRY, _put_premium),
            (CALL, "call", CALL_STRIKE, CALL_EXPIRY, _call_premium)):
        for d in DAYS:
            if d > expiry:
                continue
            px = px_of(d)
            rows.append({"occ_symbol": occ, "date": d.isoformat(), "open": px,
                         "high": px + 0.05, "low": max(0.01, px - 0.05), "close": px,
                         "volume": 500, "underlying": SYMBOL, "option_type": ot,
                         "strike": strike, "expiry": expiry.isoformat(),
                         "iv": 0.30, "delta": -0.30 if ot == "put" else 0.25})
    return rows


class _PlainBuyExpert(MarketExpertInterface):
    """A BUY expert with nothing else to say — the wheel's entry gate is directional only."""

    bypasses_classic_rm = False

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "Stub BUY expert for the wheel cycle test."

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
            current_price=price if price is not None else 20.0,
            details="buy", expected_profit_percent=25.0, raw_outputs={})


def _strip_unanswerable_gates(rules, keep_fields):
    """Drop every entry leaf this fixture cannot answer (iv_rank, relative volume, ...).

    Each is ``toggle_optimize=True``, i.e. a genome may switch it off, and switching one off
    is a DELETION of the node — so this is a point in the searched space, not a bypass."""
    import copy
    out = copy.deepcopy(rules)
    for rule in out:
        conds = (rule.get("conditions") or {}).get("conditions")
        if not conds:
            continue
        rule["conditions"]["conditions"] = [c for c in conds if c.get("field") in keep_fields]
    return out


def _rules(m):
    """O_WHEEL's OWN launcher-built rules, with the entry's DTE/strike box pinned to the
    fixture's two contracts and nothing else touched."""
    from ba2_common.core.rule_models import normalize_trade_rules

    strat = m._build_strategy("O_WHEEL", "wheel-cycle", "FMPRating")
    entry = _strip_unanswerable_gates(
        normalize_trade_rules(list(strat.entry_rules or [])),
        keep_fields={"has_no_position", "bullish"})
    # The ``option_*`` keys are the ones the shared converter forwards to the action (see
    # ``rule_builders._OPTION_ACTION_PARAM_KEYS``); the bare ``dte_min``/``strike_param``
    # spellings are what it forwards them AS, so overriding those would be overriding the
    # output of a step that has not run yet. Both windows here are points a real genome can
    # reach (``option_dte`` decodes a >= 14-day window; ``option_strike_param`` searches
    # 5-20 %), narrowed only so the fixture's two contracts are the ones selected.
    for action in entry[0].get("actions") or []:
        action["option_dte_min"] = 14
        action["option_dte_max"] = 30
        action["option_strike_param"] = 0.0      # ATM: the 20 strike against a 20 spot
        action["option_strike_method"] = "percent_otm"

    # The five put-phase thresholds pinned to levels INSIDE their own searched bands that the
    # fixture window cannot reach, so the put survives to expiry and is assigned. They must
    # still be EMITTED and EVALUATED — that is exactly what the M7 pins measure — so pinning
    # them non-firing is the only way to observe reachability without the put being closed
    # first. Each value is a point a real genome can occupy (the PMCC engine test pins its two
    # searched thresholds the same way, for the same reason).
    exits = list(strat.exit_rules or [])
    for rid, value in (("opt_tp", 75.0), ("opt_time", 200.0), ("opt_dte", 0.0),
                       ("opt_sl", -500.0), ("opt_sl_ml", 500.0)):
        rule = next((r for r in exits if r.get("id") == rid), None)
        assert rule is not None, f"the wheel stopped emitting {rid}; the M7 pins go vacuous"
        for leaf in (rule.get("conditions") or {}).get("conditions") or []:
            if "value" in leaf:
                leaf["value"] = value
    return entry, exits


def _harness(*, entry_rules, exit_rules, entry_action, account_id):
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

    tmpdir = tempfile.mkdtemp(prefix="wheel-cycle-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows(SYMBOL, START.date().isoformat(), _chain_rows())
    cache.write_bar_rows(_bar_rows())
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"wheel-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, CFG)
    enter_id = seed_entry_ruleset_from_rules(entry_rules, name=f"wheel-enter-{account_id}")
    open_id = seed_exit_ruleset_from_rules(exit_rules, name=f"wheel-open-{account_id}")
    seed_expert_instance(account_id=account_id, expert_class_name="_WheelExpert",
                         enter_market_ruleset_id=enter_id,
                         open_positions_ruleset_id=open_id, instance_id=account_id)

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(SYMBOL, _bars(UNDERLYING))
    ps.set_clock(START)

    account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
    resolver.register_account(account_id, account)
    expert = _PlainBuyExpert(account_id)
    expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                          "allow_automated_trade_modification": (True, "bool"),
                          "enable_buy": (True, "bool")})
    resolver.register_expert(account_id, expert)

    config = {"start_date": START, "end_date": END, "enabled_instruments": [SYMBOL],
              "seed": 42, "entry_action": entry_action, "hold_assigned_stock": True}
    engine = DailyBacktestEngine(
        account=account, experts=[(expert, account_id, expert.settings, enter_id)],
        price_source=ps, config=config, indicator_provider=None)
    return engine, account, ctx


def _seeded_names(exit_rules, ruleset_name):
    """seeded EventAction name -> the launcher rule id it came from.

    ``seed_ruleset_from_rules`` names each rule ``f"{ruleset}-rule-{idx}"`` off its position in
    the INPUT list (``rule.get("name")`` is absent on launcher rules), so the mapping is the
    enumerate index — stable even if a rule is skipped for having no usable action, because
    the index is taken before the skip.
    """
    return {f"{ruleset_name}-rule-{idx}": r.get("id")
            for idx, r in enumerate(exit_rules or [])}


def _watch_rule_evaluations(monkeypatch, name_to_id):
    """Record ``rule -> the bars on which its conditions were EVALUATED``.

    ``_evaluate_conditions`` is called exactly once per rule per evaluation, before the
    first-match break decides anything, so this measures REACHABILITY on real bars — not
    which rules happened to fire. That distinction is the whole point: a put-phase rule that
    is merely out of the money on the assigned stock would pass an outcome assertion.
    """
    from ba2_common.core.TradeActionEvaluator import TradeActionEvaluator

    seen = defaultdict(list)
    real = TradeActionEvaluator._evaluate_conditions

    def wrapped(self, event_action, instrument_name, expert_recommendation, existing_order):
        raw = getattr(event_action, "name", None)
        rid = name_to_id.get(raw, raw)
        seen[rid].append(getattr(expert_recommendation, "created_at", None))
        return real(self, event_action, instrument_name, expert_recommendation, existing_order)

    monkeypatch.setattr(TradeActionEvaluator, "_evaluate_conditions", wrapped)
    return seen


def _orders(account_id):
    from ba2_common.core.trade_store import orders_where
    return orders_where(account_id=account_id)


@pytest.fixture(scope="module")
def run_result():
    """ONE engine run over the whole cycle, shared by the assertions below."""
    from _pytest.monkeypatch import MonkeyPatch

    from tests.backtest.fixtures.e2e_support import hermetic_providers

    mp = MonkeyPatch()
    m = _launcher()
    entry_rules, exit_rules = _rules(m)
    engine, account, ctx = _harness(
        entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=(entry_rules[0].get("actions") or [None])[0], account_id=931)
    seen = _watch_rule_evaluations(mp, _seeded_names(exit_rules, "wheel-open-931"))
    try:
        # HERMETIC, and not merely tidy: ``wire_backtest_seams`` is idempotent, so whichever
        # test wires FIRST in a process owns the provider seam for the rest of it (the
        # suite's known order dependence, F3 in the final review). Run alone this fixture
        # never asks a provider anything; run after a test that left the REAL FMP provider
        # wired it raised "FMP API key not configured" during the entry. Pointing both seams
        # at the fixture cache for the duration is what the other engine e2e tests in this
        # directory do, for exactly this reason.
        with hermetic_providers():
            results = engine.run()
        yield account, dict(seen), list(_orders(931)), exit_rules, results
    finally:
        mp.undo()
        ctx.__exit__(None, None, None)


def _by_contract(orders, contract):
    return [o for o in orders
            if getattr(o, "contract_symbol", None) == contract
            and o.status in OrderStatus.get_executed_statuses()]


# ===========================================================================
# the cycle actually happened
# ===========================================================================
def test_the_wheel_sells_a_put_and_has_it_ASSIGNED(run_result):
    account, seen, orders, _ = run_result[:4]
    puts = _by_contract(orders, PUT)
    assert puts, f"no put was ever sold: {[o.contract_symbol for o in orders]}"

    shares = sum((o.filled_qty or 0) * (1 if o.side.value.lower().startswith("buy") else -1)
                 for o in orders if getattr(o, "contract_symbol", None) is None
                 and o.status in OrderStatus.get_executed_statuses())
    assert shares >= 100, (
        f"the put was never assigned ({shares} shares held) — the cycle this file is about "
        f"never started")


def test_ONE_covered_call_is_written_over_the_assigned_shares(run_result):
    account, seen, orders, _ = run_result[:4]
    from ba2_common.core.types import OrderDirection

    writes = [o for o in _by_contract(orders, CALL) if o.side is OrderDirection.SELL]
    assert len(writes) == 1, (
        f"expected exactly one written call (cc_guard stops it re-firing), got {len(writes)}")


def test_the_written_call_is_BOUGHT_BACK_by_cc_dte_not_called_away(run_result):
    account, seen, orders, _ = run_result[:4]
    from ba2_common.core.types import OrderDirection

    closes = [o for o in _by_contract(orders, CALL) if o.side is OrderDirection.BUY]
    assert len(closes) == 1, (
        f"the written call was never bought back ({[(o.side, o.status) for o in _by_contract(orders, CALL)]})")

    trips = [t for t in account.get_round_trip_trades() if t.get("contract_symbol") == CALL]
    assert trips, "the call has no round trip"
    left = (CALL_EXPIRY - trips[0]["exit_time"].date()).days
    assert left >= CC_DTE_FLOOR - 4, (
        f"the call exited {left} days from expiry — that is settlement, not cc_dte firing")
    assert trips[0]["exit_price"] > 0, "the call expired worthless rather than being closed"


# ===========================================================================
# THE M7 CLAIM: every emitted rule acts on a named subject or is not evaluated
# ===========================================================================
def test_every_emitted_rule_is_accounted_for(run_result):
    """No rule may be left out of the audit below by being forgotten."""
    _account, seen, _orders_, exit_rules = run_result[:4]
    emitted = {r.get("id") for r in exit_rules}
    assert emitted == {"cc_dte", "cc_guard", "cc_sell", "wheel_stock_guard",
                       "opt_tp", "opt_time", "opt_dte", "opt_sl", "opt_sl_ml"}, emitted


@pytest.mark.parametrize("rid", ["opt_tp", "opt_time", "opt_dte", "opt_sl", "opt_sl_ml"])
def test_no_put_phase_rule_is_EVALUATED_after_the_shares_are_assigned(run_result, rid):
    """THE PIN. Not "did not fire" — was never ASKED, on any bar where the subject would have
    been the assigned stock.

    Before ``wheel_stock_guard`` these rules were evaluated on every stock-phase bar, and
    ``opt_tp``/``opt_time``/``opt_sl`` read the STOCK's P&L and days-open through
    ``_get_pnl_for_condition`` / ``DaysOpenedCondition`` — thresholds authored for a short
    put's credit, applied to a share position, firing a ``close_option`` that resolves
    nothing while consuming the bar's first-match slot.
    """
    _account, seen, orders, _ = run_result[:4]
    from ba2_common.core.types import OrderDirection

    assigned_from = min(
        (o.created_at for o in orders
         if getattr(o, "contract_symbol", None) is None
         and o.side is OrderDirection.BUY
         and o.status in OrderStatus.get_executed_statuses()
         and o.created_at is not None),
        default=None)
    assert assigned_from is not None, "no assignment happened; the pin would be vacuous"

    # STRICTLY after the assignment bar, and the strictness is a fact about the engine rather
    # than a softened assertion: the MANAGE pass runs BEFORE settlement on a bar (step 3
    # before step 4a-pre — the step order ``test_wheel_assignment.py::
    # test_the_engines_STEP_ORDER_cannot_sell_shares_out_from_under_a_written_call`` pins), so
    # on the assignment bar itself the put is still open and still the anchor. Evaluating a
    # put-phase rule there is correct; it is every bar AFTER, when the subject has become the
    # stock, that this pin is about — and there are ~70 of them in this run.
    stock_phase_bars = [b for b in seen.get(rid, []) if b is not None and b > assigned_from]
    assert stock_phase_bars == [], (
        f"{rid} was evaluated on {len(stock_phase_bars)} bar(s) after assignment, with the "
        f"STOCK as its subject — first at {stock_phase_bars[0]}")


@pytest.mark.parametrize("rid", ["opt_tp", "opt_time", "opt_dte", "opt_sl", "opt_sl_ml"])
def test_every_put_phase_rule_IS_evaluated_while_the_put_is_open(run_result, rid):
    """The control: the guard must not have made them inert everywhere. They manage the
    cash-secured put and are asked on EVERY put-phase bar, not merely on one.

    Asserted against ``cc_guard``'s own bar count, which is the number of manage bars this
    run had: the two are reached on the same walk until the guard halts it, so a put-phase
    rule seen on fewer bars than the guard means something shadowed it.
    """
    _account, seen, _orders_, _ = run_result[:4]
    assert seen.get(rid), (
        f"{rid} was never evaluated at all — the wheel stopped managing its short put, "
        f"which is a bigger defect than the one M7 fixes")
    put_phase_bars = [b for b in seen.get("cc_guard", []) if b in set(seen[rid])]
    assert len(seen[rid]) == len(put_phase_bars), (
        f"{rid} was evaluated on {len(seen[rid])} bars but only {len(put_phase_bars)} of "
        f"them are bars the walk reached at all")
    assert len(seen[rid]) >= 5, (
        f"{rid} was evaluated on only {len(seen[rid])} bar(s) — too few for the control to "
        f"mean anything about the put phase")


def test_the_stock_phase_rules_ARE_evaluated_and_act_on_their_own_subjects(run_result):
    """The other half of "acts on a named subject or is absent": the three rules that DO run
    in the stock phase are the three whose subject is named there."""
    _account, seen, _orders_, _ = run_result[:4]
    for rid in ("cc_dte", "cc_guard", "cc_sell"):
        assert seen.get(rid), f"{rid} was never evaluated, so the stock phase is unmanaged"
    assert seen.get("wheel_stock_guard"), "the guard itself was never evaluated"


# ===========================================================================
# M8 — the uncovered-assigned telemetry
#
# The state is silent by construction without it: on a bar holding assigned shares with no
# written call, ``cc_sell`` matches (its trigger is ``has_assigned_shares``), so
# ``wheel_stock_guard`` halts the ruleset behind it and nothing else can act; the assignment
# liquidation is a no-op under ``hold_assigned_stock``, the lot has no bracket, and there is
# no end-of-run flatten. RECORDED, NOT SCORED.
# ===========================================================================
def _engine_with_counter():
    """A bare engine carrying only the counter state the metric reads."""
    from app.services.backtest.daily_engine import DailyBacktestEngine

    e = DailyBacktestEngine.__new__(DailyBacktestEngine)
    e._uncovered_assigned_streak = {}
    e._uncovered_assigned_max = {}
    e._uncovered_assigned_total = {}
    e._uncovered_assigned_reasons = {}
    return e


def _declined(reason="empty_chain"):
    from ba2_common.core.TradeActions import COVERED_CALL_DECLINE_KEY
    return [{"success": False, "data": {COVERED_CALL_DECLINE_KEY: reason}}]


def _written():
    return [{"success": True, "data": {"order_id": 7}}]


def test_three_uncovered_bars_are_counted_as_three(monkeypatch):
    e = _engine_with_counter()
    for i in range(3):
        e._record_uncovered_assigned(SYMBOL, _declined(), START + timedelta(days=i))

    metric = e._uncovered_assigned_metric()
    assert metric["max_consecutive"] == 3
    assert metric["total_bars"] == 3
    assert metric["by_symbol"][SYMBOL]["last_reason"] == "empty_chain"


def test_writing_the_call_ENDS_the_streak_but_not_the_total(monkeypatch):
    """The streak is what an operator acts on; the total is what a results reader counts."""
    e = _engine_with_counter()
    for i in range(3):
        e._record_uncovered_assigned(SYMBOL, _declined(), START + timedelta(days=i))
    e._record_uncovered_assigned(SYMBOL, _written(), START + timedelta(days=3))
    e._record_uncovered_assigned(SYMBOL, _declined(), START + timedelta(days=4))

    metric = e._uncovered_assigned_metric()
    assert metric["max_consecutive"] == 3, "the max must survive the reset"
    assert metric["total_bars"] == 4
    assert e._uncovered_assigned_streak[SYMBOL] == 1, "the streak restarted"


def test_the_alarm_fires_ONCE_when_the_streak_crosses(monkeypatch):
    """Once, on the bar it crosses — every bar after would bury it."""
    from app.services.backtest import daily_engine as DE

    e = _engine_with_counter()
    errors = []
    monkeypatch.setattr(DE.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    for i in range(e.UNCOVERED_ASSIGNED_ALARM_BARS + 3):
        e._record_uncovered_assigned(SYMBOL, _declined(), START + timedelta(days=i))

    assert len(errors) == 1, f"expected one alarm, got {len(errors)}"
    assert SYMBOL in errors[0] and "ASSIGNED SHARES" in errors[0]
    assert "No liquidation is taken" in errors[0], (
        "the alarm must say the platform is NOT selling the shares — that is the operator's "
        "decision, and an alarm that reads like an action would be worse than none")


def test_a_run_that_never_held_assigned_stock_reports_zeroes():
    e = _engine_with_counter()
    metric = e._uncovered_assigned_metric()
    assert metric == {"max_consecutive": 0, "total_bars": 0, "by_symbol": {}}


def test_the_real_run_REPORTS_the_uncovered_tail_it_actually_had(run_result):
    """END TO END, and it caught something on its first run.

    This fixture publishes ONE call contract. Once ``cc_dte`` buys it back there is nothing
    left to write, so the wheel holds assigned shares uncovered for the REST OF THE RUN —
    ``cc_sell`` declining ``empty_chain`` on every bar, ``wheel_stock_guard`` halting behind
    it, and nothing else able to act. That is precisely the silent steady state M8 exists to
    surface, occurring by accident in a fixture built for something else, which is the best
    evidence available that the state is reachable rather than theoretical.

    So the assertion is the honest one: the run reports the tail, names the reason, and the
    streak begins only AFTER the call was closed — not that a wheel never has one.
    """
    _account, _seen, orders, _exits, results = run_result
    from ba2_common.core.types import OrderDirection

    assert "uncovered_assigned_bars" in results, sorted(results)
    metric = results["uncovered_assigned_bars"]
    assert set(metric) == {"max_consecutive", "total_bars", "by_symbol"}

    assert metric["max_consecutive"] > 0 and metric["total_bars"] > 0, (
        "the fixture's post-buyback tail is uncovered-assigned and the metric reported "
        "nothing — it is not counting")
    assert metric["by_symbol"][SYMBOL]["last_reason"] == "empty_chain", metric

    # The streak is the TAIL, not the whole run: while the call was open the wheel was
    # covered, so the count must be well short of the number of manage bars.
    closed = [o for o in _by_contract(orders, CALL) if o.side is OrderDirection.BUY]
    assert closed, "precondition: the call was bought back"
    assert metric["max_consecutive"] < len(DAYS), (
        f"every bar counted as uncovered, including the covered ones: {metric}")
