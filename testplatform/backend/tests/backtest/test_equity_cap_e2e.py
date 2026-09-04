"""The equity cap end to end, through the REAL engine.

Unit tests on the two conversions are necessary and not sufficient: the feature is a number
flowing through the sizer, the buying-power gate, the margin check, the equity recorder and the
metric builder, and every defect of this shape in this codebase has been a seam that each side
tested correctly in isolation.

Two harnesses, both existing ones rather than new inventions:

  * ``tests.backtest.fixtures.e2e_support`` -- the hermetic ``handle_daily_backtest`` runner
    the Phase-2 GATE tests use. Drives the cap-off GOLDEN comparison.
  * the direct ``DailyBacktestEngine`` construction from ``test_entry_bracket_engine._acct`` /
    ``test_config_entry_rules_reach_build_experts_and_set_stop_loss`` -- a real account, a real
    ``_build_experts``, a real engine loop. Drives the multi-year runs, because the hermetic
    fixture is a 26-bar buy-and-hold window and start-date invariance needs many round trips
    across several years.

THE PRICE FIXTURE (``_bars``) is deliberately phase-locked: a 5-bar cycle in which the expert
signals BUY only on bar 0 and bar 2 carries a high that crosses the entry bracket's +8% take
profit. So every cycle is one complete, identical round trip, and a run started at any cycle
boundary sees exactly the state a longer run sees at that boundary. Without that lock, a
comparison between two start dates measures phase drift as well as the cap.

Frozen throughout: 2020-01-01 onward, a literal, never "today".
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation

from tests.backtest.fixtures.e2e_support import (
    earnings_drift_payload,
    ensure_host_schema,
    insider_cluster_payload,
    load_backtest,
    new_backtest_row,
    run_daily_backtest,
)

_GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "equity_cap_golden.json")

#: Columns the golden pins. Every headline metric ``Backtest`` persists.
_GOLDEN_COLUMNS = (
    "status", "total_trades", "winning_trades", "losing_trades", "win_rate",
    "total_return", "annualized_return", "sharpe_ratio", "sortino_ratio",
    "calmar_ratio", "volatility", "max_drawdown", "avg_drawdown",
    "max_drawdown_duration", "profit_factor", "expectancy", "sqn", "avg_trade",
    "best_trade", "worst_trade", "avg_trade_duration", "exposure_time",
    "final_equity", "equity_peak",
)


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    ensure_host_schema()
    yield


# =========================================================================== #
# Step 1: the no-regression golden run
# =========================================================================== #
def _golden_snapshot(backtest_id: int) -> Dict[str, Any]:
    bt = load_backtest(backtest_id)
    return json.loads(json.dumps({
        "columns": {c: getattr(bt, c) for c in _GOLDEN_COLUMNS},
        "equity_curve": bt.equity_curve,
        "drawdown_curve": bt.drawdown_curve,
        "trades": bt.trades,
    }, sort_keys=True, default=str))


@pytest.mark.parametrize("name,make_payload", [
    ("earnings_drift", earnings_drift_payload),
    ("insider_cluster", insider_cluster_payload),
])
def test_a_run_with_no_cap_is_identical_to_before_the_feature(name, make_payload):
    """Off by default must mean NOTHING moved -- proven by comparing full results against a
    run captured from the PRE-feature code, not by the suite staying green. A green suite
    proves nothing broke that a test already covered; this proves nothing moved at all.

    ``fixtures/equity_cap_golden.json`` was produced by running this exact hermetic payload in
    a detached worktree at commit 346fdabb -- the commit immediately before the first
    equity-cap change -- and is re-generated the same way if the engine legitimately changes.

    2026-09-04: the trade rows gained ``option_type`` / ``strike`` / ``expiry`` so the test
    platform's trade list can say what an option leg IS. The golden was NOT re-generated for
    it -- the three keys were inserted as nulls, by hand, leaving every existing value byte
    for byte. That is the whole point of the fixture: a SCHEMA addition that is null on every
    equity row moved no number, and re-running to capture it would have hidden any number
    that did move behind the same refresh.
    """
    with open(_GOLDEN_PATH, encoding="utf-8") as fh:
        golden = json.load(fh)

    bt_id = new_backtest_row(f"equity-cap-golden-{name}")
    result = run_daily_backtest(make_payload(bt_id, seed=42), task_id=f"cap-golden-{name}")
    assert result["status"] == "completed", result.get("error")

    got = _golden_snapshot(bt_id)
    assert got["columns"] == golden[name]["columns"]
    assert got["equity_curve"] == golden[name]["equity_curve"]
    assert got["drawdown_curve"] == golden[name]["drawdown_curve"]
    assert got["trades"] == golden[name]["trades"]


def test_the_golden_fixture_is_not_vacuous():
    """A golden that pins an empty run would pass forever. This one traded and moved money."""
    with open(_GOLDEN_PATH, encoding="utf-8") as fh:
        golden = json.load(fh)
    for name, blob in golden.items():
        assert blob["columns"]["status"] == "completed", name
        assert blob["columns"]["total_trades"] >= 1, name
        assert blob["columns"]["total_return"] != 0.0, name
        assert len(blob["equity_curve"]) > 1, name


# =========================================================================== #
# The multi-year engine harness
# =========================================================================== #
_CYCLE = 5                       # bars per complete round trip
_PRICE = 100.0
_TP_HIGH = 115.0                 # crosses the +8% (108.0) take profit
_START = datetime(2020, 1, 1)    # frozen; never "today"
_TAKE_PROFIT_PCT = 8.0

#: date -> global bar index, so the stub expert can signal on a fixed phase. Populated by
#: ``_bars`` (the full series is always built first, then sliced).
_BAR_INDEX: Dict[Any, int] = {}
_PRICE_SOURCE: Dict[str, Any] = {"src": None}


class _CycleBuyer(MarketExpertInterface):
    """BUYs on bar 0 of every cycle, HOLDs otherwise.

    Gating the signal on the bar's phase (rather than "buy whenever flat") is what makes the
    round trips exactly periodic: entry on bar 0 -> fill at bar 1's open -> take profit on bar
    2's high -> flat for bars 3-4 -> repeat. A run beginning at any cycle boundary therefore
    starts in the same state a longer run is in at that boundary.
    """

    @classmethod
    def description(cls) -> str:
        return "Phase-locked BUY stub for the equity-cap end-to-end runs."

    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {}

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        symbol = getattr(self, "_gather_symbol", "AAPL")
        idx = _BAR_INDEX.get(as_of.replace(tzinfo=None).date())
        signal = (OrderRecommendation.BUY if (idx is not None and idx % _CYCLE == 0)
                  else OrderRecommendation.HOLD)
        source = _PRICE_SOURCE["src"]
        price = source.close_at(symbol) or source.close_asof(symbol)
        return Recommendation(signal=signal, confidence=80.0, current_price=price,
                              details="equity-cap cycle", expected_profit_percent=10.0)


def _business_days(start: datetime, n: int) -> List[datetime]:
    out, day = [], start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _bars(n_cycles: int) -> List[Dict[str, Any]]:
    """The full phase-locked series, and (re)populate ``_BAR_INDEX`` for it."""
    days = _business_days(_START, n_cycles * _CYCLE)
    _BAR_INDEX.clear()
    rows = []
    for i, day in enumerate(days):
        _BAR_INDEX[day.date()] = i
        rows.append({
            "Date": day,
            "Open": _PRICE, "Close": _PRICE, "Low": _PRICE - 0.5,
            "High": (_TP_HIGH if (i % _CYCLE) == 2 else _PRICE + 0.5),
            "Volume": 1_000_000,
        })
    return rows


_ACCOUNT_SEQ = [7000]


def run_engine(rows: List[Dict[str, Any]], *, equity_cap: Optional[float],
               initial_capital: float = 20_000.0, commission: float = 10.0,
               on_bar=None) -> Dict[str, Any]:
    """One REAL engine run over ``rows``. Returns the real ``build_results`` output plus the
    account, the round-trip trades and (optionally) a per-bar sample.

    ``commission`` is non-zero on purpose. A pure percent-of-equity sizer earning a pure
    percentage return is scale-invariant, so it would show no start-date dependence to remove.
    A fixed per-trade cost is the ordinary, real reason a small account and a large one do NOT
    earn the same percentage -- which is exactly the artifact the cap exists to hold still.
    """
    from app.services.backtest import daily_backtest_handler as H
    from app.services.backtest import results as R
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition,
    )
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    _ACCOUNT_SEQ[0] += 1
    account_id = _ACCOUNT_SEQ[0]
    H._SUPPORTED_EXPERTS["_CycleBuyer"] = "tests.backtest.test_equity_cap_e2e"

    account_settings = {
        "starting_cash": float(initial_capital),
        "commission_per_trade": float(commission),
        "slippage_bps": 0.0,
        "fill_model": "next_bar_open",
        "equity_cap": equity_cap,
    }
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"equity-cap-e2e-{account_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, account_settings)
        price = AsOfPriceSource(ohlcv_provider=None)
        price.load_bars("AAPL", rows)
        _PRICE_SOURCE["src"] = price
        account = BacktestAccount(account_id, price, account_settings)
        resolver.register_account(account_id, account)
        price.set_clock(rows[0]["Date"])

        if on_bar is not None:
            original = account.snapshot_equity

            def _sampling_snapshot(as_of):
                # snapshot_equity is called exactly once per simulated bar, after fills are
                # rolled -- i.e. it IS the per-bar sampling point.
                on_bar(account)
                return original(as_of)

            account.snapshot_equity = _sampling_snapshot  # type: ignore[assignment]

        engine_config = {
            "experts": ["_CycleBuyer"],
            "enabled_instruments": ["AAPL"],
            "entry_rules": [{"id": "cap_tp", "action_type": "adjust_take_profit",
                             "reference_value": "order_open_price",
                             "action_value": _TAKE_PROFIT_PCT}],
        }
        built = H._build_experts(engine_config, resolver, account_id)
        engine = DailyBacktestEngine(
            account=account, experts=built, price_source=price,
            config={"start_date": rows[0]["Date"], "end_date": rows[-1]["Date"],
                    "enabled_instruments": ["AAPL"], "seed": 42},
            indicator_provider=None,
        )
        engine._indicator_provider = None
        engine.run()

        trades = account.get_round_trip_trades()
        results = R.build_results(account, {"initial_capital": float(initial_capital),
                                            "account_settings": account_settings})
        return {"account": account, "trades": trades, "results": results,
                "final_equity": account.equity()}
    finally:
        ctx.__exit__(None, None, None)


def _annualized_over(equity_curve: List[Dict[str, Any]], from_iso: str) -> float:
    """Annualised return of the slice of ``equity_curve`` starting at ``from_iso``.

    The same compounding formula ``results._annualized_return`` uses, applied to a window
    rather than the whole run, so "what did the 2020 run earn from 2022 onward" is directly
    comparable with "what did the 2022 run earn".
    """
    points = [p for p in equity_curve if str(p["date"])[:10] >= from_iso]
    assert len(points) > 1, f"no window to measure from {from_iso}"
    start = datetime.fromisoformat(str(points[0]["date"])[:19])
    end = datetime.fromisoformat(str(points[-1]["date"])[:19])
    years = (end - start).days / 365.25
    assert years > 0
    return ((points[-1]["equity"] / points[0]["equity"]) ** (1.0 / years) - 1.0) * 100.0


_N_CYCLES = 200                  # ~1,000 business days ~ 4 calendar years
_HALF = _N_CYCLES // 2
_CAP = 20_000.0


@pytest.fixture(scope="module")
def full_series():
    return _bars(_N_CYCLES)


@pytest.fixture(scope="module")
def late_series(full_series):
    """The tail of the SAME series, starting on a cycle boundary."""
    return full_series[_HALF * _CYCLE:]


# =========================================================================== #
# Step 2: deployed equity never exceeds the cap, sampled every bar
# =========================================================================== #
def test_deployed_equity_never_exceeds_the_cap_across_a_whole_run(full_series):
    seen: List[float] = []
    run = run_engine(full_series, equity_cap=_CAP,
                     on_bar=lambda a: seen.append(a.deployed_equity()))
    assert seen, "the run produced no bars"
    assert max(seen) <= _CAP + 1e-9, f"deployed {max(seen):,.2f} exceeded the cap"
    assert run["final_equity"] > _CAP, \
        "this run never grew past the cap, so it does not test anything"


def test_the_same_run_uncapped_does_exceed_that_figure(full_series):
    """The control: without the cap the very same run deploys far more than 20k, so the
    assertion above is measuring the cap and not the fixture's modesty."""
    seen: List[float] = []
    run = run_engine(full_series, equity_cap=None,
                     on_bar=lambda a: seen.append(a.deployed_equity()))
    assert max(seen) > _CAP * 2, f"uncapped peak deployed only {max(seen):,.2f}"
    assert run["final_equity"] > _CAP * 2


