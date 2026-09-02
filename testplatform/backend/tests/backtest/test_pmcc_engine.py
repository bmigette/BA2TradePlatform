"""``O_PMCC`` END TO END THROUGH ``DailyBacktestEngine.run()`` (plan Task 6, milestone 4).

Everything else about the PMCC is pinned at the unit level in
``packages/common/tests/test_pmcc_lifecycle.py`` — the builder, the intrinsic floor, the
roll's leg order, the restamp, the three per-leg conditions, the fail-closed guards. Each of
those proves ONE link. THIS chain had never been run end to end, and it is a chain where every
link can be individually correct while the whole thing does nothing (or, worse, does something
unsafe):

  the launcher emits a two-expiry entry action and three overlay rules
    -> the engine's option-entry path submits a structure whose legs sit on TWO expiries
    -> both legs fill, and the transaction is declared ``pmcc``
    -> ``short_leg_days_to_expiry`` reads the SHORT leg (not the LEAPS, a year out) and the
       roll rule fires
    -> ``roll_pmcc_short`` buys the expiring overlay back and sells the next ON THE SAME
       TRANSACTION, and the max-loss stamp is rewritten
    -> ``days_to_expiry`` reads the LONG leg (not the freshly written overlay) and the DTE
       floor closes the WHOLE structure — including the overlay the entry order never
       mentioned.

Six modules, four persisted row shapes, two DTE readers that must disagree with each other on
purpose. A unit test on any one of them passes with the chain broken.

THE INVARIANT IS CHECKED ON EVERY BAR, not at the end: the account's own per-bar equity
snapshot is wrapped, and on each call every open option transaction is rebuilt through the
SAME ``build_structure`` the live exit pass uses and asked
``option_lifecycle.uncovered_short_calls``. A naked short call that existed for one bar and was
then rolled away would pass an end-state assertion and fail this one.

BAR SPARSITY IS ON THE LEG THE DESIGN MEASURED IT ON. Design section 1 measured 50-65 % bar
density *at LEAPS range*; a 30-45-DTE overlay trades every day. So the LEAPS carries bars on
half the run's days and the overlays carry them on all of them — which also makes the roll and
the exit deterministic instead of dependent on which side of a gap they landed.

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_pmcc_engine.py -q
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
    spec = importlib.util.spec_from_file_location("lch_pmcc_engine", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_pmcc_engine"] = m
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

SYMBOL = "PMCCX"
START = datetime(2024, 1, 2)
END = datetime(2024, 3, 15)
SPOT = 100.0

#: 400 days out — inside the key's OWN authored entry window [380, 470], so no genome-shaped
#: pinning is needed for the entry: this is a contract a real O_PMCC job would buy.
LEAPS_EXPIRY = START.date() + timedelta(days=400)
#: The first overlay, 35 days out: inside the fixed [30, 45] window the row declares.
OVERLAY1_EXPIRY = START.date() + timedelta(days=35)
#: The replacement, 70 days out — which is 35 days out again as of the roll, i.e. the SAME
#: [30, 45] window measured from the day the roll is taken.
OVERLAY2_EXPIRY = START.date() + timedelta(days=70)

LEAPS = f"PMCCX{LEAPS_EXPIRY:%y%m%d}C00080000"
OVERLAY1 = f"PMCCX{OVERLAY1_EXPIRY:%y%m%d}C00110000"
OVERLAY2 = f"PMCCX{OVERLAY2_EXPIRY:%y%m%d}C00112000"

#: ``short_leg_days_to_expiry <= 5`` fires 30 days in (a searched level: the band is 1-7).
ROLL_DTE = 5
#: ``days_to_expiry <= 350`` on the LONG leg fires 50 days in. A real job searches 90-240 on a
#: multi-year window; the fixture uses a shorter horizon so the crossing happens inside a
#: runnable one, exactly as the O_LEAPC engine test does for the same reason.
LONG_DTE_FLOOR = 350


def _weekdays(start: date, end: date):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(START.date(), END.date())
UNDERLYING = [(d, SPOT, SPOT + 1.0, SPOT - 1.0, SPOT) for d in DAYS]
#: THE SPARSE LEG. Consecutive PAIRS, not alternate days: the default ``next_bar_open`` fill
#: model needs the bar AFTER the one an order was placed on, and a strict every-other-day
#: pattern gives it that exactly never.
LEAPS_BAR_DAYS = [d for i, d in enumerate(DAYS) if i % 4 in (0, 1)]

#: contract -> (strike, expiry, premium, delta). The LEAPS is deep ITM (intrinsic 20 at a spot
#: of 100), the overlays are ~0.20 delta above it.
CONTRACTS = {
    LEAPS: (80.0, LEAPS_EXPIRY, 25.0, 0.80),
    OVERLAY1: (110.0, OVERLAY1_EXPIRY, 3.00, 0.20),
    OVERLAY2: (112.0, OVERLAY2_EXPIRY, 2.50, 0.20),
}

#: 25.00 paid for the LEAPS, 3.00 collected on the first overlay.
ENTRY_NET_DEBIT = 22.00
ENTRY_MAX_LOSS = 2200.0


def _bars(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1_000_000}
            for (d, o, h, lo, c) in rows]


def _chain_rows():
    return [{"occ_symbol": occ, "option_type": "call", "strike": strike,
             "expiry": expiry.isoformat(), "bid": premium, "ask": premium, "last": premium,
             "iv": 0.30, "delta": delta, "open_interest": 5000}
            for occ, (strike, expiry, premium, delta) in CONTRACTS.items()]


def _premium_on(occ: str, day: date) -> float:
    """The contract's premium on ``day``.

    The LEAPS is flat -- a 400-day 0.80-delta call on a flat underlying barely moves, and a
    moving long leg would make the equity assertions measure the fixture rather than the
    marks.

    The OVERLAYS decay, because that is what makes the roll a CREDIT: buying an
    almost-expired overlay back for 0.75 and selling the next for 2.50 banks 1.75, which is
    the direction design section 3's "restamped as credits accrue" describes. A flat overlay
    would still roll, but the restamp would move the other way and the test would pin the
    arithmetic without exercising the case the design is about.

    THE DECAY IS A TERMINAL RAMP, not a straight line from the entry, and that is a
    FILL-MODEL constraint rather than realism for its own sake (though theta really does
    accelerate in the last weeks). The entry quotes a net DEBIT of ``LEAPS ask - overlay
    bid``; a short leg that starts decaying on day one makes that net RISE every bar, so the
    entry's limit could never be met and the structure would never fill. Flat until 20 days
    out, ramping after, gives the entry a stable net to fill against and the roll a decayed
    contract to buy back.
    """
    strike, expiry, premium, _delta = CONTRACTS[occ]
    if occ == LEAPS:
        return premium
    left = (expiry - day).days
    if left > 20:
        return premium
    return round(max(0.30, premium * left / 20.0), 4)


def _bar_rows():
    rows = []
    for occ, (strike, expiry, premium, delta) in CONTRACTS.items():
        days = LEAPS_BAR_DAYS if occ == LEAPS else DAYS
        for d in days:
            if d > expiry:
                continue
            px = _premium_on(occ, d)
            rows.append({"occ_symbol": occ, "date": d.isoformat(), "open": px,
                         "high": px + 0.2, "low": max(0.05, px - 0.2), "close": px,
                         "volume": 500, "underlying": SYMBOL, "option_type": "call",
                         "strike": strike, "expiry": expiry.isoformat(),
                         "iv": 0.30, "delta": delta})
    return rows


class _PlainBuyExpert(MarketExpertInterface):
    """A BUY expert with nothing else to say — the PMCC's entry gate is directional only."""

    bypasses_classic_rm = False

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "Stub BUY expert for the PMCC engine chain test."

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
            signal=OrderRecommendation.BUY, confidence=90.0,
            current_price=price if price is not None else SPOT,
            details="buy", expected_profit_percent=25.0, raw_outputs={})


