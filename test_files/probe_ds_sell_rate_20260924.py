"""Ad-hoc probe (2026-09-24): how often does DeterministicScorer emit a USABLE SELL on the
stage-1 option-grid universe (tools/options_universe_top100.txt, 97 symbols) over
2020-01-01..2025-12-31?  Question behind it: can the bearish option arms (O_LP long put,
O_BEARCS bear call spread) ever trade under this expert?

MEASUREMENT ONLY. No production code is touched.

HOW (two phases)
  compute   Per symbol (process pool), run the REAL backtest decision path:
            DeterministicScorer.analyze_as_of(as_of, BacktestContext(LiveProviderBundle(...)))
            with the same seams run_daily_backtest wires (wire_backtest_seams, a cached_only
            MemoizedOHLCVProvider installed as the run's OHLCV override, frozen_ttl_cache,
            hermetic_fmp_history), as_of = midnight-UTC of each bar exactly like
            DailyBacktestEngine. The expert is built with __new__ (no trading DB), as the
            replay/historical harness does; credentials answered offline; any requests.* call
            is blocked. The probe settings switch ON every section (w_analyst / w_earnings > 0)
            so each bar records ALL section scores (technical, fundamental, analyst, earnings),
            the veto flag and the regime (+ n_inputs). Section scores do not depend on the
            section weights / macro_mode / thetas, so phase 2 can re-combine them for any genome.
  analyse   For each settings variant, re-run the REAL combine.final_score + schmitt_trigger +
            confidence_from_score on the recorded sections (no re-implementation), then
            tabulate distributions, SELL rates, confidence-gate pass rates, per year.
            A vectorised twin of final_score (checked bit-for-bit against the real one on
            every named variant) sweeps the WHOLE GA weight grid for the best-case SELL supply.

Usage:
  .venv/Scripts/python.exe test_files/probe_ds_sell_rate_20260924.py compute [--workers 8] [--limit N]
  .venv/Scripts/python.exe test_files/probe_ds_sell_rate_20260924.py analyse
"""
from __future__ import annotations

import os
import sys

OUT_DIR = (r"C:\Users\basti\AppData\Local\Temp\claude\C--Users-basti-Documents-dev-"
           r"BA2TradePlatform\820f80e0-b6ea-41a0-8f76-d0176d9f7156\scratchpad\ds_sell")