def test_the_recorded_equity_curve_of_a_capped_run_is_the_REAL_one(full_series):
    """The recorded history stays real even though everything spendable is capped."""
    run = run_engine(full_series, equity_cap=_CAP)
    recorded = [s["net_liquidating_value"] for s in run["account"].get_balance_history()]
    assert max(recorded) > _CAP, \
        "the cap reached snapshot_equity -- the run's own history is now unreconstructable"
    assert max(recorded) == pytest.approx(run["final_equity"], rel=1e-9)


# =========================================================================== #
# Step 3: THE feature test -- the start date must not matter
# =========================================================================== #
def test_the_same_strategy_scores_the_same_started_in_year_three_as_in_year_one(
        full_series, late_series):
    """This IS the feature, and it can only be shown end to end."""
    cut = late_series[0]["Date"].date().isoformat()
    from_2020 = run_engine(full_series, equity_cap=_CAP)
    from_late = run_engine(late_series, equity_cap=_CAP)

    early_tail = _annualized_over(from_2020["results"]["equity_curve"], cut)
    late_whole = from_late["results"]["annualized_return"]
    assert early_tail == pytest.approx(late_whole, abs=0.5), (
        f"a capped strategy scored {early_tail:.3f}% over {cut}+ when started in 2020 but "
        f"{late_whole:.3f}% when started at {cut} -- the start date still matters")