def _strip_unanswerable_gates(rules, keep_fields):
    """Drop every entry leaf this fixture cannot answer.

    iv_rank / iv_to_realized_vol / relative_volume / expected_profit all fail CLOSED on three
    option bars and no IV history, and every one of them is ``toggle_optimize=True`` — i.e. a
    real genome may switch them off, and switching one off is a DELETION of the node
    (``strategy_param_space._apply_to_tree``). So this is a point in the searched space, not a
    bypass: the directional gate and the structural ``has_no_position`` guard both stay.
    """
    import copy
    out = copy.deepcopy(rules)
    for rule in out:
        conds = (rule.get("conditions") or {}).get("conditions")
        if not conds:
            continue
        rule["conditions"]["conditions"] = [c for c in conds if c.get("field") in keep_fields]
    return out


def _rules(m):
    """O_PMCC's OWN launcher-built entry and exit rules, with the two searched thresholds
    pinned to concrete levels the fixture window can reach."""
    from ba2_common.core.rule_models import normalize_trade_rules, trade_rules_from_legacy

    entry = _strip_unanswerable_gates(
        normalize_trade_rules([m._option_entry_rule("O_PMCC")]),
        keep_fields={"has_no_position", "bullish"})
    exits = trade_rules_from_legacy(exit_conditions=m._option_exit_rules("O_PMCC"))["exit_rules"]
    # Keep the ROLL and the LONG-leg DTE floor. The take-profit, the elapsed-time exit, the
    # max-loss stop and the delta floor each carry their own on/off gene, so a genome with
    # only these live is a real point in the searched space -- and leaving them armed would
    # let one of them close the position first, so the test would pass without the chain
    # working.
    exits = [r for r in exits if r.get("id") in ("pmcc_roll_dte", "opt_dte")]
    assert [r["id"] for r in exits] == ["pmcc_roll_dte", "opt_dte"], (
        "the roll must precede the closes in the emitted ruleset")
    for rule, field, value in ((exits[0], "short_leg_days_to_expiry", ROLL_DTE),
                               (exits[1], "days_to_expiry", LONG_DTE_FLOOR)):
        for leaf in rule["conditions"]["conditions"]:
            if leaf.get("field") == field:
                leaf["value"] = value
    return entry, exits


