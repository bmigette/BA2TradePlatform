"""Six deployed-strategy follow-ups plus the four ideas in the companion memo.

Pure builders: importing this module never opens a database or starts an app.
The baseline snapshot contains concrete rules, without the old grid's gene flags.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import functools
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from types import SimpleNamespace

DEPLOYED_FAMILIES = ("large_ds", "mid_insider", "small_earnings", "mid_ds",
                     "mid_earnings", "small_rating")
NEW_FAMILIES = ("quality_momentum", "pullback", "analyst_targets", "etf_trend")
FAMILIES = DEPLOYED_FAMILIES + NEW_FAMILIES
#: Opt-in families: selectable by name, never part of the default campaign (whose manifest
#: fingerprint is pinned), so adding one cannot change the default 35 jobs.
EXTENSION_FAMILIES = ("pullback_rsi",)
ALL_FAMILIES = FAMILIES + EXTENSION_FAMILIES
SNAPSHOT = Path(__file__).with_name("baselines_20260907.json")
SCHEMA_VERSION = 1

#: Grid mode's budget, written unchanged (the default manifest's fingerprint is pinned). The
#: exhaustive grid handler ignores it.
GRID_POPULATION, GRID_GENERATIONS = 24, 4
#: Genetic mode (plan 2026-09-24, Task A4): the budget scales with the job's searched genes.
GA_GENERATIONS, GA_GENERATIONS_LARGE, GA_LARGE_ABOVE_GENES = 25, 30, 20
GA_EARLY_STOP = 8
GA_POPULATION_PER_GENE, GA_POPULATION_MIN, GA_POPULATION_MAX = 4, 24, 120
#: The GA's own gene collector. It imports only ba2_common and the stdlib, so it is loaded by
#: path and the backend ``app`` package is never imported (preflight must not import it).
COLLECTOR = Path(__file__).resolve().parents[3] / "testplatform/backend/app/services/strategy_param_space.py"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def load_baselines():
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if data["schema_version"] != SCHEMA_VERSION or set(data["baselines"]) != set(DEPLOYED_FAMILIES):
        raise ValueError("Unsupported or incomplete baseline snapshot")
    return data["baselines"]


def numeric_range(lo, hi, step, kind="float"):
    return {"optimize": True, "type": kind, "min": lo, "max": hi, "step": step}


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def condition_range(rules, field, lo, hi, step):
    nodes = [n for n in walk(rules) if "field" in n and n["field"] == field]
    if len(nodes) != 1:
        raise ValueError(f"Expected one {field} condition, found {len(nodes)}")
    nodes[0].update(value=lo, optimize=True, value_min=lo, value_max=hi, value_step=step)


def time_exit():
    return {"id": "research_timeout", "name": "Maximum holding period",
            "conditions": {"type": "AND", "conditions": [
                {"id": "research_position", "field": "has_position", "op": "is_true"},
                {"id": "research_days", "field": "days_opened", "op": ">", "value": 60}]},
            "actions": [{"action_type": "close"}], "continue_processing": False}


def signal_close(field):
    """Close a held position when the expert's recommendation flips (``bearish``/``bullish``)."""
    return {"id": f"research_{field}", "conditions": {"type": "AND", "conditions": [
                {"id": f"research_{field}_flag", "field": field, "op": "is_true"}]},
            "actions": [{"action_type": "close"}], "continue_processing": False}


def interface_settings(settings, expert_cls):
    """The ``settings`` a replacement expert inherits: the keys its interface declares
    (permissions, schedules, sizing, RM, screener), without the ones the expert declares itself,
    which the caller sets. The source expert's own decision settings are dropped."""
    declared = expert_cls.get_merged_settings_definitions()
    own = expert_cls.get_settings_definitions()
    return {k: v for k, v in settings.items() if k in declared and k not in own}