def test_without_the_cap_the_start_date_DOES_matter(full_series, late_series):
    """The control that gives the test above its meaning. Same strategy, same data, same
    windows -- only the cap removed -- and the two numbers separate.

    The mechanism is the ordinary one: a fixed per-trade cost is a bigger drag on a small
    account, so the run that had already compounded for two years earns a visibly higher
    PERCENTAGE over the shared window than the one that starts there with the original
    capital. That is precisely the "later results carried by earlier luck" artifact.
    """
    cut = late_series[0]["Date"].date().isoformat()
    from_2020 = run_engine(full_series, equity_cap=None)
    from_late = run_engine(late_series, equity_cap=None)

    early_tail = _annualized_over(from_2020["results"]["equity_curve"], cut)
    late_whole = from_late["results"]["annualized_return"]
    assert abs(early_tail - late_whole) > 1.0, (
        f"uncapped, the two windows scored {early_tail:.3f}% and {late_whole:.3f}% -- if they "
        f"already agree, the capped test above proves nothing")


# =========================================================================== #
# Step 4: the cap reached the SIZER, not just the metrics
# =========================================================================== #
def test_a_capped_run_opens_visibly_smaller_positions_than_an_uncapped_one(full_series):
    uncapped = run_engine(full_series, equity_cap=None)
    capped = run_engine(full_series, equity_cap=5_000.0)
    assert capped["trades"], "the capped run placed no orders at all"

    def _notional(t):
        return float(t["size"]) * float(t["entry_price"])

    max_capped = max(_notional(t) for t in capped["trades"])
    max_uncapped = max(_notional(t) for t in uncapped["trades"])
    assert max_capped < max_uncapped, \
        f"capped max notional {max_capped:,.2f} was not below uncapped {max_uncapped:,.2f}"
    # And the cap held the size STILL, which the inequality alone would not show.
    assert len({t["size"] for t in capped["trades"]}) == 1, \
        sorted({t["size"] for t in capped["trades"]})
    assert len({t["size"] for t in uncapped["trades"]}) > 1, \
        "the uncapped run never grew its position size, so there is nothing to contrast"


