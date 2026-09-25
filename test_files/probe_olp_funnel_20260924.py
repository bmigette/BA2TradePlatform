"""Ad-hoc probe (2026-09-24): why does the stage-1 O_LP (long put) x DeterministicScorer
option-grid job barely trade, did long puts pay in 2020 / 2022, and why do some genomes report
max_drawdown -100 % on a POSITIVE total return?

MEASUREMENT ONLY. No production code is edited; every hook is a runtime monkeypatch in THIS
process. No DB other than a throwaway sqlite under OUT_DIR is opened for writing.

WHAT IT DOES (one genome per invocation):
  1. Rebuilds the O_LP Strategy exactly like the grid launcher does
     (ba2test_launcher._build_strategy with the market-condition profiles of the run), checks
     the gene space equals the genome's key set, decodes the genome with the REAL decode_params
     and assembles the trial with the REAL _build_daily_trial_config (option_trade_records=True
     -- output shape only, pinned not to change a decision), then runs the REAL
     run_daily_backtest. Local deviations from the grid (documented in the report):
       * screener_opt.store remapped /home/debian/ba2-grid/home/... -> local BA2_HOME,
       * market_condition_manifests remapped to the LOCAL published snapshots (the grid's
         ca4d65d4.../2b6e8ca8... digests are not on this box).
  2. Instruments the ENTRY FUNNEL per (entry day, symbol): expert signal -> each entry leaf
     in AND order (the evaluator stops at the first failure, so "first failing leaf" IS the
     successive funnel) -> dup/equity gate -> option action outcome (refusal message, chain
     diagnostics for "no liquid contract") -> order final status (filled / expired DAY limit).
  3. Records the intraday-drawdown refinement per trade (results._build_refine_drawdown_fn ->
     intraday_drawdown.refine_max_drawdown) next to the daily-curve drawdown.
  4. Writes trades / equity curve / funnel / summary under OUT_DIR/<label>/.

Usage:
  .venv/Scripts/python.exe test_files/probe_olp_funnel_20260924.py --pick top_fit_0
  .venv/Scripts/python.exe test_files/probe_olp_funnel_20260924.py --pick control
  .venv/Scripts/python.exe test_files/probe_olp_funnel_20260924.py --pick control --label control_mon --schedule monday
"""
from __future__ import annotations

import os
import sys

OUT_DIR = (r"C:\Users\basti\AppData\Local\Temp\claude\C--Users-basti-Documents-dev-"
           r"BA2TradePlatform\820f80e0-b6ea-41a0-8f76-d0176d9f7156\scratchpad\olp_diag")
GENOMES = (r"C:\Users\basti\AppData\Local\Temp\claude\C--Users-basti-Documents-dev-"
           r"BA2TradePlatform\820f80e0-b6ea-41a0-8f76-d0176d9f7156\scratchpad\ds_sell\olp_genomes.json")