ROWS_DIR = os.path.join(OUT_DIR, "rows")
# Any accidental DB open lands in a scratch file, never a live/dev DB.
os.environ.setdefault("DB_FILE", os.path.join(OUT_DIR, "probe_scratch.sqlite"))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(REPO, "packages", "common"), os.path.join(REPO, "packages", "providers"),
          os.path.join(REPO, "packages", "experts"), os.path.join(REPO, "testplatform"),
          os.path.join(REPO, "testplatform", "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

import logging  # noqa: E402

logging.disable(logging.INFO)  # standalone backtest-style scripts run 10x slower without this

import argparse  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

START = datetime(2020, 1, 1)
END = datetime(2025, 12, 31)
WARMUP_DAYS = 700  # >= DS OHLCV_LOOKBACK_DAYS (600) + slack
UNIVERSE_FILE = os.path.join(REPO, "tools", "options_universe_top100.txt")


def _universe():
    with open(UNIVERSE_FILE, encoding="utf-8") as f:
        return list(dict.fromkeys(s.upper() for s in f.read().split()))


# --------------------------------------------------------------------------- compute
def _block_network():
    import requests.sessions

    def _blocked(self, method, url, *a, **k):  # noqa: ANN001
        raise RuntimeError(f"PROBE NETWORK BLOCK: {method} {url}")

    requests.sessions.Session.request = _blocked


def _probe_settings():
    from ba2_experts.DeterministicScorer import DeterministicScorer
    defs = DeterministicScorer.get_settings_definitions()
    s = {k: d["default"] for k, d in defs.items()
         if k in DeterministicScorer._SETTING_KEYS and "default" in d}
    # Switch every optional section ON so it is gathered + scored (values irrelevant: the
    # probe re-combines the recorded section scores for every genome in phase 2).
    s["w_analyst"] = 0.2
    s["w_earnings"] = 0.2
    s["macro_mode"] = "multiply"  # 'off' would skip _build_regime entirely
    return s


def _compute_symbol(symbol: str) -> dict:
    out_path = os.path.join(ROWS_DIR, f"{symbol}.json")
    if os.path.exists(out_path):
        return {"symbol": symbol, "status": "cached"}
    t0 = time.time()
    _block_network()
    from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
    from ba2_common.core.TradeConditions import _get_provider
    from ba2_providers import get_provider
    from ba2_providers.fmp_common import (frozen_ttl_cache, hermetic_fmp_history,
                                          hermetic_miss_symbols, reset_hermetic_misses)
    from ba2_experts.DeterministicScorer import DeterministicScorer
    from app.services.backtest.price_source import MemoizedOHLCVProvider
    from app.services.backtest.seam_wiring import set_backtest_ohlcv_override, wire_backtest_seams
    from app.services.replay.historical import OFFLINE_API_KEY, offline_credentials

    wire_backtest_seams()
    settings = _probe_settings()
    rows = []
    errors = []
    reset_hermetic_misses()
    with offline_credentials(), frozen_ttl_cache(), hermetic_fmp_history():
        ohlcv = MemoizedOHLCVProvider(get_provider("ohlcv", "fmp"),
                                      START - timedelta(days=WARMUP_DAYS), END,
                                      interval="1d", cached_only=True)
        set_backtest_ohlcv_override(ohlcv)
        providers = LiveProviderBundle(lambda c, n, **kw: _get_provider(c, n, **kw))
        expert = DeterministicScorer.__new__(DeterministicScorer)
        expert.id = 1
        expert.logger = logging.getLogger("probe.ds")
        expert._get_fmp_api_key = lambda: OFFLINE_API_KEY
        # Bar calendar = the symbol's own cached daily bars inside the window (the engine uses
        # the price source's trading days; per-symbol bars are the same calendar minus gaps).
        df = ohlcv.get_ohlcv_data(symbol=symbol, start_date=START - timedelta(days=WARMUP_DAYS),
                                  end_date=END, interval="1d")
        import pandas as pd
        dates = pd.to_datetime(df["Date"])
        if dates.dt.tz is not None:
            dates = dates.dt.tz_convert("UTC").dt.tz_localize(None)
        days = sorted({d.date() for d in dates if START <= d.to_pydatetime() <= END + timedelta(hours=23)})
        for d in days:
            as_of = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)  # engine convention
            expert._gather_symbol = symbol
            ctx = BacktestContext(providers=providers, settings=settings, as_of=as_of,
                                  extra={"symbol": symbol})
            try:
                rec = expert.analyze_as_of(as_of, ctx)
            except Exception as e:  # noqa: BLE001 - recorded, reported
                errors.append(f"{d}: {type(e).__name__}: {e}")
                if len(errors) > 20:
                    break
                continue
            if getattr(rec, "skip", False):
                rows.append({"d": str(d), "skip": rec.skip_reason})
                continue
            ro = rec.raw_outputs or {}
            calc = ro.get("calc") or {}
            reg = ro.get("regime") or {}
            comb = ro.get("combination") or {}
            rows.append({
                "d": str(d),
                "t": calc.get("technical"), "f": calc.get("fundamental"),
                "a": calc.get("analyst"), "e": calc.get("earnings"),
                "veto": bool(calc.get("veto")),
                "r": reg.get("score"), "rn": reg.get("n_inputs"),
                "price": rec.current_price, "atr": calc.get("atr"),
                "probe_final": calc.get("final_score"),
                "probe_n_sections": comb.get("n_sections"),
            })
        set_backtest_ohlcv_override(None)
    misses = sorted(hermetic_miss_symbols())
    payload = {"symbol": symbol, "rows": rows, "errors": errors, "hermetic_misses": misses,
               "elapsed": time.time() - t0}
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, out_path)
    return {"symbol": symbol, "status": "ok", "n": len(rows), "errors": len(errors),
            "misses": misses, "elapsed": round(time.time() - t0, 1)}