def _harness(*, entry_rules, exit_rules, entry_action, account_id):
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

    tmpdir = tempfile.mkdtemp(prefix="pmcc-engine-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows(SYMBOL, START.date().isoformat(), _chain_rows())
    cache.write_bar_rows(_bar_rows())
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"pmcc-{account_id}")
    ctx.__enter__()

    seed_account_definition(account_id, CFG)
    enter_id = seed_entry_ruleset_from_rules(entry_rules, name=f"pmcc-enter-{account_id}")
    open_id = seed_exit_ruleset_from_rules(exit_rules, name=f"pmcc-open-{account_id}")
    seed_expert_instance(account_id=account_id, expert_class_name="_PmccExpert",
                         enter_market_ruleset_id=enter_id,
                         open_positions_ruleset_id=open_id, instance_id=account_id)

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars(SYMBOL, _bars(UNDERLYING))
    ps.set_clock(START)

    account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
    resolver.register_account(account_id, account)
    expert = _PlainBuyExpert(account_id)
    resolver.register_expert(account_id, expert)

    config = {"start_date": START, "end_date": END, "enabled_instruments": [SYMBOL],
              "seed": 42, "entry_action": entry_action}
    engine = DailyBacktestEngine(
        account=account, experts=[(expert, account_id, expert.settings, enter_id)],
        price_source=ps, config=config, indicator_provider=object())
    return engine, account, ctx