os.makedirs(OUT_DIR, exist_ok=True)
# Throwaway app DB (never the 9.8 GB test DB, never a live DB). Set BEFORE any app import.
_SCRATCH_DB = os.path.join(OUT_DIR, "probe_app_%d.sqlite" % os.getpid())
os.environ["DATABASE_URL"] = "sqlite:///" + _SCRATCH_DB.replace("\\", "/")
os.environ["DB_FILE"] = _SCRATCH_DB

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(REPO, "packages", "common"), os.path.join(REPO, "packages", "providers"),
          os.path.join(REPO, "packages", "experts"), os.path.join(REPO, "testplatform"),
          os.path.join(REPO, "testplatform", "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

import logging  # noqa: E402

logging.disable(logging.INFO)  # standalone backtest scripts run 10x slower without this

import argparse  # noqa: E402
import collections  # noqa: E402
import copy  # noqa: E402
import csv  # noqa: E402
import gzip  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from datetime import date, datetime  # noqa: E402

LOCAL_SCREENER_STORE = os.path.join(os.path.expanduser("~"), "Documents", "ba2", "common",
                                    "cache", "screener", "metric_store")
LOCAL_MANIFESTS = {
    "ohlcv-v1": "c9ba981fbae8726ec749eca4201c98399a4285046733d16cca6b112b8c8371df",
    "ta-structure-v1": "3c3020d05f9e24ded59272050e1f06193abc74e6c00e41e3168a1750bcc44385",
}


def _block_network():
    import requests.sessions

    def _blocked(self, method, url, *a, **k):  # noqa: ANN001
        raise RuntimeError(f"PROBE NETWORK BLOCK: {method} {url}")

    requests.sessions.Session.request = _blocked


def _seed_scratch_db():
    """The run carries FMP_API_KEY from the (scratch) app DB into its RAM trading DB; the
    provider constructors refuse a missing key even when every read is cache-only."""
    from ba2_common.core import db as cdb
    cdb.configure_db(_SCRATCH_DB)
    cdb.init_db()
    from ba2_common.core.models import AppSetting
    from sqlmodel import Session, select
    with Session(cdb.get_db().bind) as s:
        for k in ("FMP_API_KEY", "FINNHUB_API_KEY", "ALPHA_VANTAGE_API_KEY"):
            if s.exec(select(AppSetting).where(AppSetting.key == k)).first() is None:
                s.add(AppSetting(key=k, value_str="PROBE-OFFLINE-KEY"))
        s.commit()


# ------------------------------------------------------------------------------------ genomes
def _control_params(schedule_days):
    """All optional entry gates OFF, macro_mode off, DS default weights, theta_sell 0.2,
    confidence gate ON at 40, AUTHORED (template) exits and entry action. Genes not listed
    keep the strategy template's authored value / the expert's own default setting."""
    from ba2_experts.DeterministicScorer import DeterministicScorer
    defs = DeterministicScorer.get_settings_definitions()
    p = {}
    for k in ("w_technical", "w_fundamental", "w_analyst", "w_earnings", "theta_buy",
              "k_stop", "k_target"):
        if k in defs and "default" in defs[k]:
            p[f"model:{k}"] = defs[k]["default"]
    p["model:macro_mode"] = "off"
    p["model:theta_sell"] = 0.2
    p["cond:o_lp-signal:mode"] = "below"
    p["cond:shared-gate_confidence:enabled"] = 1
    p["cond:shared-gate_confidence:value"] = 40.0
    for g in ("o_lp-iv_rank", "shared-rel_volume", "o_lp-iv_rv", "o_lp-exp_profit"):
        p[f"cond:{g}:enabled"] = 0
    for m in ("slope", "adx", "rv", "dist-support", "dist-resistance", "chan-pos",
              "vs-prior-high", "structure"):
        p[f"cond:o_lp-market-{m}:mode"] = "off"
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        p[f"schedule:{d}"] = 1 if d in schedule_days else 0
    return p


# ------------------------------------------------------------------------------------ hooks
class Recorder:
    def __init__(self):
        self.rows = []            # one per (entry day, symbol) decision point
        self.cur = None
        self.bars = []            # (date, n_universe_offered)
        self.screen = []          # (date, n_allowed or None)
        self.order_meta = {}      # order_id -> (date, symbol)
        self.refine = []          # per-trade refinement diagnostics
        self.refine_summary = {}
        self.as_of = None


REC = Recorder()


def _install_hooks(leaf_labels):
    import app.services.backtest.daily_engine as de
    from ba2_common.core import TradeActionEvaluator as tae_mod
    from ba2_common.core import TradeActions as ta
    from ba2_common.core import option_selector as osel
    import app.services.backtest.intraday_drawdown as idd

    # --- decision points -----------------------------------------------------------------
    orig_bar = de.DailyBacktestEngine._run_expert_bar

    def _run_expert_bar(self, expert, expert_id, settings, ruleset_id, universe, as_of):
        REC.bars.append((as_of.date().isoformat(), len(universe)))
        REC.as_of = as_of
        return orig_bar(self, expert, expert_id, settings, ruleset_id, universe, as_of)

    de.DailyBacktestEngine._run_expert_bar = _run_expert_bar

    orig_screen = de._screened_symbols_for_bar

    def _screened(runtime, as_of_dt, cache):
        out = orig_screen(runtime, as_of_dt, cache)
        REC.screen.append((as_of_dt.date().isoformat(), None if out is None else len(out)))
        return out

    de._screened_symbols_for_bar = _screened

    orig_stage = de.DailyBacktestEngine._stage_recommendation_candidate

    def _stage(self, rec, *, expert, expert_id, symbol, ruleset_id, as_of, equity_candidates):
        sig = getattr(getattr(rec, "signal", None), "value", getattr(rec, "signal", None))
        row = {"d": as_of.date().isoformat(), "sym": symbol,
               "sig": ("SKIP" if getattr(rec, "skip", False) else str(sig)),
               "conf": round(float(getattr(rec, "confidence", 0) or 0), 2),
               "epp": (None if getattr(rec, "expected_profit_percent", None) is None
                       else round(float(rec.expected_profit_percent), 2)),
               "stage": None, "fail_idx": None, "fail_leaf": None, "fail_val": None,
               "fail_status": None, "msg": None, "order_id": None}
        REC.cur = row
        try:
            return orig_stage(self, rec, expert=expert, expert_id=expert_id, symbol=symbol,
                              ruleset_id=ruleset_id, as_of=as_of,
                              equity_candidates=equity_candidates)
        finally:
            if row["stage"] is None:
                row["stage"] = "no_rec"          # SKIP/HOLD/ERROR never staged
            elif row["stage"] == "rule_pass":
                row["stage"] = "dup_or_equity_gate"
            REC.rows.append(row)
            REC.cur = None

    de.DailyBacktestEngine._stage_recommendation_candidate = _stage

    # --- entry leaves ---------------------------------------------------------------------
    TAE = tae_mod.TradeActionEvaluator
    orig_eval = TAE.evaluate

    def _evaluate(self, instrument_name, expert_recommendation, ruleset_id, existing_order=None):
        out = orig_eval(self, instrument_name, expert_recommendation, ruleset_id, existing_order)
        row = REC.cur
        if row is not None and row["stage"] is None:
            evs = list(self.condition_evaluations or [])
            failed = [i for i, c in enumerate(evs) if not c.get("condition_result")]
            if failed:
                i = failed[0]
                c = evs[i]
                row["stage"] = "cond_fail"
                row["fail_idx"] = i
                row["fail_leaf"] = leaf_labels[i] if i < len(leaf_labels) else c.get("event_type")
                row["fail_val"] = c.get("calculated_value")
                row["fail_status"] = c.get("market_condition_status")
            elif not out or any("error" in s for s in out):
                row["stage"] = "eval_error"
                row["msg"] = str(out)[:200]
            else:
                row["stage"] = "rule_pass"
        return out

    TAE.evaluate = _evaluate

    orig_exec = TAE.execute

    def _execute(self, submit_to_broker=True):
        res = orig_exec(self, submit_to_broker)
        row = REC.cur
        if row is not None and row["stage"] == "rule_pass":
            ok = [r for r in res if r.get("success") and (r.get("data") or {}).get("order_id")]
            if ok:
                row["stage"] = "submitted"
                oid = ok[0]["data"]["order_id"]
                row["order_id"] = oid
                REC.order_meta[oid] = (row["d"], row["sym"])
            else:
                row["stage"] = "action_refused"
                if not row.get("msg"):
                    row["msg"] = "; ".join(str(r.get("message"))[:160] for r in res)
        return res

    TAE.execute = _execute

    # --- option action internals ------------------------------------------------------------
    orig_chain = ta._OptionEntryAction._chain

    def _chain(self, option_type):
        ch = orig_chain(self, option_type)
        self._probe_chain = ch
        return ch

    ta._OptionEntryAction._chain = _chain

    orig_resolve = ta.BuyPutAction._resolve

    def _resolve(self):
        out = orig_resolve(self)
        row = REC.cur
        if isinstance(out, dict) and row is not None:
            msg = str(out.get("message"))
            diag = ""
            ch = getattr(self, "_probe_chain", None)
            if ch:
                today = self._today()
                puts = [c for c in ch if getattr(c, "option_type", None) in
                        (ta.OptionRight.PUT, "put", "PUT")] or ch
                in_dte = osel.filter_dte(puts, today, self.dte_min, self.dte_max)
                liq = [c for c in in_dte if osel.passes_liquidity(
                    c, self.min_open_interest, self.max_spread_pct, self.min_volume)]
                vol_ok = [c for c in in_dte if c.volume is not None and self.min_volume is not None
                          and c.volume >= self.min_volume]
                diag = (f" |diag chain={len(ch)} puts={len(puts)} in_dte[{self.dte_min},"
                        f"{self.dte_max}]={len(in_dte)} vol>={self.min_volume}:{len(vol_ok)} "
                        f"liq_pass={len(liq)}")
            row["msg"] = msg + diag
        return out

    ta.BuyPutAction._resolve = _resolve

    # --- drawdown refinement ------------------------------------------------------------------
    orig_refine = idd.refine_max_drawdown

    def _refine(trades, max_drawdown, *, equity_at, daily_bar_low, prior_daily_bar_low,
                delta_at_entry, underlying_price_at, bars_5m_between,
                commission_per_trade=0.0, multiplier=100.0):
        res = orig_refine(trades, max_drawdown, equity_at=equity_at, daily_bar_low=daily_bar_low,
                          prior_daily_bar_low=prior_daily_bar_low, delta_at_entry=delta_at_entry,
                          underlying_price_at=underlying_price_at,
                          bars_5m_between=bars_5m_between,
                          commission_per_trade=commission_per_trade, multiplier=multiplier)
        # Same loop, recorded (read-only re-evaluation of the same callables).
        for t in trades:
            contract, und = t.get("contract_symbol"), t.get("underlying_symbol")
            if not contract or not und:
                continue
            d = {"contract": contract, "entry": str(t.get("entry_time")),
                 "exit": str(t.get("exit_time")), "pnl": t.get("pnl"),
                 "bars_held": t.get("bars_held"), "flagged": False}
            try:
                exit_low = daily_bar_low(und, t.get("exit_time"))
                prior_low = prior_daily_bar_low(und, t.get("exit_time"))
                if not idd.is_flagged_for_intraday_check(t, prior_low, exit_low):
                    REC.refine.append(d)
                    continue
                d["flagged"] = True
                delta = delta_at_entry(und, contract, t.get("entry_time"))
                upx = underlying_price_at(und, t.get("entry_time"))
                d["delta"], d["entry_underlying"] = delta, upx
                if delta is None or upx is None:
                    d["uncovered"] = True
                    REC.refine.append(d)
                    continue
                bars = bars_5m_between(und, t.get("entry_time"), t.get("exit_time"))
                sign = 1.0 if t.get("direction") == "buy" else -1.0
                worst = idd.estimate_worst_intraday_pnl(
                    entry_premium=t["entry_price"], entry_underlying_price=upx, delta=delta,
                    size=t["size"], multiplier=multiplier, commission=commission_per_trade,
                    bars_5m=bars, direction_sign=sign)
                d["n_5m_bars"] = len(bars)
                d["worst_pnl"] = worst
                if worst is not None:
                    realised = t.get("pnl") or 0.0
                    extra = min(0.0, worst - realised)
                    eq = equity_at(t.get("entry_time"))
                    d["extra_loss"] = extra
                    d["equity_at_entry"] = eq
                    if extra and eq:
                        d["candidate_dd"] = max_drawdown + extra / eq * 100.0
            except Exception as e:  # noqa: BLE001
                d["error"] = repr(e)[:200]
            REC.refine.append(d)
        REC.refine_summary = {"input_daily_dd": max_drawdown, "refined": res}
        return res

    idd.refine_max_drawdown = _refine


def _leaf_labels(entry_rules):
    """Leaf ids of the (single) decoded entry rule in tree order == trigger order."""
    rule = entry_rules[0]
    out = []

    def walk(n):
        if isinstance(n, dict):
            kids = n.get("conditions")
            if isinstance(kids, list):
                for k in kids:
                    walk(k)
            elif n.get("field"):
                mode = n.get("mode")
                out.append(f"{n.get('id')}[{n.get('field')} {mode or n.get('op')} {n.get('value')}]")

    walk(rule["conditions"])
    return out


# ------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", required=True, help="a key of olp_genomes.json picks, or 'control'")
    ap.add_argument("--label", default=None)
    ap.add_argument("--schedule", default="monday,tuesday,wednesday,thursday,friday",
                    help="control only: entry weekdays")
    ap.add_argument("--override", default=None, help="JSON dict of genes to override")
    args = ap.parse_args()
    label = args.label or args.pick
    out = os.path.join(OUT_DIR, label)
    os.makedirs(out, exist_ok=True)

    t0 = time.time()
    _block_network()
    import ba2test_launcher as L
    L._enter_backend()           # chdir backend + ba2_common -> DATABASE_URL (the scratch DB)
    _seed_scratch_db()

    data = json.load(open(GENOMES, encoding="utf-8"))
    row = data["row"]
    bt_block = copy.deepcopy(row["optimization_config"]["backtest"])
    # LOCAL REMAPS (documented deviations from the grid host)
    bt_block["screener_opt"]["store"] = LOCAL_SCREENER_STORE
    bt_block["market_condition_manifests"] = dict(LOCAL_MANIFESTS)
    if isinstance(bt_block.get("market_condition"), dict):
        bt_block["market_condition"]["manifests"] = dict(LOCAL_MANIFESTS)

    # Strategy exactly as the launcher builds it for this job.
    L._OPTION_GATES_OFF = False
    L._MARKET_CONDITION_PROFILES = tuple(bt_block["market_condition_profiles"])
    L._MARKET_CONDITION_MANIFESTS = dict(LOCAL_MANIFESTS)
    L._OPTION_MIN_VOLUME = int(bt_block["entry_action"].get("option_min_volume", 25))
    strat = L._build_strategy("O_LP", row["name"], "DeterministicScorer")

    from app.services.strategy_param_space import collect_param_space, decode_params
    from app.services.strategy_optimization_handler import (_build_daily_trial_config,
                                                            _build_hoisted_state)
    space = collect_param_space(strat)
    gene_keys = set(row["parameter_ranges"])
    rule_keys = {k for k in gene_keys if not k.startswith(("model:", "schedule:"))}
    missing = sorted(rule_keys - set(space))
    extra = sorted(set(k for k in space if not k.startswith(("model:", "schedule:"))) - rule_keys)
    gene_check = {"rule_genes_grid": len(rule_keys), "rule_genes_local": len(
        [k for k in space if not k.startswith(("model:", "schedule:"))]),
        "missing_locally": missing, "extra_locally": extra}
    print("gene-space check:", gene_check, flush=True)

    if args.pick == "control":
        params = _control_params([d.strip() for d in args.schedule.split(",") if d.strip()])
        ga = None
    else:
        pick = data["picks"][args.pick]
        params = dict(pick["params"])
        ga = {k: pick.get(k) for k in ("fitness", "fitness_raw", "trades", "total_return",
                                       "max_drawdown", "robustness")}
    if args.override:
        params.update(json.loads(args.override))

    decoded = decode_params(strat, params)
    hoisted = _build_hoisted_state(bt_block)
    cfg = _build_daily_trial_config(bt_block, decoded, hoisted, option_trade_records=True)
    labels = _leaf_labels(decoded["entry_rules"])
    with open(os.path.join(out, "decoded_rules.json"), "w", encoding="utf-8") as f:
        json.dump({"params": params, "entry_rules": decoded["entry_rules"],
                   "exit_rules": decoded["exit_rules"],
                   "expert_settings": cfg["experts"][0]["settings"],
                   "run_schedule_override": cfg["run_schedule_override"],
                   "entry_action_run_level": cfg["entry_action"], "leaf_labels": labels},
                  f, indent=1, default=str)
    _install_hooks(labels)

    # Capture the account's orders + counters just before build_results reads it.
    import app.services.backtest.results as res_mod
    orig_build = res_mod.build_results
    orders_dump = {}

    def _build_results(account, config):
        from ba2_common.core.types import OrderStatus
        try:
            allo = account._orders_filtered(statuses=list(OrderStatus))
        except Exception as e:  # noqa: BLE001
            allo = []
            orders_dump["error"] = repr(e)
        rows = []
        for o in allo:
            meta = REC.order_meta.get(o.id)
            rows.append({"id": o.id, "status": getattr(o.status, "value", str(o.status)),
                         "side": getattr(o.side, "value", str(o.side)),
                         "type": getattr(o.order_type, "value", str(o.order_type)),
                         "qty": o.quantity, "filled": getattr(o, "filled_qty", None),
                         "limit": getattr(o, "limit_price", None),
                         "contract": getattr(o, "contract_symbol", None),
                         "symbol": o.symbol, "placed": meta[0] if meta else None,
                         "entry": meta is not None,
                         "comment": (getattr(o, "comment", "") or "")[-120:]})
        orders_dump["orders"] = rows
        orders_dump["rejected_illiquid_fills"] = getattr(account, "rejected_illiquid_fills", None)
        orders_dump["rejected_arb_fills"] = getattr(account, "rejected_arb_fills", None)
        return orig_build(account, config)

    res_mod.build_results = _build_results

    # LOCAL DEVIATION: the local snapshots end 2025-12-30 and the window check asks for the
    # 2025-12-31 session too (a single session at the very end of the window); skip that check
    # rather than re-warm the shared cache. Entries on 2025-12-31 read missing_session.
    import app.services.backtest.seam_wiring as sw
    sw.check_market_condition_window = lambda config, reader: []

    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    results = run_daily_backtest(cfg)
    elapsed = time.time() - t0

    from app.services.strategy_fitness import compute_fitness
    fit_in = copy.deepcopy(results)
    try:
        fitness = compute_fitness(row["fitness_metric"], fit_in)
    except Exception as e:  # noqa: BLE001
        fitness = f"ERR {e!r}"

    trades = results.get("trades") or []
    eq = results.get("equity_curve") or []
    # per-year P&L: trades by EXIT year; equity-curve calendar-year returns
    per_year = collections.OrderedDict()
    for t in trades:
        y = (t.get("exit_time") or "")[:4]
        d = per_year.setdefault(y, {"trades": 0, "pnl": 0.0, "wins": 0})
        d["trades"] += 1
        d["pnl"] += t.get("pnl") or 0.0
        d["wins"] += 1 if (t.get("pnl") or 0) > 0 else 0
    eq_year = collections.OrderedDict()
    prev_end = None
    for pt in eq:
        y = pt["date"][:4]
        e = eq_year.setdefault(y, {"start": prev_end if prev_end is not None else pt["equity"],
                                   "end": None, "min": pt["equity"], "max": pt["equity"]})
        e["end"] = pt["equity"]
        e["min"] = min(e["min"], pt["equity"])
        e["max"] = max(e["max"], pt["equity"])
        prev_end = pt["equity"]
    for y, e in eq_year.items():
        e["ret_pct"] = round((e["end"] / e["start"] - 1) * 100, 2) if e["start"] else None
    # daily-curve drawdown trough
    peak, trough = None, (0.0, None, None)
    for pt in eq:
        if peak is None or pt["equity"] > peak[0]:
            peak = (pt["equity"], pt["date"])
        dd = (pt["equity"] / peak[0] - 1) * 100 if peak[0] else 0
        if dd < trough[0]:
            trough = (dd, pt["date"], peak)
    min_eq = min(eq, key=lambda p: p["equity"]) if eq else None

    # funnel
    fun = collections.Counter()
    for r in REC.rows:
        key = r["stage"]
        if key == "cond_fail":
            key = f"cond_fail@{r['fail_idx']}:{r['fail_leaf']}"
            if r["fail_status"] and r["fail_status"] not in ("ok",):
                key += f" <{r['fail_status']}>"
        elif key == "action_refused":
            m = (r["msg"] or "")
            key = "action_refused: " + m.split(" for ")[0].split("|diag")[0][:90]
        fun[key] += 1
    sig = collections.Counter(r["sig"] for r in REC.rows)
    osts = collections.Counter((o["entry"], o["status"]) for o in orders_dump.get("orders", []))

    summary = {
        "label": label, "pick": args.pick, "elapsed_s": round(elapsed, 1),
        "gene_check": gene_check,
        "ga_record": ga,
        "rerun": {k: results.get(k) for k in (
            "total_trades", "total_return", "annualized_return", "max_drawdown",
            "max_drawdown_daily", "win_rate", "profit_factor", "account_wiped_out",
            "adjusted_annualized_return", "calmar_ratio")},
        "rerun_fitness": fitness,
        "robustness": fit_in.get("robustness"),
        "n_entry_days": len(REC.bars),
        "entry_days_first_last": [REC.bars[0][0], REC.bars[-1][0]] if REC.bars else None,
        "decision_points": len(REC.rows),
        "universe_offered_total": sum(n for _, n in REC.bars),
        "screen_sizes": collections.Counter(n for _, n in REC.screen).most_common(5),
        "signals": dict(sig),
        "funnel": dict(fun.most_common()),
        "orders_by_(entry,status)": {f"{k[0]}|{k[1]}": v for k, v in osts.items()},
        "rejected_illiquid_fills": orders_dump.get("rejected_illiquid_fills"),
        "rejected_arb_fills": orders_dump.get("rejected_arb_fills"),
        "per_year_trades": per_year, "per_year_equity": eq_year,
        "daily_curve_worst_dd": {"dd_pct": round(trough[0], 2), "trough_date": trough[1],
                                 "peak": trough[2]},
        "min_equity_point": min_eq,
        "refine_summary": REC.refine_summary,
        "refine_worst": sorted([d for d in REC.refine if d.get("candidate_dd") is not None],
                               key=lambda d: d["candidate_dd"])[:8],
        "open_positions": results.get("open_positions"),
        "option_basis_guard": results.get("option_basis_guard"),
        "market_condition": {k: v for k, v in (results.get("market_condition") or {}).items()
                             if k != "entries"} if isinstance(results.get("market_condition"), dict) else None,
    }
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, default=str)
    with open(os.path.join(out, "trades.json"), "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=1, default=str)
    with open(os.path.join(out, "equity_curve.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "equity"])
        for pt in eq:
            w.writerow([pt["date"], pt["equity"]])
    with gzip.open(os.path.join(out, "funnel_rows.csv.gz"), "wt", newline="", encoding="utf-8") as f:
        if REC.rows:
            w = csv.DictWriter(f, fieldnames=list(REC.rows[0]))
            w.writeheader()
            w.writerows(REC.rows)
    with open(os.path.join(out, "orders.json"), "w", encoding="utf-8") as f:
        json.dump(orders_dump, f, indent=1, default=str)
    with open(os.path.join(out, "refine_trades.json"), "w", encoding="utf-8") as f:
        json.dump(REC.refine, f, indent=1, default=str)

    print(json.dumps({k: summary[k] for k in ("label", "elapsed_s", "ga_record", "rerun",
                                              "rerun_fitness", "decision_points", "signals",
                                              "funnel", "orders_by_(entry,status)",
                                              "refine_summary")}, indent=1, default=str),
          flush=True)


if __name__ == "__main__":
    main()