def cmd_compute(args):
    os.makedirs(ROWS_DIR, exist_ok=True)
    syms = _universe()
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",")]
    if args.limit:
        syms = syms[: args.limit]
    print(f"compute: {len(syms)} symbols, {args.workers} workers -> {ROWS_DIR}", flush=True)
    t0 = time.time()
    if args.workers <= 1:
        for s in syms:
            print(_compute_symbol(s), flush=True)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_compute_symbol, s): s for s in syms}
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001
                    r = {"symbol": futs[fut], "status": f"FAILED {type(e).__name__}: {e}"}
                print(f"[{i}/{len(syms)} {time.time() - t0:.0f}s] {r}", flush=True)
    print(f"compute done in {time.time() - t0:.0f}s", flush=True)


# --------------------------------------------------------------------------- analyse
VARIANTS = {
    # name: (w_technical, w_fundamental, w_analyst, w_earnings) -- all inside the GA gene ranges
    "default(.5/.3/0/0)": (0.5, 0.3, 0.0, 0.0),
    "tech_only(.8/0/0/0)": (0.8, 0.0, 0.0, 0.0),
    "fund_only(0/.7/0/0)": (0.0, 0.7, 0.0, 0.0),
    "all4(.4/.3/.4/.4)": (0.4, 0.3, 0.4, 0.4),
    "analyst_only(0/0/.4/0)": (0.0, 0.0, 0.4, 0.0),
    "earnings_only(0/0/0/.4)": (0.0, 0.0, 0.0, 0.4),
}
MODES = ("multiply", "gate", "off")
THETA_SELL = (0.1, 0.2, 0.3, 0.4)
CONF_GATES = (10, 20, 30, 40, 45, 50)
YEARS = list(range(2020, 2026))


def _load_rows():
    import pandas as pd
    frames, meta = [], []
    for fn in sorted(os.listdir(ROWS_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(ROWS_DIR, fn), encoding="utf-8") as f:
            p = json.load(f)
        meta.append({"symbol": p["symbol"], "n": len(p["rows"]), "errors": len(p["errors"]),
                     "first_error": p["errors"][0] if p["errors"] else "",
                     "hermetic_misses": ",".join(p["hermetic_misses"])})
        if p["rows"]:
            df = pd.DataFrame(p["rows"])
            df["symbol"] = p["symbol"]
            frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    if "skip" not in df.columns:
        df["skip"] = None
    df["d"] = pd.to_datetime(df["d"])
    df["year"] = df["d"].dt.year
    df["wd"] = df["d"].dt.weekday
    return df, pd.DataFrame(meta)


def _real_eval(df, weights, mode, base):
    """REAL combine.final_score per row -> (final, pre_macro, mult)."""
    import math
    from ba2_experts.DeterministicScorer.combine import final_score

    def nz(v):
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)

    s = dict(base)
    s.update({"w_technical": weights[0], "w_fundamental": weights[1],
              "w_analyst": weights[2], "w_earnings": weights[3], "macro_mode": mode})
    s_off = dict(s, macro_mode="off")
    finals, pres, mults, nsec = [], [], [], []
    for t, f, a, e, veto, r, rn in zip(df["t"], df["f"], df["a"], df["e"], df["veto"],
                                       df["r"], df["rn"]):
        kw = dict(technical=nz(t), fundamental=nz(f), analyst=nz(a), earnings=nz(e),
                  veto=bool(veto))
        rr = nz(r)
        rni = None if nz(rn) is None else int(rn)
        out = final_score(regime=rr, s=s, regime_n_inputs=rni, **kw)
        pre = final_score(regime=None, s=s_off, **kw)
        finals.append(out["final"])
        pres.append(pre["final"])
        mults.append(out["exposure_multiplier"])
        nsec.append(out["n_sections"])
    import numpy as np
    return np.array(finals), np.array(pres), np.array(mults), np.array(nsec)