def _watch_the_invariant(account, expert_id, seen):
    """Wrap the account's per-bar equity snapshot with the leg-pair check.

    ``snapshot_equity`` is called ONCE PER SIMULATED BAR, after the bar's fills and
    settlements, so it is the one place that sees every state the book passes through. Each
    call rebuilds every open option transaction through ``OptionRiskManagement.build_structure``
    -- the SAME function the live exit pass uses, not a reimplementation -- and asks
    ``option_lifecycle.uncovered_short_calls`` about it.
    """
    from ba2_common.core.OptionRiskManagement import sleeve_structures, structure_metrics
    from ba2_common.core.option_lifecycle import uncovered_short_calls
    from ba2_common.core.trade_store import orders_where

    real = account.snapshot_equity

    def _stamp(transaction_id):
        """The entry order's current ``max_loss_per_contract``. Sampled per bar because the
        roll REWRITES it, so the entry-time value cannot be read back after the run."""
        for o in orders_where(transaction_id=transaction_id):
            if getattr(o, "parent_order_id", None) is None:
                return (getattr(o, "data", None) or {}).get("max_loss_per_contract")
        return None

    def wrapped(as_of, *a, **kw):
        structures, unbuildable = sleeve_structures(expert_id)
        assert not unbuildable, f"unbuildable option transaction(s) {unbuildable} @ {as_of}"
        for structure in structures:
            naked = uncovered_short_calls(structure.held_legs)
            assert not naked, (
                f"NAKED SHORT CALL {naked} on transaction {structure.transaction_id} "
                f"@ {as_of}: the engine held the short without the long")
            if structure.held_legs:
                metrics = structure_metrics(structure)
                assert metrics.is_defined_risk is not False, (
                    f"the structure measured as UNDEFINED risk @ {as_of} — the rails would "
                    f"charge it to the naked sub-cap")
                seen.append({"as_of": as_of,
                             "legs": tuple(l.contract_symbol for l in structure.held_legs),
                             "committed": metrics.committed,
                             "max_loss": _stamp(structure.transaction_id)})
        return real(as_of, *a, **kw)

    account.snapshot_equity = wrapped
    return seen


def _orders(account_id):
    from ba2_common.core.trade_store import orders_where
    return orders_where(account_id=account_id)


@pytest.fixture(scope="module")
def run_result():
    """ONE engine run, shared by the assertions below: it is ~50 simulated bars and every
    assertion reads a different link of the same chain."""
    m = _launcher()
    entry_rules, exit_rules = _rules(m)
    engine, account, ctx = _harness(
        entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_rules[0]["actions"][0], account_id=781)
    seen = []
    _watch_the_invariant(account, 781, seen)
    try:
        engine.run()
        yield account, seen
    finally:
        ctx.__exit__(None, None, None)


def test_the_entry_opens_BOTH_legs_on_TWO_expiries_and_they_fill(run_result):
    from ba2_common.core.types import OrderStatus

    account, _ = run_result
    orders = _orders(781)
    parents = [o for o in orders
               if o.parent_order_id is None and not o.contract_symbol and o.option_strategy]
    entries = [o for o in parents if o.option_strategy == "pmcc"]
    assert entries, ("the O_PMCC entry never fired: the 0.80/0.20 delta picks or the two DTE "
                     "windows found no contract on the fixture chain")
    entry = entries[0]

    children = [o for o in orders if o.parent_order_id == entry.id]
    assert {c.contract_symbol for c in children} == {LEAPS, OVERLAY1}
    assert {c.expiry for c in children} == {LEAPS_EXPIRY, OVERLAY1_EXPIRY}, (
        "the per-leg expiries did not reach the child rows")
    assert all(c.status == OrderStatus.FILLED for c in children), (
        "the structure was submitted but never filled off the fixture bars")
    # The structure-level value stays NULL, because no single date is true of the position.
    assert entry.expiry is None


def test_the_entry_stamps_the_intrinsic_floor_and_the_overlay_spec(run_result):
    from ba2_common.core.option_lifecycle import ORDER_PMCC_OVERLAY_KEY

    entry = next(o for o in _orders(781)
                 if o.parent_order_id is None and o.option_strategy == "pmcc")
    account, seen = run_result
    data = entry.data or {}
    # 25.00 for the LEAPS - 3.00 for the overlay = a 22.00 debit, and the intrinsic floor is
    # that debit x 100. Read from the FIRST bar the watcher saw, because the roll REWRITES
    # this row later in the run -- the entry-time value does not survive to the end. The stamp
    # is measured from the CONCEDED limit (``entry_cross`` is applied at the submit seam,
    # after sizing), which errs against the strategy by at most one modelled half-spread per
    # leg, so it is compared against the order's own limit rather than hard-coded.
    at_entry = seen[0]["max_loss"]
    assert at_entry == pytest.approx(float(entry.limit_price) * 100.0)
    assert ENTRY_MAX_LOSS <= at_entry <= ENTRY_MAX_LOSS * 1.05, (
        f"the entry stamp is {at_entry}, which is not the 22.00 net debit plus at most a "
        f"modelled spread")
    spec = data[ORDER_PMCC_OVERLAY_KEY]
    assert (spec["dte_min"], spec["dte_max"]) == (30, 45)
    assert spec["strike_param"] == 0.20


