"""(b) the simulation's selection function on the capture's RECORDED inputs, and the measured verdicts (forming bar, cap basis).
    python sim_recorded.py <label> [instances...]"""
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from replay import load_store  # noqa: E402
from ba2_providers.screener import live_sim as ls  # noqa: E402
from ba2_providers.screener.float_filter import parse_float_table  # noqa: E402


def recorded_bars(store, today):
    """symbol -> {date: (o,h,l,c,v)} from every recorded historical-price-full response."""
    bars = defaultdict(dict)
    for (url, _p), bodies in store.items():
        if "/historical-price-full/" not in url:
            continue
        for body in bodies:
            data = json.loads(body)
            lst = data.get("historicalStockList", [data] if "historical" in data else [])
            for e in lst:
                s = (e.get("symbol") or "").upper()
                for b in e.get("historical", []):
                    bars[s][b["date"]] = (b.get("open"), b.get("high"), b.get("low"), b.get("close"), b.get("volume"))
    return bars


def stage(stages, name, nth=0):
    got = [s for s in stages if s["stage"] == name]
    return got[nth] if len(got) > nth else None


def main(label, iids=None):
    root = os.path.join(HERE, label)
    store, _ = load_store(root)
    float_bodies = [b for (u, _), bs in store.items() if u.endswith("/shares_float/all") for b in bs]
    float_table = parse_float_table(json.loads(float_bodies[0])) if float_bodies else {}
    insts = sorted(int(os.path.basename(d)[4:]) for d in glob.glob(os.path.join(root, "inst*")))
    bars_all = None
    report = {"label": label, "instances": {}, "verdicts": {}}
    quote_cmp = []
    bar_cmp = []
    cap_rows = []
    for iid in (iids or insts):
        cap = json.load(open(os.path.join(root, f"inst{iid}", "result.json")))
        stg = json.load(open(os.path.join(root, f"inst{iid}", "stages.json")))
        if cap["error"]:
            continue
        today = cap["started_utc"][:10]
        if bars_all is None:
            bars_all = recorded_bars(store, today)
        s1 = stage(stg, "provider.screen_stocks")
        enr = stage(stg, "_enrich_with_rvol")
        qfb = stage(stg, "_quotes_from_bars")
        # quotes: merged over every _fetch_quotes_chunked stage of the instance (+ earlier instances' cache hits are in other files)
        quotes = {}
        for st in [x for x in stg if x["stage"] == "_fetch_quotes_chunked"]:
            quotes.update(st["out"])
        res_set = {k[len("screener_"):]: v for k, v in cap["resolved_settings"].items() if k.startswith("screener_") and k != "screener_provider"}
        syms = [c["symbol"].upper() for c in (s1["out"] or [])]
        vend = {c["symbol"].upper(): c for c in s1["out"]}
        # ---------- finished bars (< today) and the forming bar ----------
        sessions = sorted({d for s in syms for d in bars_all.get(s, {}) if d < today} | {today})
        didx = {d: i for i, d in enumerate(sessions)}
        T = len(sessions)
        bars = {}
        shares = np.full((len(syms), T), np.nan)
        flm = np.full((len(syms), T), np.nan)
        nowv = np.full(len(syms), np.nan)
        for k, s in enumerate(syms):
            b = bars_all.get(s, {})
            ds = sorted(d for d in b if d < today)
            if ds:
                idx = np.array([didx[d] for d in ds])
                arr = np.array([b[d] for d in ds], dtype=float)
                bars[s] = (idx, arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4])
                lc = arr[-1, 3]
            else:
                lc = np.nan
            q = quotes.get(s) or {}
            capq = q.get("marketCap")
            # shares so that previous close x shares == the QUOTE market cap (the live rank key), as-traded basis
            if capq and q.get("price") and lc == lc and lc > 0:
                shares[k, :] = capq / q["price"]        # the vendor's share count; rank key = quote price x shares = the quote's marketCap
            fv = float_table.get(s)
            if fv:
                flm[k, :] = fv
            nowv[k] = q.get("price") if q.get("price") else np.nan
            # forming bar (dated today) and its relation to the quote
            if today in b:
                o, h, l, c, v = b[today]
                bar_cmp.append({"iid": iid, "symbol": s, "bar_close": c, "bar_high": h, "bar_low": l, "bar_open": o, "bar_vol": v,
                                "quote_price": q.get("price"), "quote_open": q.get("open"), "quote_high": q.get("dayHigh"), "quote_low": q.get("dayLow"),
                                "quote_vol": q.get("volume")})
            else:
                bar_cmp.append({"iid": iid, "symbol": s, "bar_close": None, "quote_price": q.get("price")})
        arrays = ls.build_panel_arrays(bars, sessions, shares, syms, fl=flm)
        panel = ls.DailyPanel(syms, sessions, arrays, {})
        # sim settings: band by the VENDOR (stage 1 is the vendor's); every other criterion is the simulation's
        st = {k: v for k, v in res_set.items() if k != "universe_mode"}
        st["market_cap_min"] = 0
        st["market_cap_max"] = 0
        now_fn = (lambda idx: (nowv[idx], nowv[idx]))
        fh = None
        diag = {}
        # price floors compare the vendor's price in stage 1: here they are the sim's own test on the quote price
        fclose = np.array([bars_all.get(s, {}).get(today, (None,) * 5)[3] or np.nan for s in syms], dtype=float)   # the forming bar's close, as returned
        fhigh = np.array([bars_all.get(s, {}).get(today, (None,) * 5)[1] or np.nan for s in syms], dtype=float)
        picks = panel.select(today, st, ls.POST_FIX, now=now_fn, diag=diag, forming_close=lambda idx: fclose[idx],
                             forming_hi=lambda idx: fhigh[idx])
        live_picks = cap["picks"]
        # per-term comparison of stage-2 numbers (live's bar-derived quote vs the sim's panel columns)
        diffs = []
        p = panel.pos(today)
        cols = panel.columns(p)
        for k, s in enumerate(syms):
            lv = (qfb["out"] or {}).get(s) if qfb else None
            if lv is None:
                continue
            for name, simv, livev in (("avg_volume", cols["avg20"][k], lv.get("avgVolume")), ("last_volume", cols["last_vol"][k], lv.get("volume")),
                                      ("last_close", cols["last_close"][k], lv.get("price"))):
                if livev is None or simv != simv or abs(float(simv) - float(livev)) > 1e-9 * max(1.0, abs(float(livev))):
                    diffs.append((s, name, None if simv != simv else float(simv), livev))
        # cap basis: vendor stage-1 cap vs prev close x vendor shares vs quote price x vendor shares
        for s in syms:
            v = vend[s]
            if not (v.get("market_cap") and v.get("price")):
                continue
            sh_v = v["market_cap"] / v["price"]
            b = bars_all.get(s, {})
            ds = sorted(d for d in b if d < today)
            q = quotes.get(s) or {}
            if not ds or not q.get("price"):
                continue
            pc = b[ds[-1]][3]
            cap_rows.append({"iid": iid, "symbol": s, "vendor_cap": v["market_cap"], "vendor_price": v["price"],
                             "prev_close": pc, "quote_price": q["price"], "quote_cap": q.get("marketCap"),
                             "B_prevclose_x_vshares": pc * sh_v, "C_quote_x_vshares": q["price"] * sh_v})
        report["instances"][iid] = {"live_picks": live_picks, "sim_on_recorded": picks, "identical": picks == live_picks,
                                    "live_only": [x for x in live_picks if x not in picks], "sim_only": [x for x in picks if x not in live_picks],
                                    "term_differences": diffs[:40], "n_term_differences": len(diffs), "sim_stages": diag,
                                    "stage_counts_live": {s["stage"]: len(s["out"]) if isinstance(s["out"], list) else None for s in stg if s["stage"] != "_quotes_from_bars"}}
        print(f"inst {iid}: live {len(live_picks)} | sim-on-recorded {len(picks)} | IDENTICAL {picks == live_picks} | live-only {report['instances'][iid]['live_only']} "
              f"| sim-only {report['instances'][iid]['sim_only']} | term diffs {len(diffs)}", flush=True)
    # ---------------- verdicts ----------------
    have = [r for r in bar_cmp if r.get("bar_close") is not None]
    report["verdicts"]["forming_bar"] = {"symbols_checked": len({r['symbol'] for r in bar_cmp}), "with_bar_dated_today": len({r['symbol'] for r in have})}
    if have:
        d_close = np.array([abs(r["bar_close"] / r["quote_price"] - 1) for r in have if r.get("quote_price")])
        d_high = np.array([abs(r["bar_high"] / r["quote_high"] - 1) for r in have if r.get("quote_high")])
        report["verdicts"]["forming_bar"].update({"close_vs_quote_median": float(np.median(d_close)) if d_close.size else None,
                                                  "close_vs_quote_p95": float(np.quantile(d_close, .95)) if d_close.size else None,
                                                  "high_vs_quote_dayHigh_median": float(np.median(d_high)) if d_high.size else None})
    if cap_rows:
        a = np.array([r["vendor_cap"] for r in cap_rows])
        eb = np.abs(np.array([r["B_prevclose_x_vshares"] for r in cap_rows]) / a - 1)
        ec = np.abs(np.array([r["C_quote_x_vshares"] for r in cap_rows]) / a - 1)
        # by construction (cap/price) the vendor cap equals vendor_price x shares; B/C differ from it by price ratios only
        report["verdicts"]["cap_basis"] = {"n": len(cap_rows), "prev_close_basis_median_abs_err": float(np.median(eb)), "quote_basis_median_abs_err": float(np.median(ec)),
                                           "share_closer_to_prev_close": float((eb < ec).mean()), "share_closer_to_quote": float((ec < eb).mean()),
                                           "vendor_price_equals_quote_price_share": float(np.mean([abs(r["vendor_price"] / r["quote_price"] - 1) < 1e-4 for r in cap_rows])),
                                           "vendor_price_equals_prev_close_share": float(np.mean([abs(r["vendor_price"] / r["prev_close"] - 1) < 1e-4 for r in cap_rows]))}
        qc = [r for r in cap_rows if r.get("quote_cap")]
        if qc:
            report["verdicts"]["cap_basis"]["quote_marketCap_over_quote_price_x_vshares_median"] = float(np.median([r["quote_cap"] / r["C_quote_x_vshares"] for r in qc]))
            report["verdicts"]["cap_basis"]["quote_marketCap_over_prev_close_x_vshares_median"] = float(np.median([r["quote_cap"] / r["B_prevclose_x_vshares"] for r in qc]))
    json.dump({"report": report, "bar_cmp": bar_cmp, "cap_rows": cap_rows}, open(os.path.join(root, "sim_recorded.json"), "w"), default=str)
    print(json.dumps(report["verdicts"], indent=1, default=str))
    return report


if __name__ == "__main__":
    main(sys.argv[1], [int(x) for x in sys.argv[2:]] or None)
