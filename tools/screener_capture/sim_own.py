"""(c) the simulation on its OWN inputs (the panel: vendor share histories, cached daily bars) for the capture's instant, and the
ablation that classifies every difference from live.
    python sim_own.py <label> [instances...]
Variants (each changes ONE class of input; a difference is classified by the first variant that removes it):
  V0  own panel, own shares, band by own cap, "now" = the recorded quote price           (price source held equal to live)
  V1  V0 with the VENDOR's share count (stage-1 cap / price) instead of the panel's        -> classes "share count"
  V2  V1 with the candidate universe = the vendor's stage-1 list, band off                -> "cap basis / universe / band edge"
  V3  = sim_recorded (recorded bars, recorded quote cap as rank key)                       -> "daily bars / forming bar / rule"
  W   V0 with "now" = the previous close (the strict rule, no opening-print exception)     -> price effect, reported separately
"""
import glob
import json
import os
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from replay import load_store  # noqa: E402
from sim_recorded import recorded_bars, stage  # noqa: E402
from ba2_providers.screener import live_sim as ls  # noqa: E402
from ba2_providers.screener.float_filter import parse_float_table  # noqa: E402

PANEL_ROOT = "C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/screensim/panels_real"
NS = 330                                   # sessions of the panel kept for the own-input rebuild


def build_own(today, recorded_bars_all, symbols_wanted=None):
    """Rebuild the derived columns on the panel's RAW arrays, with the sessions the cache lacks (it ends 2026-10-06 and today's capture
    day is 2026-10-08) patched from the capture's recorded daily bars: what the cache holds after the next top-up."""
    pdir = ls.list_panels_in(PANEL_ROOT)[0][0]
    pan = ls.load_panel(pdir)
    p = pan.pos(today)
    lo = max(0, p - NS)
    sess = pan.sessions[lo:p + 1]
    syms = [str(s) for s in pan.symbols]
    raw = {k: np.asarray(pan.arrays[k][lo:p]) for k in ("o", "h", "l", "c", "v")}
    T = len(sess)
    bars = {}
    patched = 0
    sidx = {d: i for i, d in enumerate(sess)}
    for j, s in enumerate(syms):
        c = raw["c"][:, j]
        ok = np.isfinite(c)
        idx = np.flatnonzero(ok)
        o, h, l, cc, v = raw["o"][ok, j], raw["h"][ok, j], raw["l"][ok, j], c[ok], raw["v"][ok, j]
        extra = recorded_bars_all.get(s, {})
        add = [(sidx[d], *extra[d]) for d in extra if d in sidx and d < today and not ok[sidx[d]]] if extra else []
        if add:
            patched += len(add)
            add.sort()
            idx = np.concatenate([idx, [a[0] for a in add]])
            o = np.concatenate([o, [a[1] for a in add]]); h = np.concatenate([h, [a[2] for a in add]])
            l = np.concatenate([l, [a[3] for a in add]]); cc = np.concatenate([cc, [a[4] for a in add]]); v = np.concatenate([v, [a[5] for a in add]])
            order = np.argsort(idx, kind="stable")
            idx, o, h, l, cc, v = idx[order], o[order], h[order], l[order], cc[order], v[order]
        if idx.size:
            bars[s] = (idx, o, h, l, cc, v)
    shares = np.asarray(pan.arrays["shares"][lo:p + 1]).copy()
    fac = np.asarray(pan.arrays["fac"][lo:p + 1], dtype=float)
    fl = np.asarray(pan.arrays["fl"][lo:p + 1])
    arrays = ls.build_panel_arrays(bars, sess, shares.T, syms, fl=fl.T, fac=fac.T)
    return ls.DailyPanel(syms, sess, arrays, pan.manifest), patched