def cmd_analyse(args):
    import numpy as np
    import pandas as pd
    from ba2_experts.DeterministicScorer.combine import (confidence_from_score, schmitt_trigger)

    df_all, meta = _load_rows()
    meta.to_csv(os.path.join(OUT_DIR, "symbol_coverage.csv"), index=False)
    df = df_all[df_all["skip"].isna()].reset_index(drop=True)
    n_skip = int(df_all["skip"].notna().sum())
    print(f"rows: {len(df_all)} total, {n_skip} skipped (insufficient history), {len(df)} scored; "
          f"symbols with data: {df['symbol'].nunique()}")
    print(f"symbols with errors: {int((meta['errors'] > 0).sum())}; with hermetic misses: "
          f"{int((meta['hermetic_misses'] != '').sum())}")
    cov = {c: float(df[c].notna().mean()) for c in ("t", "f", "a", "e", "r")}
    print("section coverage (non-null share):", {k: round(v, 3) for k, v in cov.items()})
    print("regime n_inputs dist:", df["rn"].value_counts(dropna=False).to_dict())
    print("veto share:", round(float(df["veto"].mean()), 4))

    from ba2_experts.DeterministicScorer import DeterministicScorer
    defs = DeterministicScorer.get_settings_definitions()
    base = {k: d["default"] for k, d in defs.items()
            if k in DeterministicScorer._SETTING_KEYS and "default" in d}
    n_sym = df["symbol"].nunique()

    dist_rows, sell_rows, conf_rows, year_rows = [], [], [], []
    per_bar = {}
    for vname, w in VARIANTS.items():
        for mode in MODES:
            final, pre, mult, nsec = _real_eval(df, w, mode, base)
            key = f"{vname}|{mode}"
            per_bar[key] = final
            if mode == "multiply":
                per_bar[f"{vname}|pre_macro"] = pre
                per_bar[f"{vname}|mult"] = mult
            valid = nsec > 0
            for yr in YEARS + ["all"]:
                m = valid & ((df["year"] == yr).to_numpy() if yr != "all" else True)
                x = final[m]
                if len(x) == 0:
                    continue
                dist_rows.append({"variant": vname, "mode": mode, "year": yr, "n": len(x),
                                  "min": x.min(), "p1": np.percentile(x, 1),
                                  "p5": np.percentile(x, 5), "p10": np.percentile(x, 10),
                                  "p50": np.percentile(x, 50), "max": x.max(),
                                  "zero_share": float((x == 0).mean())})
                for th in THETA_SELL:
                    s = dict(base, theta_sell=th)
                    sell = np.array([schmitt_trigger(v, s, None) == "SELL" for v in x])
                    n_sell = int(sell.sum())
                    n_yrs = 6 if yr == "all" else 1
                    rec = {"variant": vname, "mode": mode, "year": yr, "theta_sell": th,
                           "n_bars": len(x), "n_sell": n_sell,
                           "sell_pct": 100.0 * n_sell / len(x),
                           "sell_bars_per_yr": n_sell / n_yrs,
                           "sell_symbols": int(df.loc[m, "symbol"].to_numpy()[sell].__len__() and
                                               len(set(df.loc[m, "symbol"].to_numpy()[sell])))}
                    conf = np.array([confidence_from_score(v) for v in x[sell]])
                    for g in CONF_GATES:
                        rec[f"conf>{g}"] = int((conf > g).sum())
                    sell_rows.append(rec)

    dist = pd.DataFrame(dist_rows)
    sells = pd.DataFrame(sell_rows)
    dist.to_csv(os.path.join(OUT_DIR, "final_distribution.csv"), index=False)
    sells.to_csv(os.path.join(OUT_DIR, "sell_rates.csv"), index=False)

    # Pre-macro vs final under multiply, 2022 focus: does multiply shrink the bear-year SELLs?
    macro_rows = []
    for vname in VARIANTS:
        pre = per_bar[f"{vname}|pre_macro"]
        mult = per_bar[f"{vname}|mult"]
        for yr in YEARS:
            m = (df["year"] == yr).to_numpy()
            macro_rows.append({
                "variant": vname, "year": yr,
                "regime_mean": float(np.nanmean(df.loc[m, "r"].astype(float))),
                "regime_lt_-0.5_share": float((df.loc[m, "r"].astype(float) < -0.5).mean()),
                "mult_mean": float(np.mean(mult[m])), "mult_zero_share": float((mult[m] == 0).mean()),
                "pre<-0.2_pct": 100 * float((pre[m] < -0.2).mean()),
                "mult<-0.2_pct": 100 * float((per_bar[f"{vname}|multiply"][m] < -0.2).mean()),
                "gate<-0.2_pct": 100 * float((per_bar[f"{vname}|gate"][m] < -0.2).mean()),
                "off<-0.2_pct": 100 * float((per_bar[f"{vname}|off"][m] < -0.2).mean()),
                "pre<-0.4_pct": 100 * float((pre[m] < -0.4).mean()),
                "mult<-0.4_pct": 100 * float((per_bar[f"{vname}|multiply"][m] < -0.4).mean()),
                "off<-0.4_pct": 100 * float((per_bar[f"{vname}|off"][m] < -0.4).mean()),
            })
    macro = pd.DataFrame(macro_rows)
    macro.to_csv(os.path.join(OUT_DIR, "macro_effect_by_year.csv"), index=False)

    # Per-bar dump for the default variant (for later drill-down).
    dump = df[["symbol", "d", "t", "f", "a", "e", "veto", "r", "rn", "price", "atr"]].copy()
    for k, v in per_bar.items():
        if k.startswith("default"):
            dump[k.split("|", 1)[1]] = v
    dump.to_csv(os.path.join(OUT_DIR, "per_bar_default.csv.gz"), index=False)

    # ---- exhaustive GA weight sweep (vectorised twin, verified against the real function) ----
    sweep = _sweep(df, base, per_bar)
    sweep.to_csv(os.path.join(OUT_DIR, "weight_sweep.csv"), index=False)

    # expected-profit gate reach for SELL bars (default variant, off mode, theta 0.2)
    exp_rows = []
    fin = per_bar["default(.5/.3/0/0)|off"]
    sellm = fin < -0.2
    p = df["price"].to_numpy(float)
    a = df["atr"].to_numpy(float)
    for k in (3.0, 4.5, 6.0):
        tgt = p - k * a
        epp = np.where(tgt > 0, (p / tgt - 1) * 100, np.nan)
        exp_rows.append({"k_target": k, "sell_bars": int(sellm.sum()),
                         **{f"epp>{g}": int((epp[sellm] > g).sum()) for g in (2, 5, 10, 20)}})
    pd.DataFrame(exp_rows).to_csv(os.path.join(OUT_DIR, "sell_expected_profit_gate.csv"), index=False)

    summary = {
        "rows_total": int(len(df_all)), "rows_scored": int(len(df)), "rows_skipped": n_skip,
        "symbols": int(n_sym), "section_coverage": cov,
        "regime_n_inputs": {str(k): int(v) for k, v in df["rn"].value_counts(dropna=False).items()},
        "veto_share": float(df["veto"].mean()),
    }
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    print("\n== final distribution (all years) ==")
    print(dist[dist["year"] == "all"].round(3).to_string(index=False))
    print("\n== SELL rates, all years ==")
    print(sells[sells["year"] == "all"].round(3).to_string(index=False))
    print("\n== macro effect by year ==")
    print(macro.round(3).to_string(index=False))
    print(f"\noutputs in {OUT_DIR}")