@functools.lru_cache(maxsize=None)
def param_space_module():
    """``strategy_param_space`` loaded by file path under a private name (never ``app.*``)."""
    from tools.strategy_research.exploration.market_conditions import _shared_paths
    _shared_paths()
    spec = importlib.util.spec_from_file_location("_research10_strategy_param_space", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def searched_genes(strategy, expert_params):
    """The genes the GA searches for this job, by the GA's own collector.

    ``expert_params`` is split exactly as ``strategy_optimization_handler`` splits it
    (``screener:``/``schedule:`` prefixes go to their own namespaces). One-point ranges
    (min == max, e.g. a fixed control's sizing placeholder) search nothing and are dropped.
    No exploration family uses a bypass expert (FactorRanker), for which the handler would
    drop the rule genes; a test pins that."""
    model = {k: v for k, v in expert_params.items() if not k.startswith(("screener:", "schedule:"))}
    screener = {k[len("screener:"):]: v for k, v in expert_params.items() if k.startswith("screener:")}
    schedule = {k[len("schedule:"):]: v for k, v in expert_params.items() if k.startswith("schedule:")}
    space = param_space_module().collect_param_space(
        SimpleNamespace(**strategy), expert_cfg=model or None,
        screener_cfg=screener or None, schedule_cfg=schedule or None)
    return sorted(name for name, spec in space.items() if spec["min"] != spec["max"])


def ga_budget(genes, population=None, generations=None, early_stop=None):
    """Genetic-mode budget for a job with ``genes`` searched genes; explicit values win.

    Returns ``(budget, source)``: the three optimization_config values, and for each whether it
    was derived ("auto") or passed ("explicit"). Generations: 25, or 30 above 20 genes.
    Population: clamp(4 x genes, 24, 120). Early stop: 8, capped at the generations when only
    ``generations`` was passed below 8; an explicit early stop above the generations is refused."""
    auto_generations = GA_GENERATIONS_LARGE if genes > GA_LARGE_ABOVE_GENES else GA_GENERATIONS
    budget = {
        "populationSize": population if population is not None else min(
            max(GA_POPULATION_PER_GENE * genes, GA_POPULATION_MIN), GA_POPULATION_MAX),
        "generations": generations if generations is not None else auto_generations,
    }
    budget["earlyStoppingGenerations"] = (early_stop if early_stop is not None
                                          else min(GA_EARLY_STOP, budget["generations"]))
    if not 1 <= budget["earlyStoppingGenerations"] <= budget["generations"]:
        raise ValueError(f"early stop {budget['earlyStoppingGenerations']} must be between 1 and "
                         f"the generations ({budget['generations']})")
    passed = {"populationSize": population, "generations": generations,
              "earlyStoppingGenerations": early_stop}
    return budget, {k: "auto" if v is None else "explicit" for k, v in passed.items()}


def budget_text(job):
    """One line for the preview and launch output: a genetic job's genes and resolved budget."""
    oc = job["optimization_config"]
    if "geneCount" not in oc:
        return ""
    if job["fixed"]:
        return "budget: none; a fixed recipe evaluated once (0 searched genes)"
    bt = oc["backtest"]
    parts = [f"{name} {bt[key]['gene_count']}" for key, name in
             (("market_condition", "market entry"), ("market_exit", "market exit")) if key in bt]
    explicit = [k for k, v in oc["budgetSource"].items() if v == "explicit"]
    return (f"budget: genes={oc['geneCount']}" + (f" (incl. {', '.join(parts)})" if parts else "")
            + f" population={oc['populationSize']} generations={oc['generations']}"
            + f" early_stop={oc['earlyStoppingGenerations']}"
            + (f" explicit={','.join(explicit)}" if explicit else ""))


def pullback_reversion_class():
    """Imported only when the opt-in family is selected: the default campaign stays stdlib-only."""
    from tools.strategy_research.exploration.runtime import add_source_paths
    add_source_paths()
    from ba2_experts.PullbackReversion import PullbackReversion
    return PullbackReversion


def new_idea_baseline(family, reference):
    baseline = deepcopy(reference)
    baseline["source"] = {"memo": "reports/expert_strategy_ideas_2026-09-07.md",
                          "hypothesis": family, "defaults_from": reference["source"]}
    bt = baseline["backtest"]
    settings = bt["experts"][0]["settings"]
    settings.update(sizing_mode="notional", risk_per_trade_pct=10.0,
                    atr_risk_budget_pct=1.0, max_virtual_equity_per_instrument_percent=20.0,
                    min_stop_loss_pct=7.0, min_available_balance_pct=10.0,
                    macro_mode="off", w_analyst=0.0, w_earnings=0.0,
                    w_technical=0.7, w_fundamental=0.3,
                    tw_mom=0.7, tw_d200=0.3, tw_rsi=0.0, tw_don=0.0,
                    fw_quality=0.6, fw_piotroski=0.4, fw_value=0.0, fw_growth=0.0)
    bt["account_settings"]["spread_bps"] = 5.0
    bt["stress_spread_bps"] = 5.0  # additional spread for the robustness calculation
    bt["warmup_days"] = 600
    screen = bt["screener_opt"]["base_settings"]
    screen.update(market_cap_min=10000000000, market_cap_max=0, price_min=10.0,
                  volume_min=500000, dollar_volume_min=10000000.0, float_min=10000000,
                  relative_volume_min=0.0, price_drop_pct=0.0, price_drop_days=5,
                  weinstein_stage2_only=False, max_stocks=50)
    settings.update({"screener_" + k: v for k, v in screen.items()})
    baseline["strategy"] = {
        "entry_rules": [{"id": "research_buy", "conditions": {"type": "AND", "conditions": [
            {"id": "research_bull", "field": "bullish", "op": "is_true"},
            {"id": "research_flat", "field": "has_no_position", "op": "is_true"},
            {"id": "research_cooldown", "field": "days_since_last_close", "op": ">", "value": 1}]},
            "actions": [{"action_type": "buy"},
                        {"action_type": "adjust_stop_loss", "reference_value": "order_open_price", "action_value": -8.0},
                        {"action_type": "adjust_take_profit", "reference_value": "order_open_price", "action_value": 20.0}],
            "continue_processing": False}],
        "exit_rules": [time_exit()]}
    if family in ("pullback", "etf_trend", "pullback_rsi"):
        bt["run_schedule_override"] = deepcopy(bt["manage_schedule_override"])
        bt["screener_opt"]["cadence_days"] = 1
    if family == "analyst_targets":
        baseline["earliest_start"] = "2022-01-01"
    if family == "etf_trend":
        # The ETF hypothesis uses a fixed universe, never the stock screener.
        common = load_baselines()["mid_insider"]["backtest"]["experts"][0]["settings"]
        expert_keys = ("lookback_days", "min_insiders", "min_total_value", "expected_profit_percent",
                       "expected_profit_mode", "model_target_method", "max_expected_profit_percent")
        settings = {k: v for k, v in common.items() if k not in expert_keys and not k.startswith("screener_")}
        settings.update(instrument_selection_method="static", sizing_mode="notional",
                        risk_per_trade_pct=45.0, max_virtual_equity_per_instrument_percent=45.0,
                        min_available_balance_pct=10.0, min_stop_loss_pct=7.0,
                        universe_symbols=["SPY", "IEF", "TLT", "GLD"],
                        momentum_bars=252, trend_bars=200, top_n=2)
        bt["experts"] = [{"class": "ETFTrend", "settings": settings}]
        bt["enabled_instruments"] = list(settings["universe_symbols"])
        del bt["screener_opt"]
        baseline["strategy"]["entry_rules"][0]["actions"] = [
            {"action_type": "buy"},
            {"action_type": "adjust_stop_loss", "reference_value": "order_open_price", "action_value": -10.0}]
        baseline["strategy"]["exit_rules"] = [{
            "id": "research_unselected", "conditions": {"type": "AND", "conditions": [
                {"id": "research_unselected_flag", "field": "bearish", "op": "is_true"}]},
            "actions": [{"action_type": "close"}], "continue_processing": False}]
    if family == "pullback_rsi":
        # The literal RSI pullback expert on the same point-in-time large-cap screen. Long by
        # default; variants() flips a short job's direction, rules and short permission.
        settings = interface_settings(settings, pullback_reversion_class())
        settings.update(direction="long", trend_gate="sma200", rsi_period=2,
                        entry_threshold=5.0, exit_mode="sma5", rsi_exit=70.0)
        bt["experts"] = [{"class": "PullbackReversion", "settings": settings}]
        # A long's SELL recommendation is its EXIT signal, so the entry never sells, and the
        # expert's exit (or the time limit) replaces a profit target.
        baseline["strategy"]["entry_rules"][0]["actions"] = [
            {"action_type": "buy"},
            {"action_type": "adjust_stop_loss", "reference_value": "order_open_price", "action_value": -8.0}]
        baseline["strategy"]["exit_rules"] = [signal_close("bearish"), time_exit()]
    return baseline


def pullback_rsi_short(job):
    """Mirror a long pullback_rsi job: SELL on the expert's bearish (entry) recommendation with
    a stop 8% above the entry, close on its bullish (exit) one, and enable shorts in the engine."""
    entry = job["strategy"]["entry_rules"][0]
    entry["id"] = "research_short"
    flags = [n for n in walk(entry["conditions"]) if n.get("field") == "bullish"]
    if len(flags) != 1:
        raise ValueError(f"pullback_rsi entry must have one bullish condition, found {len(flags)}")
    flags[0].update(id="research_bear", field="bearish")
    # The stop keeps action_value -8.0: TradeActions reads the percent in the POSITION's
    # direction (a short's level is reference * (1 - pct/100)), so -8 puts it 8% ABOVE the
    # short entry. +8.0 would place it 8% below, i.e. on the profit side.
    entry["actions"] = [
        {"action_type": "sell"},
        {"action_type": "adjust_stop_loss", "reference_value": "order_open_price", "action_value": -8.0}]
    exits = job["strategy"]["exit_rules"]
    if exits[0] != signal_close("bearish"):
        raise ValueError("pullback_rsi exit rules must start with the reverse-signal close")
    exits[0] = signal_close("bullish")
    bt = job["backtest"]
    # enable_short reaches the trial config (_build_daily_trial_config) and forces the RM's
    # enable_sell gate (deploy_parity: enable_sell follows enable_short); the expert setting
    # states the same permission for a deployed instance. With it, the `sell` entry opens a
    # short from a flat book (TradeActions.SellAction).
    bt["enable_short"] = True
    bt["experts"][0]["settings"].update(direction="short", enable_sell=True)


def variants(family, baseline):
    """Separate experiments share a control; independent hypotheses are not crossed."""
    def fresh(label, hypothesis):
        return {"family": family, "variant": label, "hypothesis": hypothesis,
                "strategy": deepcopy(baseline["strategy"]),
                "backtest": deepcopy(baseline["backtest"]), "expert_params": {}}

    out = ([fresh("control", "Frozen source recipe under the declared campaign capital and costs.")]
           if family in DEPLOYED_FAMILIES else [])
    if family == "large_ds":
        for technical, fundamental in ((70, 30), (50, 50), (30, 70)):
            job = fresh(f"quality_momentum_{technical}_{fundamental}",
                        "Technical/quality blend; retain the control's screen, entry thresholds and exits.")
            job["backtest"]["experts"][0]["settings"].update(
                w_technical=technical / 100, w_fundamental=fundamental / 100,
                w_analyst=0.0, w_earnings=0.0, tw_mom=0.7, tw_d200=0.3,
                tw_rsi=0.0, tw_don=0.0, fw_quality=0.6, fw_piotroski=0.4,
                fw_value=0.0, fw_growth=0.0, mom_skip_days=21)
            job["expert_params"]["mom_lookback_days"] = numeric_range(126, 252, 126, "int")
            out.append(job)
    elif family == "mid_insider":
        job = fresh("timeout", "60/90/120-day timeout; retain the 120-day signal lookback.")
        condition_range(job["strategy"]["exit_rules"], "days_opened", 60, 120, 30)
        out.append(job)
        job = fresh("signal_freshness", "30/60/90/120-day insider lookback; retain the 210-day timeout.")
        job["expert_params"]["lookback_days"] = numeric_range(30, 120, 30, "int")
        out.append(job)
    elif family in ("small_earnings", "small_rating"):
        job = fresh("timeout", "60/90/120-day timeout against the original unlimited-hold control.")
        # A matching floor-stop rule stops processing. Put the close BEFORE it.
        job["strategy"]["exit_rules"].insert(0, time_exit())
        condition_range(job["strategy"]["exit_rules"], "days_opened", 60, 120, 30)
        out.append(job)
        if family == "small_rating":
            for label, tp, sl in (("common_near", -14.0, -4.0), ("common_wide", 8.0, -18.0)):
                job = fresh(label, "Apply one of the existing brackets to both confidence tiers; retain the floor stop.")
                for rule in job["strategy"]["entry_rules"]:
                    for action in rule["actions"]:
                        if action["action_type"] == "adjust_take_profit":
                            action["action_value"] = tp
                        elif action["action_type"] == "adjust_stop_loss":
                            action["action_value"] = sl
                out.append(job)
    elif family == "mid_ds":
        job = fresh("timeout", "15/20/25/30-day timeout with all score settings held fixed.")
        condition_range(job["strategy"]["exit_rules"], "days_opened", 15, 30, 5)
        out.append(job)
        job = fresh("signal_reversal", "Add a bearish close, retaining the control's 25-day timeout.")
        job["strategy"]["exit_rules"].insert(0, signal_close("bearish"))
        out.append(job)
    elif family == "mid_earnings":
        job = fresh("target_offset", "First-tier target offsets -14/-12/-10/-8%; signal, stop and timeout fixed.")
        actions = [a for a in job["strategy"]["entry_rules"][0]["actions"]
                   if a["action_type"] == "adjust_take_profit"]
        if len(actions) != 1:
            raise ValueError("Mid EarningsDrift must have one first-tier TP action")
        actions[0].update(action_value=-14.0, action_value_optimize=True,
                          action_value_min=-14.0, action_value_max=-8.0, action_value_step=2.0)
        out.append(job)
    elif family == "quality_momentum":
        for technical, fundamental in ((70, 30), (50, 50), (30, 70)):
            job = fresh(f"blend_{technical}_{fundamental}",
                        "Standalone quality/momentum search in a fixed liquid large-cap universe.")
            job["backtest"]["experts"][0]["settings"].update(
                w_technical=technical / 100, w_fundamental=fundamental / 100)
            job["expert_params"] = {"mom_lookback_days": numeric_range(126, 252, 126, "int"),
                                    "theta_buy": numeric_range(0.2, 0.4, 0.1)}
            condition_range(job["strategy"]["exit_rules"], "days_opened", 30, 90, 30)
            job["strategy"]["entry_rules"][0]["actions"][1].update(
                action_value_optimize=True, action_value_min=-12.0, action_value_max=-8.0, action_value_step=4.0)
            out.append(job)
    elif family == "pullback":
        for rsi in (2, 3, 5):
            for hold in (3, 5, 10):
                job = fresh(f"rsi{rsi}_hold{hold}", "Daily blended RSI pullback; not a hard raw-RSI/SMA gate.")
                job["backtest"]["experts"][0]["settings"].update(
                    w_technical=0.8, w_fundamental=0.2, tw_mom=0.0, tw_d200=0.2,
                    tw_rsi=0.8, tw_don=0.0, rsi_period=rsi)
                job["strategy"]["exit_rules"][0]["conditions"]["conditions"][1]["value"] = hold
                job["expert_params"]["theta_buy"] = numeric_range(0.15, 0.35, 0.1)
                out.append(job)
    elif family == "analyst_targets":
        for hold in (15, 30, 60):
            job = fresh(f"hold{hold}", "Dated target-change/upside blend with technical confirmation, starting in 2022.")
            job["backtest"]["experts"][0]["settings"].update(
                w_technical=0.2, w_fundamental=0.0, w_analyst=0.8, w_earnings=0.0,
                aw_targets=1.0, aw_grades=0.0)
            job["strategy"]["exit_rules"][0]["conditions"]["conditions"][1]["value"] = hold
            job["expert_params"] = {"analyst_target_window_days": numeric_range(30, 90, 30, "int"),
                                    "analyst_min_targets": numeric_range(3, 5, 2, "int")}
            out.append(job)
    elif family == "etf_trend":
        for top_n in range(1, min(2, len(baseline["backtest"]["enabled_instruments"])) + 1):
            job = fresh(f"top{top_n}", "Prior-month momentum/trend selection; unallocated slots remain cash.")
            job["backtest"]["experts"][0]["settings"].update(
                top_n=top_n, risk_per_trade_pct=90.0 / top_n,
                max_virtual_equity_per_instrument_percent=90.0 / top_n)
            job["expert_params"]["momentum_bars"] = numeric_range(126, 252, 126, "int")
            out.append(job)
    elif family == "pullback_rsi":
        for label, direction, gate, exit_mode, hypothesis in (
                ("long_sma5", "long", "sma200", "sma5",
                 "Buy an RSI dip above SMA200; exit on a close above SMA5."),
                ("long_choch", "long", "sma200", "sma5_or_choch",
                 "As long_sma5, also exiting on a bearish swing-structure CHoCH."),
                ("long_rsi", "long", "sma200", "rsi",
                 "Buy an RSI dip above SMA200; exit when RSI recovers above 60/70."),
                ("short_sma5", "short", "sma200", "sma5",
                 "Short an RSI rally below SMA200; cover on a close below SMA5."),
                ("short_spy", "short", "sma200_and_spy", "sma5",
                 "As short_sma5, only while SPY is also below its SMA200.")):
            job = fresh(label, hypothesis)
            job["backtest"]["experts"][0]["settings"].update(
                direction=direction, trend_gate=gate, exit_mode=exit_mode,
                rsi_exit=60.0 if exit_mode == "rsi" else 70.0)
            if direction == "short":
                pullback_rsi_short(job)
            job["expert_params"] = {"rsi_period": numeric_range(2, 3, 1, "int"),
                                    "entry_threshold": numeric_range(5.0, 15.0, 5.0)}
            if exit_mode == "rsi":
                job["expert_params"]["rsi_exit"] = numeric_range(60.0, 70.0, 10.0)
            condition_range(job["strategy"]["exit_rules"], "days_opened", 5, 10, 5)
            out.append(job)
    else:
        raise ValueError(f"Unknown strategy family: {family}")
    return out


def build_manifest(*, families=FAMILIES, equity=10000.0, equity_cap=10000.0,
                   start="2020-01-01", end="2025-12-31", search="grid",
                   population=None, generations=None, early_stop=None, parallel=1, seed=42,
                   workers=(), save_top=5, store=None, spread_bps=None, etf_symbols=None,
                   market_condition_profile="none", market_condition_manifest=None,
                   market_condition_mode="search", market_exit=(), allow_sl_loosen=False):
    """Build a portable manifest. No data access; --preflight resolves the actual universe.

    ``market_exit`` (kinds from exit/stop/tp) appends the off-by-default market exit templates to
    every job's exit rules; ``allow_sl_loosen`` sets the expert setting
    ``allow_ruleset_sl_loosen``. Both enter the job identity only when set, so the default
    manifests are byte-identical.

    Search budget: grid mode writes population 24 / generations 4 (or the explicit values) with
    ``earlyStoppingGenerations = generations``, exactly as before. Genetic mode sizes each job
    from its searched genes in the FINAL manifest (:func:`searched_genes`, :func:`ga_budget`)
    and also records ``geneCount`` and ``budgetSource``; explicit values always win."""
    if not families or len(set(families)) != len(families) or not set(families) <= set(ALL_FAMILIES):
        raise ValueError("Select distinct, known strategy families")
    if equity <= 0 or (equity_cap is not None and equity_cap <= 0):
        raise ValueError("Equity and the optional equity cap must be positive")
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Start must be on or before end")
    if (search not in ("grid", "genetic") or (population is not None and population < 2)
            or (generations is not None and generations < 1) or parallel < 0 or save_top < 1):
        raise ValueError("Invalid search budget")
    if early_stop is not None:
        if search != "genetic":
            raise ValueError("--early-stop applies to --search genetic only (an exhaustive grid has no generations)")
        if early_stop < 1 or (generations is not None and early_stop > generations):
            raise ValueError(f"Early stop must be between 1 and the generations; got {early_stop}")
    if search == "grid" and workers:
        raise ValueError("The existing exhaustive-grid handler is local/serial; use --search genetic for remote workers")
    from tools.strategy_research.exploration.market_conditions import (
        selection, attach, attach_exits, exit_selection, refuse_inert_market_exit)
    profiles, pins = selection(market_condition_profile, market_condition_manifest, market_condition_mode)
    if profiles and market_condition_mode == "search" and search != "genetic":
        raise ValueError("Market-condition gene search requires --search genetic; exhaustive grids are too large")
    market_exit = exit_selection(tuple(market_exit), profiles, market_condition_mode, search)
    if parallel == 0 and not workers:
        raise ValueError("--parallel 0 requires named remote workers")
    if spread_bps is not None and spread_bps < 0:
        raise ValueError("Spread cannot be negative")
    if etf_symbols is not None:
        if (not etf_symbols or len(set(etf_symbols)) != len(etf_symbols)
                or any(not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", s) for s in etf_symbols)):
            raise ValueError("ETF symbols must be distinct uppercase ticker symbols")
    baselines = load_baselines()
    jobs = []
    for family in families:
        baseline = baselines[family] if family in DEPLOYED_FAMILIES else new_idea_baseline(family, baselines["large_ds"])
        if family == "etf_trend" and etf_symbols is not None:
            baseline["backtest"]["enabled_instruments"] = list(etf_symbols)
            baseline["backtest"]["experts"][0]["settings"]["universe_symbols"] = list(etf_symbols)
        for job in variants(family, baseline):
            bt = job.pop("backtest")
            effective_start = max(start, baseline["earliest_start"])
            if effective_start > end:
                raise ValueError(f"{family}: no supported dates before {end}")
            bt.update(start_date=effective_start, end_date=end, initial_capital=equity, seed=seed)
            bt["account_settings"].update(starting_cash=equity, equity_cap=equity_cap)
            if spread_bps is not None:
                bt["account_settings"]["spread_bps"] = spread_bps
            if store is not None and "screener_opt" in bt:
                bt["screener_opt"]["store"] = str(Path(store).expanduser().resolve())
            # Settings and actual overrides carry the same schedule contract.
            settings = bt["experts"][0]["settings"]
            settings["execution_schedule_enter_market"] = deepcopy(bt["run_schedule_override"])
            settings["execution_schedule_open_positions"] = deepcopy(bt["manage_schedule_override"])
            attach(job, bt, profiles, pins, market_condition_mode)
            attach_exits(job, bt, profiles, market_exit)
            if allow_sl_loosen:
                for expert in bt["experts"]:
                    expert["settings"]["allow_ruleset_sl_loosen"] = True
            expert_params = job.pop("expert_params")
            has_rule_gene = any(n.get("optimize") or n.get("action_value_optimize") or n.get("mode_optimize")
                                or n.get("toggle_optimize") for n in walk(job["strategy"]))
            fixed = not expert_params and not has_rule_gene
            if fixed:
                # The shared optimizer requires a nonempty parameter space. A one-point
                # range preserves the actual sizing value and evaluates the control once.
                value = settings["risk_per_trade_pct"]
                expert_params["risk_per_trade_pct"] = numeric_range(value, value, 1)
            if search == "genetic":
                genes = searched_genes(job["strategy"], expert_params)
                # B5/B6 record the market genes they add; they must be genes the GA searches.
                recorded = {g for key in ("market_condition", "market_exit") if key in bt
                            for g in bt[key]["genes"]}
                if not recorded <= set(genes):
                    raise ValueError(f"{family}/{job['variant']}: recorded market genes the GA would not "
                                     f"search: {sorted(recorded - set(genes))}")
                try:
                    budget, source = ga_budget(len(genes), population, generations, early_stop)
                except ValueError as exc:
                    raise ValueError(f"{family}/{job['variant']} ({len(genes)} genes): {exc}") from None
            else:
                budget = {"populationSize": GRID_POPULATION if population is None else population,
                          "generations": GRID_GENERATIONS if generations is None else generations}
                budget["earlyStoppingGenerations"] = budget["generations"]
            job.update(source=deepcopy(baseline["source"]), fixed=fixed,
                       expert=bt["experts"][0]["class"], fitness_metric="consistent_annual_return",
                       optimization_type="brute_force" if fixed or search == "grid" else "genetic",
                       worker_names=list(workers), save_top=1 if fixed else save_top,
                       optimization_config={
                           "populationSize": budget["populationSize"], "generations": budget["generations"],
                           "crossoverProb": 0.6, "mutationProb": 0.3,
                           "earlyStoppingGenerations": budget["earlyStoppingGenerations"], "elitismPercent": 10,
                           "parallelIndividuals": parallel, "seed": seed,
                           "expert_params": expert_params, "backtest": bt})
            if search == "genetic":  # only here: grid manifests stay byte-identical
                job["optimization_config"].update(geneCount=len(genes), budgetSource=source)
            digest = fingerprint(job)[:12]
            options = ""  # only when set: the default names are unchanged
            if market_exit:
                options += "-mx_" + "_".join(market_exit)
            if allow_sl_loosen:
                options += "-slloosen"
            job["name"] = f"research10-{family}-{job['variant']}{options}-eq{equity:g}-{digest}"
            bt["name"] = job["name"]
            bt["labels"] = ["research10", "goal2020-followup", family, job["variant"], f"equity-{equity:g}"]
            if market_exit:
                bt["labels"].append("market-exit")
            if allow_sl_loosen:
                bt["labels"].append("sl-loosen")
            job["fingerprint"] = fingerprint(job)
            jobs.append(job)
    refuse_inert_market_exit(jobs)
    return {"schema_version": SCHEMA_VERSION, "jobs": jobs,
            "scope": "Six deployed-strategy follow-ups and four new ideas; each job is a standalone account, not a joint portfolio.",
            "validation": "2020-2025 is previously searched history, not an untouched holdout."}