def test_the_overlay_is_ROLLED_and_the_leaps_is_not_touched(run_result):
    """THE HEADLINE. ``short_leg_days_to_expiry`` reads the SHORT leg; reading the LEAPS would
    put the roll a year out and the rule would never fire. And the roll must be a ROLL, not a
    close: same transaction, new overlay, LEAPS untouched."""
    account, _ = run_result
    orders = _orders(781)
    rolls = [o for o in orders if o.option_strategy == "pmcc_roll"]
    assert rolls, (
        "no roll ever happened: either short_leg_days_to_expiry read the LEAPS (a year out, "
        "so the rule could never fire) or roll_pmcc_short refused")

    entry = next(o for o in orders
                 if o.parent_order_id is None and o.option_strategy == "pmcc")
    assert all(r.transaction_id == entry.transaction_id for r in rolls), (
        "a roll must stay on the SAME transaction — the structure's identity survives it")

    roll_legs = [o for o in orders if o.parent_order_id in {r.id for r in rolls}]
    assert {l.contract_symbol for l in roll_legs} == {OVERLAY1, OVERLAY2}
    by_contract = {l.contract_symbol: l for l in roll_legs}
    assert by_contract[OVERLAY1].position_intent == "buy_to_close"
    assert by_contract[OVERLAY2].position_intent == "sell_to_open"
    assert LEAPS not in {l.contract_symbol for l in roll_legs}, "the LEAPS was traded on a roll"


def test_the_roll_ticket_closes_the_old_overlay_BEFORE_it_opens_the_new(run_result):
    """Fail-closed, on the rows the engine actually wrote: the child order that buys the
    expiring overlay back is created ahead of the one that sells its replacement."""
    account, _ = run_result
    orders = _orders(781)
    roll = next(o for o in orders if o.option_strategy == "pmcc_roll")
    legs = sorted((o for o in orders if o.parent_order_id == roll.id), key=lambda o: o.id)
    assert [l.position_intent for l in legs] == ["buy_to_close", "sell_to_open"]


def test_the_max_loss_stamp_is_RESTAMPED_by_the_roll(run_result):
    """Design section 3: the intrinsic floor is the LEAPS debit less EVERY credit collected
    since. The roll banks the difference between the buy-back and the re-sale, and the entry
    order's stamp -- ``loss_pct_of_max_loss``'s denominator -- moves with it."""
    account, seen = run_result
    orders = _orders(781)
    entry = next(o for o in orders
                 if o.parent_order_id is None and o.option_strategy == "pmcc")
    roll = next(o for o in orders if o.option_strategy == "pmcc_roll")

    stamped = (entry.data or {})["max_loss_per_contract"]
    at_entry = seen[0]["max_loss"]                       # the stamp the submit seam wrote
    expected = max(0.0, at_entry + float(roll.limit_price) * 100.0)
    assert stamped == pytest.approx(expected), (
        f"the stamp is {stamped} but it was {at_entry} at entry and the roll netted "
        f"{roll.limit_price} per share; the denominator did not follow the credit")
    assert float(roll.limit_price) < 0, (
        "this fixture's overlay decays, so the roll must bank a CREDIT (a negative net under "
        "the house MLEG convention) — otherwise the restamp is being pinned in the direction "
        "the design is not about")
    assert stamped < at_entry, "the accrued credit did not lower the intrinsic floor"