def main(label, iids=None):
    root = os.path.join(HERE, label)
    store, _ = load_store(root)
    fb = [b for (u, _), bs in store.items() if u.endswith("/shares_float/all") for b in bs]
    float_table = parse_float_table(json.loads(fb[0])) if fb else {}
    insts = sorted(int(os.path.basename(d)[4:]) for d in glob.glob(os.path.join(root, "inst*")))
    first = json.load(open(os.path.join(root, f"inst{insts[0]}", "result.json")))
    today = first["started_utc"][:10]
    bars_all = recorded_bars(store, today)
    own, patched = build_own(today, bars_all)
    print(f"own panel rebuilt for {today}: {len(own.symbols)} symbols, {patched} bars patched from the capture", flush=True)
    out = {}
    for iid in (iids or insts):
        cap = json.load(open(os.path.join(root, f"inst{iid}", "result.json")))
        stg = json.load(open(os.path.join(root, f"inst{iid}", "stages.json")))
        if cap["error"]:
            continue
        s1 = stage(stg, "provider.screen_stocks")
        quotes = {}
        for st in [x for x in stg if x["stage"] == "_fetch_quotes_chunked"]:
            quotes.update(st["out"])
        res_set = {k[len("screener_"):]: v for k, v in cap["resolved_settings"].items() if k.startswith("screener_") and k != "screener_provider"}
        res_set.pop("universe_mode", None)
        live = cap["picks"]
        vend = {c["symbol"].upper(): c for c in s1["out"]}
        S = len(own.symbols)
        pos = own.pos(today)
        qprice = np.full(S, np.nan)
        for s, q in quotes.items():
            if s in own.sym_index and q.get("price"):
                qprice[own.sym_index[s]] = q["price"]
        # the quote table only covers stage-1 survivors: candidates outside it have no recorded quote -> not candidates in V0 either
        now_q = lambda idx: (qprice[idx], qprice[idx])
        lc = np.asarray(own.arrays["lc"][pos])
        now_pc = lambda idx: (lc[idx], lc[idx])
        sets = {}
        sets["V0"] = own.select(today, res_set, ls.POST_FIX, now=now_q, band_at_now=True)
        # V1: vendor share counts
        sh_v = np.full(S, np.nan)
        for s, v in vend.items():
            if s in own.sym_index and v.get("market_cap") and v.get("price"):
                sh_v[own.sym_index[s]] = v["market_cap"] / v["price"]
        saved = own.arrays["shares"]
        sh_mat = np.array(saved, copy=True)
        fac_row = np.asarray(own.arrays["fac"][pos], dtype=float)
        sh_mat[pos] = np.where(np.isfinite(sh_v), sh_v / np.where(fac_row > 0, fac_row, 1.0), sh_mat[pos])
        own.arrays["shares"] = sh_mat
        sets["V1"] = own.select(today, res_set, ls.POST_FIX, now=now_q, band_at_now=True)
        # V2: universe = the vendor's stage-1 list, band off, rank by the QUOTE cap
        valid = np.array([str(s) in vend for s in own.symbols])
        res2 = dict(res_set, market_cap_min=0, market_cap_max=0)
        qcap = np.full(S, np.nan)
        for s, q in quotes.items():
            if s in own.sym_index and q.get("marketCap"):
                qcap[own.sym_index[s]] = q["marketCap"]
        sh2 = np.array(sh_mat, copy=True)
        sh2[pos] = np.where(np.isfinite(qcap) & (lc > 0), qcap / np.where(lc > 0, lc, 1.0) / np.where(fac_row > 0, fac_row, 1.0), sh2[pos])
        own.arrays["shares"] = sh2
        sets["V2"] = own.select(today, res2, ls.POST_FIX, now=now_q, valid=valid)
        own.arrays["shares"] = saved
        sets["W_prev_close_now"] = own.select(today, res_set, ls.POST_FIX, now=now_pc, band_at_now=True)
        # classify live <-> V0 differences by the first variant that fixes them
        rows = []
        for side, a, b in (("live-only", set(live) - set(sets["V0"]), None), ("sim-only", set(sets["V0"]) - set(live), None)):
            for s in sorted(a):
                if s not in own.sym_index:
                    cls = "outside the finite universe (not in the store/panel)"
                elif side == "live-only":
                    cls = ("share count" if s in sets["V1"] else "cap basis / universe / band edge" if s in sets["V2"] else
                           "daily bars / forming bar / rule (not fixed by V2)")
                else:
                    cls = ("share count" if s not in sets["V1"] else "cap basis / universe / band edge" if s not in sets["V2"] else
                           "daily bars / forming bar / rule (not fixed by V2)")
                i = own.sym_index.get(s)
                v = vend.get(s)
                # do the cached bars still equal the vendor's CURRENT bars for this symbol (restatement / split-basis drift)?
                rb = bars_all.get(s, {})
                bd = None
                if i is not None and rb:
                    cc = np.asarray(own.arrays["c"][:, i])
                    vv = np.asarray(own.arrays["v"][:, i])
                    ds = [d for d in sorted(rb) if d in own.sessions and d < today and np.isfinite(cc[own.sessions.index(d)])][-60:]
                    rel = [abs(cc[own.sessions.index(d)] / rb[d][3] - 1) for d in ds] +                           [abs(vv[own.sessions.index(d)] / rb[d][4] - 1) for d in ds if rb[d][4]]
                    bd = float(max(rel)) if rel else None
                if bd is not None and bd > 0.005:
                    cls = f"CACHE BARS DIFFER from the vendor's current bars (max close/volume diff {bd:.1%}: restatement / spin-off or split basis); " + cls
                rows.append({"side": side, "symbol": s, "class": cls,
                             "own_cap": float(lc[i] * own.arrays["shares"][pos, i] * own.arrays["fac"][pos, i]) if i is not None else None,
                             "vendor_cap": v["market_cap"] if v else None, "vendor_price": v["price"] if v else None,
                             "quote": qprice[i] if i is not None else None, "max_close_diff_vs_vendor_bars": bd})
        jac = lambda x, y: len(set(x) & set(y)) / max(1, len(set(x) | set(y)))
        # INDEPENDENT cap-basis check: the vendor stage-1 cap vs previous close x the PANEL's vendor-history shares x fac, and vs the
        # quote price x the same shares (the panel's shares come from the vendor's dated series, not from this cap)
        rows_c = []
        for sy, v in vend.items():
            i = own.sym_index.get(sy)
            if i is None or not v.get("market_cap") or not np.isfinite(qprice[i]) or not (lc[i] > 0):
                continue
            shr = float(own.arrays["shares"][pos, i]) * float(own.arrays["fac"][pos, i])
            if not shr > 0:
                continue
            rows_c.append((v["market_cap"], lc[i] * shr, qprice[i] * shr))
        if rows_c:
            a_, b_, c_ = (np.array(x) for x in zip(*rows_c))
            eb, ec = np.abs(b_ / a_ - 1), np.abs(c_ / a_ - 1)
            cap_basis = {"n": len(rows_c), "median_err_prev_close_x_panel_shares": float(np.median(eb)),
                         "median_err_quote_x_panel_shares": float(np.median(ec)),
                         "share_closer_to_quote": float((ec < eb).mean()), "share_within_0.5pct_of_quote_basis": float((ec < .005).mean()),
                         "share_within_0.5pct_of_prev_close_basis": float((eb < .005).mean())}
        else:
            cap_basis = None
        out[iid] = {"live": live, "cap_basis_independent": cap_basis, "V0": sets["V0"], "V1": sets["V1"], "V2": sets["V2"], "W": sets["W_prev_close_now"], "diff": rows,
                    "J": {k: round(jac(live, v), 3) for k, v in sets.items()}}
        print(f"inst {iid}: independent cap basis: {cap_basis}", flush=True)
        cnt = defaultdict(int)
        for r in rows:
            cnt[(r["side"], r["class"])] += 1
        print(f"inst {iid}: live {len(live)} | J own-inputs(V0) {out[iid]['J']['V0']} V1 {out[iid]['J']['V1']} V2 {out[iid]['J']['V2']} | strict prev-close now {out[iid]['J']['W_prev_close_now']} | differences {dict(cnt)}", flush=True)
    json.dump(out, open(os.path.join(root, "sim_own.json"), "w"), default=str, indent=1)


if __name__ == "__main__":
    main(sys.argv[1], [int(x) for x in sys.argv[2:]] or None)
