"""Six deployed-strategy follow-ups plus the four ideas in the companion memo.

Pure builders: importing this module never opens a database or starts an app.
The baseline snapshot contains concrete rules, without the old grid's gene flags.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import re

DEPLOYED_FAMILIES = ("large_ds", "mid_insider", "small_earnings", "mid_ds",
                     "mid_earnings", "small_rating")
NEW_FAMILIES = ("quality_momentum", "pullback", "analyst_targets", "etf_trend")
FAMILIES = DEPLOYED_FAMILIES + NEW_FAMILIES
SNAPSHOT = Path(__file__).with_name("baselines_20260907.json")
SCHEMA_VERSION = 1


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
    if family in ("pullback", "etf_trend"):
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
    return baseline


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
        job["strategy"]["exit_rules"].insert(0, {
            "id": "research_bearish", "conditions": {"type": "AND", "conditions": [
                {"id": "research_bearish_flag", "field": "bearish", "op": "is_true"}]},
            "actions": [{"action_type": "close"}], "continue_processing": False})
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
    else:
        raise ValueError(f"Unknown strategy family: {family}")
    return out


def build_manifest(*, families=FAMILIES, equity=10000.0, equity_cap=10000.0,
                   start="2020-01-01", end="2025-12-31", search="grid",
                   population=24, generations=4, parallel=1, seed=42,
                   workers=(), save_top=5, store=None, spread_bps=None, etf_symbols=None):
    """Build a portable manifest. No data access; --preflight resolves the actual universe."""
    if not families or len(set(families)) != len(families) or not set(families) <= set(FAMILIES):
        raise ValueError("Select distinct, known strategy families")
    if equity <= 0 or (equity_cap is not None and equity_cap <= 0):
        raise ValueError("Equity and the optional equity cap must be positive")
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("Start must be on or before end")
    if search not in ("grid", "genetic") or population < 2 or generations < 1 or parallel < 0 or save_top < 1:
        raise ValueError("Invalid search budget")
    if search == "grid" and workers:
        raise ValueError("The existing exhaustive-grid handler is local/serial; use --search genetic for remote workers")
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
            expert_params = job.pop("expert_params")
            has_rule_gene = any(n.get("optimize") or n.get("action_value_optimize")
                                for n in walk(job["strategy"]))
            fixed = not expert_params and not has_rule_gene
            if fixed:
                # The shared optimizer requires a nonempty parameter space. A one-point
                # range preserves the actual sizing value and evaluates the control once.
                value = settings["risk_per_trade_pct"]
                expert_params["risk_per_trade_pct"] = numeric_range(value, value, 1)
            job.update(source=deepcopy(baseline["source"]), fixed=fixed,
                       expert=bt["experts"][0]["class"], fitness_metric="consistent_annual_return",
                       optimization_type="brute_force" if fixed or search == "grid" else "genetic",
                       worker_names=list(workers), save_top=1 if fixed else save_top,
                       optimization_config={
                           "populationSize": population, "generations": generations,
                           "crossoverProb": 0.6, "mutationProb": 0.3,
                           "earlyStoppingGenerations": generations, "elitismPercent": 10,
                           "parallelIndividuals": parallel, "seed": seed,
                           "expert_params": expert_params, "backtest": bt})
            digest = fingerprint(job)[:12]
            job["name"] = f"research10-{family}-{job['variant']}-eq{equity:g}-{digest}"
            bt["name"] = job["name"]
            bt["labels"] = ["research10", "goal2020-followup", family, job["variant"], f"equity-{equity:g}"]
            job["fingerprint"] = fingerprint(job)
            jobs.append(job)
    return {"schema_version": SCHEMA_VERSION, "jobs": jobs,
            "scope": "Six deployed-strategy follow-ups and four new ideas; each job is a standalone account, not a joint portfolio.",
            "validation": "2020-2025 is previously searched history, not an untouched holdout."}