def _sweep(df, base, per_bar):
    """Every GA weight genome x macro_mode: share of bars with final < -theta (vectorised).

    The twin mirrors combine.final_score (skip-on-missing renormalisation, tanh(raw/k), veto
    cap, multiply/gate regime) and is asserted equal to the REAL function's output on every
    named variant before it is trusted."""
    import itertools
    import numpy as np
    import pandas as pd
    from ba2_experts.DeterministicScorer.macro import exposure_multiplier

    k = float(base["k_compress"])
    veto_cap = float(base["veto_cap"])
    gate_min = float(base["macro_gate_min"])
    secs = np.stack([df[c].astype(float).to_numpy() for c in ("t", "f", "a", "e")], axis=1)
    have = ~np.isnan(secs)
    secs0 = np.nan_to_num(secs)
    veto = df["veto"].to_numpy(bool)
    r = df["r"].astype(float).to_numpy()
    rn = df["rn"].astype(float).to_numpy()
    mult = np.array([1.0 if np.isnan(ri) else exposure_multiplier(
        ri, float(base["m_floor"]), float(base["hard_riskoff"]),
        n_inputs=None if np.isnan(ni) else int(ni)) for ri, ni in zip(r, rn)])
    years = df["year"].to_numpy()

    def twin(w, mode):
        w = np.array(w, float)
        pos = w > 0
        if w[pos].sum() <= 0:
            return np.zeros(len(df)), np.zeros(len(df), bool)
        wn = np.where(pos, w / w[pos].sum(), 0.0)
        used = have & pos
        tw = (used * wn).sum(axis=1)
        valid = tw > 0
        raw = np.where(valid, (secs0 * used * wn).sum(axis=1) / np.where(valid, tw, 1), 0.0)
        sc = np.tanh(raw / k) if k > 0 else np.clip(raw, -1, 1)
        sc = np.where(veto, np.minimum(sc, veto_cap), sc)
        if mode == "multiply":
            sc = np.where(np.isnan(r), sc, sc * mult)
        elif mode == "gate":
            sc = np.where((~np.isnan(r)) & (r < gate_min), np.minimum(sc, 0.0), sc)
        return np.clip(sc, -1, 1), valid

    for vname, w in VARIANTS.items():
        for mode in MODES:
            tw, _ = twin(w, mode)
            real = per_bar[f"{vname}|{mode}"]
            if not np.allclose(tw, real, atol=1e-12):
                bad = int((~np.isclose(tw, real, atol=1e-12)).sum())
                raise AssertionError(f"vectorised twin diverges from final_score on {vname}|{mode}: "
                                     f"{bad} bars")
    rows = []
    grid = itertools.product(np.round(np.arange(0, 0.81, 0.1), 1), np.round(np.arange(0, 0.71, 0.1), 1),
                             np.round(np.arange(0, 0.41, 0.1), 1), np.round(np.arange(0, 0.41, 0.1), 1))
    for w in grid:
        if sum(w) == 0:
            continue
        for mode in MODES:
            fin, valid = twin(w, mode)
            rec = {"w_t": w[0], "w_f": w[1], "w_a": w[2], "w_e": w[3], "mode": mode,
                   "min": float(fin.min()), "p1": float(np.percentile(fin, 1))}
            for th in THETA_SELL:
                s = fin < -th
                rec[f"sell_pct@{th}"] = 100 * float(s.mean())
                rec[f"sell_per_yr@{th}"] = float(s.sum()) / 6
                rec[f"sell_2022_pct@{th}"] = 100 * float(s[years == 2022].mean())
                for g in (40, 45, 50):
                    rec[f"sell&conf>{g}@{th}"] = int((s & (100 * np.abs(fin) > g)).sum())
            rows.append(rec)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compute")
    c.add_argument("--workers", type=int, default=8)
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--symbols", default="")
    sub.add_parser("analyse")
    args = ap.parse_args()
    {"compute": cmd_compute, "analyse": cmd_analyse}[args.cmd](args)


if __name__ == "__main__":
    main()
