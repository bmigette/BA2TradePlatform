"""B7: a market exit in the REAL backtest engine, and the same decision read by live.

The rules under test are the shared templates (``market_exit_rules``), decoded by the REAL GA
decoder (``decode_params``) and seeded by the REAL seeder (``seed_exit_ruleset_from_rules``, the
unified-rule branch ``run_daily_backtest`` takes). Market conditions come from a pinned, published
tiny manifest (the ``test_research10_market_conditions`` fixture shape, with per-session values),
installed through the real seam ``install_backtest_market_conditions``. The engine is a full
``DailyBacktestEngine.run()`` over synthetic bars with a stub expert that buys once (the B3/B4
engine-test fixture).

Pinned:

1. **Market exit on:** a long whose ``structure_state`` turns ``bear`` is closed by
   ``mkt-exit-structure`` on EXACTLY the bar whose row the reader reports bear (bar D reads its
   own session D, the live decision labelled N(D)), and the close fills at the next bar's open.
   Moving the flip moves the close. With the toggle off the same position is never closed.
2. **All-off templates** (every template rule of both profiles, toggles decoded 0): orders,
   trades, equity and stops are byte-identical to a run with no templates and no profile at all.
   The research-driver path (``build_manifest --market-exit`` -> ``_build_daily_trial_config`` ->
   ``run_daily_backtest``) is pinned the same way on the research10 ETF fixture, where the
   structure close toggled ON closes the held position on the flip session.
3. **BT/live observation parity:** for the flip bar, the backtest context and a LIVE resolver
   (``resolver_for_profiles`` over the same pinned manifest, decision at 10:00 New York during
   session N(D)) agree on ``prior_session`` and read the identical row; and the rule as the live
   export writes it (``trade_rules_to_live_export``), evaluated by the real ``TradeConditions``
   inside a live ``market_condition_decision_scope``, fires for N(D) and not for N(D-1). The full
   live ``TradeManager`` pass is in the root suite, ``tests/test_market_exit_live_parity.py``.
4. **Market stop and TP** (``allow_ruleset_sl_loosen`` unset, i.e. off): the TP rule sets
   TP = open x 1.20 only on bars where BOTH slope > 0 and ADX > 25 hold; the stop rule's
   breakeven request is floored to 3% under the current price when that is too close (still a
   tighten from the 8% safeguard, so it applies), moves to breakeven when the price allows it, and
   is KEPT when a later floored request would loosen it (the ratchet).

Run from the backend dir:
    python -m pytest tests/backtest/test_market_exit_parity_engine.py -v
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ba2_common.core.market_calendar import (
    backtest_decision_label, decision_data_session, next_regular_session, regular_session_dates,
    regular_sessions_ending_at,
)
from ba2_common.core.market_condition_store import MarketConditionStore
from ba2_common.core.market_condition_templates import market_exit_rules
from ba2_common.core.market_conditions import (
    FIELD_ADX, FIELD_STRUCTURE_STATE, FIELD_TREND_SLOPE, PROFILES, STATUS_VALID,
    STRUCTURE_STATE_CODES,
)

from tests.backtest.test_max_loss_stop_engine import CFG, _MaxLossStubExpert, _store_mode

SYMBOL = "AAPL"          # the stub expert prices AAPL
STRUCT, OHLCV = "ta-structure-v1", "ohlcv-v1"
BULL, BEAR = float(STRUCTURE_STATE_CODES["bull"]), float(STRUCTURE_STATE_CODES["bear"])
NY = ZoneInfo("America/New_York")

#: The run's bars: ten regular sessions from 2024-01-02 (2024-01-15 is a holiday).
DAYS = regular_session_dates(date(2024, 1, 2), date(2024, 1, 16))
assert len(DAYS) == 10 and date(2024, 1, 15) not in DAYS
BUY_DAY = DAYS[0]        # signal on the 2024-01-02 close, filled at the 2024-01-03 open


# --------------------------------------------------------------------------- #
# the pinned manifest
# --------------------------------------------------------------------------- #

def publish(root, profile, values, symbols=(SYMBOL,), last=DAYS[-1], n=40):
    """Publish ``profile``'s snapshot for ``symbols`` over the ``n`` sessions ending at ``last``
    and return its digest. ``values(session) -> {field: value}`` overrides the neutral 0.5 of
    every field; every row is VALID. The research10 ``publish`` shape, with per-session values."""
    store = MarketConditionStore(root)
    spec = PROFILES[profile]
    sessions = regular_sessions_ending_at(last, n)
    rows = []
    for day in sessions:
        chosen = values(day)
        rows.append({"session": day,
                     "values": [float(chosen.get(f.name, 0.5)) for f in spec.fields],
                     "status": [STATUS_VALID] * len(spec.fields),
                     "reasons": ["fixture"] * len(spec.fields),
                     "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "",
                     "raw_row_lo": 0, "raw_row_hi": 0})
    objects = []
    for symbol in symbols:
        for month in sorted({r["session"].strftime("%Y-%m") for r in rows}):
            obj, _ = store.write_feature_object(
                spec, symbol, [r for r in rows if r["session"].strftime("%Y-%m") == month])
            objects.append(obj)
    manifest = store.make_manifest(
        spec, source_profile="fmp-daily-split-adjusted-v1", timing_policy="prior_session_v1",
        objects=objects, raw_objects=[], coverage={s: {"rows": len(rows)} for s in symbols},
        universe=symbols, sessions=sessions, window_start=min(sessions), window_end=max(sessions),
        created_at="2026-09-24T00:00:00+00:00")
    return store.write_manifest(manifest)


def structure_flip(flip):
    """``structure_state`` bull before ``flip``, bear from it on."""
    return lambda day: {FIELD_STRUCTURE_STATE: BEAR if day >= flip else BULL}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """The run's cache root: the reader resolves ``ba2_common.config.CACHE_FOLDER``."""
    import ba2_common.config as bc
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# the rules: real templates, real decoder
# --------------------------------------------------------------------------- #