# =========================================================================== #
# Step 5: the cap reached buying power and margin, not just the risk manager
# =========================================================================== #
@pytest.fixture
def engine_account():
    """A real ``BacktestAccount`` (no engine loop) for the money-surface gates."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition,
    )
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    made = []

    def _build(*, cash, equity_cap):
        _ACCOUNT_SEQ[0] += 1
        account_id = _ACCOUNT_SEQ[0]
        cfg = {"starting_cash": float(cash), "commission_per_trade": 0.0,
               "slippage_bps": 0.0, "fill_model": "next_bar_open",
               "equity_cap": equity_cap}
        wire_backtest_seams()
        ctx = backtest_trading_db(f"equity-cap-gate-{account_id}")
        ctx.__enter__()
        made.append(ctx)
        seed_account_definition(account_id, cfg)
        price = AsOfPriceSource(ohlcv_provider=None)
        price.load_bars("AAPL", [{"Date": _START, "Open": 100.0, "High": 100.0,
                                  "Low": 100.0, "Close": 100.0, "Volume": 1_000}])
        price.set_clock(_START)
        account = BacktestAccount(account_id, price, cfg)
        wire_backtest_seams().register_account(account_id, account)
        return account

    try:
        yield _build
    finally:
        for ctx in reversed(made):
            ctx.__exit__(None, None, None)


def test_the_buying_power_gate_refuses_what_the_real_balance_would_have_allowed(
        engine_account):
    """If the cap stopped at the risk manager, a margin account could deploy 2x it."""
    account = engine_account(cash=40_000.0, equity_cap=20_000.0)
    assert account.get_account_info()["buying_power"] == 20_000.0
    assert account.available_option_buying_power() == 20_000.0
    assert account.check_option_buying_power(30_000.0) is False
    assert account.check_option_buying_power(15_000.0) is True


def test_the_same_gate_allows_it_when_the_cap_is_off(engine_account):
    """The control: the refusal above is the cap's doing, not the fixture's."""
    account = engine_account(cash=40_000.0, equity_cap=None)
    assert account.get_account_info()["buying_power"] == 40_000.0
    assert account.check_option_buying_power(30_000.0) is True


def test_the_assignment_capacity_gate_also_sees_the_capped_balance(engine_account):
    """``check_assignment_capacity`` measures against the BALANCE (a second, independent
    budget from the reserve pool), so it must inherit the cap too or a capped account could
    still take delivery on the full real balance."""
    account = engine_account(cash=40_000.0, equity_cap=20_000.0)
    assert account.check_assignment_capacity(30_000.0) is False
    assert account.check_assignment_capacity(15_000.0) is True
    uncapped = engine_account(cash=40_000.0, equity_cap=None)
    assert uncapped.check_assignment_capacity(30_000.0) is True


# =========================================================================== #
# Step 6: the GA path
# =========================================================================== #
def test_a_ga_run_completes_with_the_cap_and_scores_every_individual_the_same_way(
        monkeypatch):
    """Every per-trial config the optimizer builds carries the SAME cap, and the cap is absent
    from the gene space -- so no individual is scored against a different denominator.

    Follows ``test_options_optimization_ga_e2e``: the REAL
    ``handle_strategy_optimization`` control flow with only the data-heavy leaf
    ``_run_trial_backtest`` stubbed, going THROUGH the real ``_build_daily_trial_config``.
    """
    from app.services.genetic import DEAP_AVAILABLE

    if not DEAP_AVAILABLE:
        pytest.skip("deap not available")

    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services import strategy_optimization_handler as H

    Base.metadata.create_all(bind=engine)

    backtest_block = {
        "engine": "daily",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": "FMPRating", "settings": {}}],
        "start_date": "2024-02-01", "end_date": "2024-03-01",
        "initial_capital": 20_000.0,
        "account_settings": {
            "starting_cash": 20_000.0, "commission_per_trade": 1.0,
            "slippage_bps": 0.0, "fill_model": "next_bar_open",
            "equity_cap": _CAP,
        },
        "warmup_days": 5, "seed": 42, "subtype": "daily_expert",
        "backtest_id": 987_654, "name": "equity-cap-ga",
    }

    db = SessionLocal()
    try:
        strategy = Strategy(name="equity-cap-ga-strategy",
                            entry_rules=[], exit_rules=[])
        db.add(strategy)
        db.commit()
        db.refresh(strategy)
        opt = StrategyOptimization(
            strategy_id=strategy.id, name="equity-cap-ga",
            fitness_metric="sharpe_ratio", optimization_type="genetic",
            optimization_config={
                "populationSize": 4, "generations": 2, "crossoverProb": 0.6,
                "mutationProb": 0.3, "earlyStoppingGenerations": 2,
                "elitismPercent": 0.1, "seed": 42, "parallelIndividuals": 1,
                # TWO genes minimum: deap's cxTwoPoint does randint(1, size - 1) and dies
                # with "empty range in randrange(1, 1)" on a one-gene genome.
                "expert_params": {
                    "min_confidence": {"min": 50, "max": 90, "step": 5,
                                       "type": "int", "optimize": True},
                    "expected_profit_percent": {"min": 5.0, "max": 15.0, "step": 1.0,
                                                "type": "float", "optimize": True},
                },
                "backtest": backtest_block,
            },
            status="pending",
        )
        db.add(opt)
        db.commit()
        db.refresh(opt)
        opt_id = opt.id
    finally:
        db.close()

    seen_caps: List[Any] = []

    def _stub_trial(backtest_cfg, decoded, *a, **kw):
        cfg = H._build_daily_trial_config(backtest_cfg, decoded)
        seen_caps.append(cfg["account_settings"]["equity_cap"])
        return {"total_trades": 3, "sharpe_ratio": 1.0, "max_drawdown": 5.0,
                "total_return": 1.0, "profit_factor": 1.5, "win_rate": 55.0}

    monkeypatch.setattr(H, "_run_trial_backtest", _stub_trial)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {"backtest_cfg": cfg})

    out = H.handle_strategy_optimization("t-equity-cap-ga", {"optimization_id": opt_id})
    assert out["status"] == "completed", out

    assert seen_caps, "the GA built no trials"
    assert all(cap == _CAP for cap in seen_caps), sorted(set(map(str, seen_caps)))

    db = SessionLocal()
    try:
        row = db.query(StrategyOptimization).filter(
            StrategyOptimization.id == opt_id).first()
        assert row.status == "completed"
        assert not any("equity_cap" in k for k in (row.parameter_ranges or {})), \
            [k for k in (row.parameter_ranges or {}) if "equity_cap" in k]
        assert not any("equity_cap" in k for k in (row.best_params or {}))
    finally:
        db.close()