def test_the_structure_exit_reads_the_LONG_leg_and_closes_BOTH_legs(run_result):
    """``days_to_expiry`` reads the LONG leg for a declared two-expiry structure. Reading the
    SHORT one would have flattened the position the first time an overlay approached its own
    expiry -- a LEAPS with a year left, thrown away on schedule.

    And the close must reach the ROLLED overlay, which is a child of the ROLL order: closing
    only what the ENTRY named would sell the LEAPS and leave that overlay standing.
    """
    account, _ = run_result
    orders = _orders(781)
    closes = [o for o in orders if o.option_strategy == "close"]
    assert closes, "the LONG-leg DTE floor never closed the structure"

    close_legs = {o.contract_symbol for o in orders
                  if o.parent_order_id in {c.id for c in closes}}
    assert close_legs == {LEAPS, OVERLAY2}, (
        f"the closing order named {sorted(close_legs)}; it must flatten the LEAPS AND the "
        f"ROLLED overlay, and must not re-reverse the already-flat first overlay")
    assert not account.get_option_positions(), "the structure is still open after its exit"


def test_the_engine_NEVER_held_the_short_without_the_long(run_result):
    """THE INVARIANT, checked on every simulated bar rather than at the end (see the module
    docstring). This assertion is the wrapper's; here we prove the wrapper actually SAW the
    position, so a check that never ran cannot pass by vacuity."""
    account, seen = run_result
    assert seen, "the invariant watcher never saw an open structure — it proved nothing"
    # ``held_legs`` is contract-symbol ordered (so every derived answer is stable), hence the
    # comparison on SETS rather than on the tuple's order.
    held = {frozenset(row["legs"]) for row in seen}
    assert frozenset((LEAPS, OVERLAY1)) in held, "the pre-roll state was never observed"
    assert frozenset((LEAPS, OVERLAY2)) in held, "the post-roll state was never observed"


def test_every_bar_charged_the_structure_as_COVERED_defined_risk(run_result):
    """The rails' question, on the live position: the short call is covered by a long call of
    the same right from the order's own legs, so the committed capital is the WIDTH, never the
    short strike's full notional. A structure measuring as undefined risk would be charged to
    ``undefined_risk_max_pct`` instead of the deployment cap."""
    account, seen = run_result
    committed = {round(row["committed"], 2) for row in seen
                 if row["committed"] is not None}
    assert committed, "no committed-capital measurement was taken"

    entry = next(o for o in _orders(781)
                 if o.parent_order_id is None and o.option_strategy == "pmcc")
    contracts = float(entry.filled_qty or entry.quantity)
    # The WING WIDTH, both sides of the roll: (110 - 80) x 100 while the first overlay is on,
    # (112 - 80) x 100 after. Hand-derived, and asserted as the WHOLE set so a bar charged any
    # other way shows up.
    assert committed == {round(30.0 * 100.0 * contracts, 2),
                         round(32.0 * 100.0 * contracts, 2)}, sorted(committed)
    # ...and never the SHORT STRIKE's full notional, which is what an uncovered short call
    # commits and what the naked sub-cap would have been charged.
    assert max(committed) < 110.0 * 100.0 * contracts


def test_the_structure_is_marked_on_bars_the_LEAPS_has_no_bar_for(run_result):
    """Design section 1 measured 50-65 % bar density at LEAPS range. The equity curve is
    sampled every trading day, so on the barless ones the long leg's mark came from the
    Black-Scholes stage (plan Task 3) or, failing that, its intrinsic floor -- never a hole."""
    account, _ = run_result
    hist = account.get_balance_history()
    vals = [float(h["net_liquidating_value"]) for h in hist]
    assert vals and all(v == v for v in vals), "an unmarked option left a NaN in the curve"
    assert all(v > 0 for v in vals)
    barless = [h for h in hist
               if (h["date"].date() if hasattr(h["date"], "date") else h["date"])
               not in set(LEAPS_BAR_DAYS)]
    assert len(barless) > len(hist) // 3, (
        f"the fixture is not actually sparse: only {len(barless)}/{len(hist)} snapshots fall "
        f"on a day the LEAPS has no bar for")
    assert min(vals) > CFG["starting_cash"] * 0.80, (
        f"the marks moved the account by more than the flat fixture can justify "
        f"(min {min(vals):.2f} vs start {CFG['starting_cash']:.2f})")