def template_rules(profiles, kinds=("exit", "stop", "tp")):
    return market_exit_rules("b7", list(profiles), "long", kinds)


def decode(exit_rules, on=()):
    """Decode ``exit_rules`` the way a GA trial does: each template's toggle gene is 1 when its
    id is in ``on`` (else 0); every other gene keeps its authored default (the threshold 0 /
    25, the stop at 0% = breakeven, the TP at +20%)."""
    from app.services.strategy_param_space import collect_param_space, decode_params

    strategy = SimpleNamespace(entry_rules=[], exit_rules=exit_rules)
    authored = {}
    for rule in exit_rules:
        for leaf in rule["conditions"]["conditions"]:
            authored[f"cond:{leaf['id']}:value"] = leaf.get("value")
        for i, action in enumerate(rule["actions"]):
            authored[f"exit:{rule['id']}:a{i}:action_value"] = action.get("action_value")
    if not any(r.get("toggle_optimize") for r in exit_rules):
        return decode_params(strategy, {})["exit_rules"]    # no gene at all (nothing searched)
    genome = {}
    for gene in collect_param_space(strategy, {}):
        genome[gene] = int(gene.split(":")[1] in on) if gene.endswith(":enabled") else authored[gene]
    return decode_params(strategy, genome)["exit_rules"]


def rule_id(kind_suffix):
    return f"b7-mkt-{kind_suffix}"


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #

def _bars(closes, lows=None):
    """Daily bars: open = previous close (100 on the first bar), high = max(open, close) + 0.5,
    low = min(open, close) - 0.5 unless ``lows`` pins it."""
    out, prev = [], 100.0
    for i, (day, close) in enumerate(zip(DAYS, closes)):
        low = (lows or {}).get(day, min(prev, close) - 0.5)
        out.append((day, prev, max(prev, close) + 0.5, low, close))
        prev = close
    return out


def _run(bars, *, run_id, inmem, exit_rules, pins=None):
    """One full ``engine.run()``. ``pins`` ({profile: digest}) installs the market-condition
    resolver through the real seam; None runs with no profile at all. Returns the outcome, the
    bars ``close_transaction`` was called on, and a per-bar snapshot of the entry transaction's
    (stop_loss, take_profit) taken right after each open-positions pass."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_enter_long_ruleset, seed_exit_ruleset_from_rules)
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import (
        clear_backtest_market_conditions, install_backtest_market_conditions, wire_backtest_seams)
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection

    account_id = expert_id = run_id
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"market-exit-parity-{run_id}")
    ctx.__enter__()
    try:
        from ba2_common.core import trade_store
        assert trade_store.inmem_trades_active() == (inmem == "1"), "store mode is not the one asked for"
        seed_account_definition(account_id, CFG)
        enter_id = seed_enter_long_ruleset()
        open_id = seed_exit_ruleset_from_rules(exit_rules, name=f"market-exit-open-{run_id}")
        seed_expert_instance(account_id=account_id, expert_class_name="_MaxLossStubExpert",
                             enter_market_ruleset_id=enter_id, open_positions_ruleset_id=open_id,
                             instance_id=expert_id)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars(SYMBOL, [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c,
                               "Volume": 1000} for (d, o, h, low, c) in bars])
        account = BacktestAccount(account_id, ps, CFG)
        resolver.register_account(account_id, account)
        expert = _MaxLossStubExpert(expert_id, ps, buy_on={BUY_DAY})
        expert.save_settings({
            "allow_automated_trade_opening": (True, "bool"),
            "allow_automated_trade_modification": (True, "bool"),
            "enable_buy": (True, "bool"),
            "sizing_mode": ("risk_atr", "str"),
            "risk_per_trade_pct": (8.0, "float"),
            "min_stop_loss_pct": (8.0, "float"),
            "use_atr_stop": (False, "bool"),
        })
        resolver.register_expert(expert_id, expert)
        start = datetime.combine(bars[0][0], time.min)
        end = datetime.combine(bars[-1][0], time.min)

        mc_resolver = None
        if pins is not None:
            mc_config = {
                "market_condition_profiles": list(pins), "market_condition_manifests": dict(pins),
                "enabled_instruments": [SYMBOL], "start_date": start, "end_date": end,
                "exit_rules": exit_rules,
                "experts": [{"class": "_MaxLossStubExpert",
                             "settings": {"market_condition_profile": ",".join(pins)}}],
            }
            mc_resolver = install_backtest_market_conditions(mc_config, ps)
            assert mc_resolver is not None and mc_resolver.reader is not None

        closes = []
        real_close = account.close_transaction

        def spy_close(transaction_id, *a, **k):
            closes.append(account._as_of_date())
            return real_close(transaction_id, *a, **k)

        account.close_transaction = spy_close

        snapshots = {}
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, {}, enter_id)], price_source=ps,
            config={"start_date": start, "end_date": end, "enabled_instruments": [SYMBOL], "seed": 42},
            indicator_provider=None)
        engine._indicator_provider = None
        real_manage = engine._manage_open_positions

        def manage_and_snapshot(expert_, expert_id_, settings_, as_of):
            real_manage(expert_, expert_id_, settings_, as_of)
            entries = [o for o in account.get_orders() if o.symbol == SYMBOL
                       and o.side == OrderDirection.BUY and o.depends_on_order is None
                       and o.transaction_id is not None]
            if entries:
                txn = get_instance(Transaction, min(entries, key=lambda o: o.id).transaction_id)
                snapshots[as_of.date()] = (txn.stop_loss, txn.take_profit)

        engine._manage_open_positions = manage_and_snapshot
        try:
            engine.run()
        finally:
            clear_backtest_market_conditions()

        orders = sorted(
            (o.side.value, o.order_type.value, o.status.value, o.quantity, o.filled_qty,
             o.open_price, o.stop_price, o.limit_price, o.depends_on_order is None)
            for o in account.get_orders())
        outcome = {"orders": orders, "trades": account.get_round_trip_trades(),
                   "equity": list(account.get_balance_history())}
        return outcome, closes, snapshots, mc_resolver
    finally:
        ctx.__exit__(None, None, None)


def _day(value):
    return value.date() if isinstance(value, datetime) else value


# --------------------------------------------------------------------------- #
# 1. the market exit closes on the session the reader reports
# --------------------------------------------------------------------------- #

#: Gently rising, far from the 92 safeguard stop: nothing but the market exit can close it.
RISING = _bars([100, 101, 102, 103, 104, 105, 106, 107, 108, 109])


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
@pytest.mark.parametrize("flip", [DAYS[4], DAYS[6]], ids=lambda d: f"flip-{d}")
def test_market_exit_closes_on_the_flip_session(cache, monkeypatch, inmem, flip):
    _store_mode(monkeypatch, inmem)
    pins = {STRUCT: publish(cache, STRUCT, structure_flip(flip))}
    rules = template_rules([STRUCT], kinds=("exit",))
    assert [r["id"] for r in rules] == [rule_id("exit-structure")]
    run_id = 710 + DAYS.index(flip) + (100 if inmem == "0" else 0)

    on, closes, _, mc = _run(RISING, run_id=run_id, inmem=inmem,
                             exit_rules=decode(rules, on={rule_id("exit-structure")}), pins=pins)

    # The reader reports bear first for the flip session, and bar D reads its own session D.
    assert mc.reader.observe(SYMBOL, flip).values[FIELD_STRUCTURE_STATE].value == BEAR
    before = DAYS[DAYS.index(flip) - 1]
    assert mc.reader.observe(SYMBOL, before).values[FIELD_STRUCTURE_STATE].value == BULL
    assert decision_data_session(backtest_decision_label(flip)) == flip

    assert closes == [flip], "the market exit must fire on the flip bar, and only once"
    (trade,) = on["trades"]
    assert trade["exit_reason"] == "exit"
    # The engine's clock: an order submitted on bar D fills on D's fill step at the NEXT bar's
    # open, stamped D (``BacktestAccount._bar_for_fill``, next_bar_open).
    assert _day(trade["exit_time"]) == flip
    next_open = next(o for (d, o, *_rest) in RISING if d == next_regular_session(flip))
    assert trade["exit_price"] == pytest.approx(next_open)

    # Toggle off: the rule is absent after decode, and the same position is never closed.
    off_rules = decode(rules, on=())
    assert off_rules == []
    off, closes_off, _, _ = _run(RISING, run_id=run_id + 50, inmem=inmem, exit_rules=off_rules,
                                 pins=pins)
    assert closes_off == []
    (held,) = off["trades"]
    assert held["exit_reason"] == "open_at_end"


# --------------------------------------------------------------------------- #
# 2. all-off templates are byte-identical to no templates
# --------------------------------------------------------------------------- #

#: A plain, non-market exit rule both arms carry, so the ruleset really manages the position
#: (it closes on the bar the position is 5% up: 2024-01-09 at the 105 close).
BASE_EXIT = {"id": "base-pl-close", "name": "base-pl-close",
             "conditions": {"type": "AND", "conditions": [
                 {"id": "base-pl", "field": "profit_loss_percent", "op": ">=", "value": 5.0}]},
             "actions": [{"action_type": "close"}]}


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_all_off_templates_match_no_templates_byte_for_byte(cache, monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    # A fixture on which EVERY template would act if it were on: structure bear from DAYS[4]
    # (exit + stop), slope 0.1 with ADX 30 throughout (TP).
    pins = {OHLCV: publish(cache, OHLCV, lambda day: {FIELD_TREND_SLOPE: 0.1, FIELD_ADX: 30.0}),
            STRUCT: publish(cache, STRUCT, structure_flip(DAYS[4]))}
    templates = template_rules([OHLCV, STRUCT])
    assert [r["id"] for r in templates] == [rule_id(k) for k in
                                            ("exit-structure", "exit-slope", "stop", "tp")]
    run_id = 740 + (100 if inmem == "0" else 0)

    decoded = decode([BASE_EXIT] + templates, on=())
    assert decoded == decode([BASE_EXIT]), "all-off must decode to exactly the base rules"
    off, closes_off, snaps_off, _ = _run(RISING, run_id=run_id, inmem=inmem,
                                         exit_rules=decoded, pins=pins)
    plain, closes_plain, snaps_plain, _ = _run(RISING, run_id=run_id + 1, inmem=inmem,
                                               exit_rules=decode([BASE_EXIT]), pins=None)

    assert closes_plain == [DAYS[5]], "fixture drifted: the base rule should close at +5%"
    assert off["orders"] == plain["orders"]
    assert off["trades"] == plain["trades"]
    assert off["equity"] == plain["equity"]
    assert (closes_off, snaps_off) == (closes_plain, snaps_plain)

    # The same fixture with every template ON is a different run: the comparison has teeth.
    on, closes_on, _, _ = _run(RISING, run_id=run_id + 2, inmem=inmem, pins=pins,
                               exit_rules=decode([BASE_EXIT] + templates,
                                                 on={r["id"] for r in templates}))
    assert closes_on == [DAYS[4]] and on["trades"] != plain["trades"]


def test_research_driver_market_exits_all_off_parity_and_structure_close(tmp_path, monkeypatch):
    """The GA path end to end on the research10 ETF fixture: ``build_manifest(market_exit=...)``
    with both profiles pinned, through ``_build_daily_trial_config`` and ``run_daily_backtest``.

    * every entry mode and every exit toggle decoded off: identical to the job with no profile
      at all (fills, quantities, P&L, equity curve);
    * only the structure-close toggle on: the held position closes on the flip session."""
    import logging

    import ba2_common.config as bc
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import collect_param_space, decode_params
    from tests.backtest.fixtures import hermetic_providers as fixtures
    from tests.backtest.fixtures.e2e_support import hermetic_providers

    # The checkout root is on sys.path via tests/backtest/conftest.py (as test_etf_trend uses).
    from tools.strategy_research.exploration import profiles as P

    monkeypatch.setattr(fixtures, "_BASE_START", date(2023, 1, 2))
    monkeypatch.setattr(fixtures, "_N_BARS", 400)
    monkeypatch.setattr(fixtures, "_PRICE_ROWS", {s: fixtures._build_price_rows(s) for s in ("AAPL", "MSFT")})
    monkeypatch.setattr(bc, "CACHE_FOLDER", str(tmp_path))
    end = date(2024, 2, 16)
    # The structure flips bear mid-window and the TP conditions hold: an ON template would act.
    pins = {OHLCV: publish(tmp_path, OHLCV, lambda d: {FIELD_TREND_SLOPE: 0.1, FIELD_ADX: 30.0},
                           symbols=("AAPL", "MSFT"), last=end, n=150),
            STRUCT: publish(tmp_path, STRUCT, structure_flip(date(2024, 2, 8)),
                            symbols=("AAPL", "MSFT"), last=end, n=150)}
    results = {}
    for arm in ("none", "all-off", "exit-on"):
        extra = {} if arm == "none" else {
            "market_condition_profile": f"{OHLCV},{STRUCT}",
            "market_condition_manifest": ",".join(f"{p}={d}" for p, d in pins.items()),
            "market_exit": ("exit", "stop", "tp")}
        job = P.build_manifest(families=["etf_trend"], search="genetic", **extra)["jobs"][0]
        bt = job["optimization_config"]["backtest"]
        bt.update(backtest_id="equity-market-exit-parity", start_date="2024-02-01", end_date="2024-02-16",
                  warmup_days=90, enabled_instruments=["AAPL", "MSFT"], execution_interval="1d",
                  run_schedule_override=None, manage_schedule_override=None)
        bt["experts"][0]["settings"].update(universe_symbols=["AAPL", "MSFT"], momentum_bars=5,
                                            trend_bars=5, top_n=1)
        genes = collect_param_space(SimpleNamespace(**job["strategy"]), job["optimization_config"]["expert_params"])
        if arm != "none":
            assert bt["market_exit"]["rules"], "the job must carry the market exit templates"
            assert any(g.endswith(":enabled") for g in genes)
        params = {g: "off" for g in genes if g.endswith(":mode")}
        params.update({g: int(arm == "exit-on" and "-mkt-exit-structure:" in g)
                       for g in genes if g.endswith(":enabled")})
        params.update({g: 0.0 for g in genes if g.startswith("cond:") and g.endswith(":value")})
        params.update({g: 20.0 if "-mkt-tp:" in g else 0.0
                       for g in genes if g.endswith(":action_value")})
        decoded = decode_params(SimpleNamespace(**job["strategy"]), params)
        config = _build_daily_trial_config(bt, decoded, option_trade_records=False)
        before = logging.root.manager.disable
        try:
            logging.disable(logging.INFO)
            with hermetic_providers():
                results[arm] = run_daily_backtest(config)
        finally:
            logging.disable(before)
    assert results["none"]["total_trades"] >= 1
    for key in ("final_equity", "total_trades", "max_drawdown", "total_return", "equity_curve"):
        assert results["all-off"][key] == results["none"][key], key

    def economic_trades(result):
        return [{k: v for k, v in trade.items() if k != "entry_state"} for trade in result["trades"]]
    assert economic_trades(results["all-off"]) == economic_trades(results["none"])

    # The structure close ON, same genome otherwise: the position the ungated run holds to the
    # end is closed by the market exit on exactly the flip session, 2024-02-08.
    held, closed = results["none"]["trades"][0], results["exit-on"]["trades"][0]
    assert held["exit_reason"] == "open_at_end" and held["exit_time"] > "2024-02-08"
    assert closed["entry_time"] == held["entry_time"] and closed["size"] == held["size"]
    assert closed["exit_reason"] == "exit"
    assert closed["exit_time"].startswith("2024-02-08"), closed["exit_time"]


# --------------------------------------------------------------------------- #
# 3. the backtest and a live resolver read the same observation
# --------------------------------------------------------------------------- #

def _live_decision_at(session):
    """10:00 New York during ``session``: a live decision labelled ``session``."""
    return datetime.combine(session, time(10, 0), tzinfo=NY).astimezone(timezone.utc)


def test_backtest_and_live_read_the_same_observation_and_fire_the_same_rule(cache, monkeypatch):
    import ba2_common.core.TradeConditions as TC
    from ba2_common.core import market_condition_live as live
    from ba2_common.core.rules_convert import trade_rules_to_live_export
    from ba2_common.core.types import ExpertEventType

    flip = DAYS[4]
    before = DAYS[3]
    digest = publish(cache, STRUCT, structure_flip(flip))
    on_rules = decode(template_rules([STRUCT], kinds=("exit",)), on={rule_id("exit-structure")})
    _, closes, _, bt = _run(RISING, run_id=770, inmem="1", exit_rules=on_rules, pins={STRUCT: digest})
    assert closes == [flip]

    # The live rule is what the deploy writes: one trigger, the resolved categorical leaf.
    (ruleset,) = trade_rules_to_live_export(exit_rules=on_rules)["rulesets"]
    (rule,) = ruleset["rules"]
    (trigger,) = rule["triggers"].values()
    assert trigger == {"event_type": FIELD_STRUCTURE_STATE, "operator": "==", "value": BEAR}
    assert rule["actions"] and rule["continue_processing"] is False

    live_resolver = live.resolver_for_profiles([STRUCT], manifest_digests={STRUCT: digest},
                                               cache_root=str(cache))
    saved = TC.get_market_condition_context_resolver()
    TC.set_market_condition_context_resolver(live_resolver)
    try:
        fired = {}
        for bar in (before, flip):
            bt_ctx = bt(SimpleNamespace(_as_of_date=lambda bar=bar: bar), SYMBOL, None)
            label = next_regular_session(bar)
            assert bt_ctx.session_label == label and bt_ctx.prior_session == bar
            monkeypatch.setattr(live, "_replay_now", lambda label=label: _live_decision_at(label))
            with live.market_condition_decision_scope() as state:
                live_ctx = state.context()
                assert live_ctx.session_label == bt_ctx.session_label
                assert live_ctx.prior_session == bt_ctx.prior_session == bar
                bt_row = bt_ctx.reader.observe(SYMBOL, bar)
                live_row = live_ctx.reader.observe(SYMBOL, live_ctx.prior_session)
                assert dict(live_row.values) == dict(bt_row.values), "BT and live read different rows"
                assert live_row.values[FIELD_STRUCTURE_STATE].status == STATUS_VALID
                leaf = TC.create_condition(ExpertEventType(trigger["event_type"]), object(), SYMBOL,
                                           None, operator_str=trigger["operator"],
                                           value=trigger["value"])
                fired[bar] = leaf.evaluate()
                assert leaf.last_status == STATUS_VALID
        assert fired == {before: False, flip: True}
    finally:
        TC.set_market_condition_context_resolver(saved)


# --------------------------------------------------------------------------- #
# 4. market stop and TP in the real engine (allow_ruleset_sl_loosen off)
# --------------------------------------------------------------------------- #

#: Closes per bar. Signal 2024-01-02 at 100, filled at the 2024-01-03 open (100): open price 100,
#: the risk manager's safeguard stop 92 (8%). Lows stay above every stop and highs under the TP.
MANAGED = _bars([100, 101, 101, 101, 105, 110, 102, 103, 104, 105])

#: Per-session market rows (anything not listed: structure bull, slope -0.1, ADX 20).
MANAGED_ROWS = {
    DAYS[1]: {FIELD_TREND_SLOPE: 0.1, FIELD_ADX: 20.0},                 # ADX fails: no TP
    DAYS[2]: {FIELD_TREND_SLOPE: -0.1, FIELD_ADX: 30.0},                # slope fails: no TP
    DAYS[3]: {FIELD_STRUCTURE_STATE: BEAR, FIELD_TREND_SLOPE: 0.0,      # slope == 0 is not > 0
              FIELD_ADX: 30.0},
    DAYS[4]: {FIELD_TREND_SLOPE: 0.1, FIELD_ADX: 30.0},                 # TP only
    DAYS[5]: {FIELD_STRUCTURE_STATE: BEAR, FIELD_TREND_SLOPE: 0.1, FIELD_ADX: 30.0},
    DAYS[6]: {FIELD_STRUCTURE_STATE: BEAR, FIELD_TREND_SLOPE: 0.1, FIELD_ADX: 30.0},
}


def _managed_values(profile):
    neutral = {FIELD_STRUCTURE_STATE: BULL, FIELD_TREND_SLOPE: -0.1, FIELD_ADX: 20.0}
    fields = {f.name for f in PROFILES[profile].fields}

    def values(day):
        row = {**neutral, **MANAGED_ROWS.get(day, {})}
        return {k: v for k, v in row.items() if k in fields}
    return values


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_market_stop_and_take_profit_in_the_engine(cache, monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    pins = {p: publish(cache, p, _managed_values(p)) for p in (OHLCV, STRUCT)}
    rules = template_rules([OHLCV, STRUCT], kinds=("stop", "tp"))
    assert [r["id"] for r in rules] == [rule_id("stop"), rule_id("tp")]
    run_id = 780 + (100 if inmem == "0" else 0)

    on, closes, snaps, _ = _run(MANAGED, run_id=run_id, inmem=inmem, pins=pins,
                                exit_rules=decode(rules, on={rule_id("stop"), rule_id("tp")}))
    assert closes == []
    (trade,) = on["trades"]
    assert trade["exit_reason"] == "open_at_end" and trade["entry_price"] == pytest.approx(100.0)

    floored = 101 * (1 - 0.03)          # breakeven 100 is 0.99% under 101: the 3% floor wins
    expected = {
        DAYS[1]: (92.0, None),          # safeguard stop, no TP: ADX 20 fails the TP rule
        DAYS[2]: (92.0, None),          # slope below 0 fails the TP rule
        DAYS[3]: (floored, None),       # bear: breakeven floored to 97.97, still a tighten
        DAYS[4]: (floored, 120.0),      # slope 0.1 and ADX 30: TP = open x 1.20
        DAYS[5]: (100.0, 120.0),        # bear at 110: breakeven is 9% away, applied as is
        DAYS[6]: (100.0, 120.0),        # bear at 102: floored 98.94 would LOOSEN: kept at 100
        DAYS[7]: (100.0, 120.0),
        DAYS[8]: (100.0, 120.0),
        DAYS[9]: (100.0, 120.0),
    }
    got = {d: snaps.get(d) for d in expected}
    for day, (sl, tp) in expected.items():
        assert got[day] is not None, (day, snaps)
        assert got[day][0] == pytest.approx(sl), (day, got)
        assert (got[day][1] is None) if tp is None else (got[day][1] == pytest.approx(tp)), (day, got)

    # Toggles off: the same fixture leaves the safeguard stop and no TP on every bar.
    _, _, snaps_off, _ = _run(MANAGED, run_id=run_id + 1, inmem=inmem, pins=pins,
                              exit_rules=decode(rules, on=()))
    assert all(snaps_off[d][0] == pytest.approx(92.0) and snaps_off[d][1] is None
               for d in expected), snaps_off
