"""Read-only comparison of the simulation (``live_sim``) with what the LIVE screener really returned.

Sources of live output, in order of fidelity:
  1. ``StockScreener LIVE SELECTION: N symbols [cap>=.. rvol>=.. stage2only=.. drop>=P/Dd max=M] -> A,B,C`` log lines
     (the screener's own final list; the thresholds identify the instance together with its DB settings);
  2. ``StockScreener LIVE STAGES: provider=N -> relative_volume=N -> .. -> final=N | ..`` (branch
     ``feat/screener-stage-log``): per-stage survivor COUNTS, compared with the simulation's counts when present;
  3. the prod DB ``marketanalysis`` rows (ENTER_MARKET, one per analysed symbol and day): the symbols the screener's
     picks reached AFTER the broker-tradability and open-position filters (a subset of 1).

Nothing here writes anywhere.  ``tools/screener_parity_report.py`` is the command line.
"""
from __future__ import annotations

import glob
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from ba2_providers.screener import live_sim as ls

_SEL_RE = re.compile(
    r"StockScreener LIVE SELECTION: (?P<n>\d+) symbols \[cap>=(?P<cap>[\d.e+]+) rvol>=(?P<rvol>[\d.e+-]+) "
    r"stage2only=(?P<w>\S+) drop>=(?P<dp>[\d.e+-]+)/(?P<dd>\d+)d max=(?P<max>\d+)\] -> (?P<syms>.*)$")
_STAGES_RE = re.compile(r"StockScreener LIVE STAGES: (?P<stages>.*?) \| FMP history")


def parse_selection_line(line: str) -> Optional[Dict[str, Any]]:
    m = _SEL_RE.search(line)
    if not m:
        return None
    syms = [s for s in m.group("syms").strip().split(",") if s and s != "?"]
    return {"ts": line[:19], "day": line[:10], "n": int(m.group("n")), "cap_min": float(m.group("cap")),
            "rvol_min": float(m.group("rvol")), "weinstein": m.group("w") in ("1", "True", "true"),
            "drop_pct": float(m.group("dp")), "drop_days": int(m.group("dd")), "max_stocks": int(m.group("max")),
            "symbols": syms}


def parse_stages_line(line: str) -> Optional[Dict[str, Any]]:
    m = _STAGES_RE.search(line)
    if not m:
        return None
    stages = {}
    for part in m.group("stages").split(" -> "):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                stages[k.strip()] = int(v)
            except ValueError:
                pass
    return {"ts": line[:19], "day": line[:10], "stages": stages}


def read_log_records(patterns: Iterable[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    sel: List[Dict[str, Any]] = []
    stg: List[Dict[str, Any]] = []
    for pat in patterns:
        for f in sorted(glob.glob(pat)):
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if "StockScreener LIVE" not in line:
                        continue
                    r = parse_selection_line(line)
                    if r:
                        sel.append(r)
                        continue
                    r = parse_stages_line(line)
                    if r:
                        stg.append(r)
    return sel, stg


def instance_settings(db_path: str, instance_id: int) -> Dict[str, Any]:
    """The instance's screener settings resolved EXACTLY as live resolves them (``StockScreener`` defaults and
    coercion), as the unprefixed dict the simulation takes."""
    from ba2_providers.StockScreener import StockScreener
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        raw: Dict[str, Any] = {}
        for key, vs, vj, vf in con.execute(
                "select key, value_str, value_json, value_float from expertsetting "
                "where instance_id=? and key like 'screener_%'", (instance_id,)):
            if vf is not None:
                raw[key] = vf
            elif vj not in (None, "{}", ""):
                v = json.loads(vj)
                raw[key] = (1 if str(v).lower() == "true" else 0) if isinstance(v, (bool, str)) else v
            elif vs is not None:
                raw[key] = vs
    finally:
        con.close()
    resolved = StockScreener(raw)._settings
    out = {k[len("screener_"):]: v for k, v in resolved.items() if k.startswith("screener_")}
    out.pop("provider", None)
    return out


def analysed_symbols(db_path: str, instance_id: int) -> Dict[str, List[str]]:
    """``{day: [symbols]}`` of the ENTER_MARKET analyses of the instance (the day = the row's UTC date)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out: Dict[str, List[str]] = defaultdict(list)
        for sym, ts in con.execute("select symbol, created_at from marketanalysis "
                                   "where expert_instance_id=? and subtype='ENTER_MARKET'", (instance_id,)):
            out[str(ts)[:10]].append(sym)
        return dict(out)
    finally:
        con.close()


def match_records(records: Sequence[Dict[str, Any]], settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The LIVE SELECTION records produced under ``settings`` (the log line carries cap_min, rvol_min, stage-2
    flag, drop pct / days, max_stocks), the last record of each day winning."""
    by_day: Dict[str, Dict[str, Any]] = {}
    for r in records:
        if (abs(r["cap_min"] - float(settings["market_cap_min"])) < 1 and
                abs(r["rvol_min"] - float(settings["relative_volume_min"])) < 1e-9 and
                abs(r["drop_pct"] - float(settings["price_drop_pct"])) < 1e-9 and
                r["drop_days"] == int(settings["price_drop_days"]) and r["max_stocks"] == int(settings["max_stocks"]) and
                r["weinstein"] == bool(float(settings["weinstein_stage2_only"]))):
            by_day[r["day"]] = r
    return [by_day[d] for d in sorted(by_day)]


def open_now(panel: ls.DailyPanel, day: str):
    """Validation 'now' = the session's OPENING print (daily bar open), the same value the backtest's first-bar
    decision reads from the intraday bars."""
    o = np.asarray(panel.arrays["o"][panel.pos(day)])

    def _now(idx):
        return o[idx], o[idx]
    return _now


def simulate_day(panel: ls.DailyPanel, day: str, settings: Dict[str, Any], beh: ls.LiveBehaviour, *,
                 cut: bool = True, now=None, diag: Optional[Dict[str, int]] = None) -> List[str]:
    vol = np.asarray(panel.arrays["v"][panel.pos(day)])
    return panel.select(day, settings, beh, now=now or open_now(panel, day),
                        vol_today=lambda idx: vol[idx], cut=cut, diag=diag)


def explain_symbol(panel: ls.DailyPanel, day: str, sym: str, settings: Dict[str, Any], beh: ls.LiveBehaviour,
                   sim_all: Sequence[str], sim_cut: Sequence[str]) -> Dict[str, Any]:
    """Why ``sym`` is (not) in the simulated list on ``day``: the criterion values against their thresholds and
    a class for the residual (``edge`` = within 2 % of price / 2 % of the threshold, ``cut`` = passes every
    filter but ranks beyond max_stocks, ``band`` = outside the cap band, ``rvol``, ``weinstein``, ``unknown``)."""
    if sym not in panel.sym_index:
        return {"symbol": sym, "class": "outside_universe"}
    i, p = panel.sym_index[sym], panel.pos(day)
    a = panel.arrays
    lc = float(a["lc"][p, i]); sh = float(a["shares"][p, i]); mcap = lc * sh
    rvol = float(a["rvol"][p, i]); o = float(a["o"][p, i])
    n = int(settings["price_drop_days"])
    pk = float(panel.peak_by_day(n)[p, i]) if float(settings["price_drop_pct"]) > 0 else float("nan")
    if ls.FORMING_BAR_PRESENT and np.isfinite(o):
        pk = float(np.fmax(pk, o))                      # the forming bar's high is part of live's peak
    drop = round((pk - o) / pk * 100, 2) if pk and pk > 0 and np.isfinite(pk) and np.isfinite(o) else float("nan")
    if ls.FORMING_BAR_PRESENT:
        w2 = bool(ls.weinstein_forming_pass(np.asarray(a["wa"][p, i:i + 1]), np.asarray(a["wp"][p, i:i + 1]),
                                            np.array([o]))[0])
    else:
        w2 = bool(a["w2"][p, i])
    d = {"symbol": sym, "mcap": mcap, "rvol": rvol, "drop": drop, "weinstein": w2,
         "in_sim_picks": sym in sim_cut, "passes_filters": sym in sim_all}
    cmin, cmax = float(settings["market_cap_min"]), float(settings["market_cap_max"])
    rmin, dpct = float(settings["relative_volume_min"]), float(settings["price_drop_pct"])
    cls = "unknown"
    if not np.isfinite(mcap):
        cls = "no_data"
    elif (cmin > 0 and mcap < cmin) or (cmax > 0 and mcap > cmax):
        edge = min(abs(mcap - cmin) / cmin if cmin > 0 else 9, abs(mcap - cmax) / cmax if cmax > 0 else 9)
        cls = "band_edge" if edge < 0.03 else "band"
        d["band_margin_pct"] = round(edge * 100, 2)
    elif rmin > 0 and not rvol >= rmin:
        cls = "rvol_edge" if abs(rvol - rmin) <= 0.05 * max(rmin, 1) else "rvol"
    elif float(settings["weinstein_stage2_only"]) > 0 and not d["weinstein"]:
        cls = "weinstein"
    elif dpct > 0 and np.isfinite(drop) and not drop >= dpct:
        d["drop_margin_pct_of_price"] = round(dpct - drop, 2)
        cls = "drop_edge" if dpct - drop <= 2.0 else "drop"
    elif sym in sim_all and sym not in sim_cut:
        cls = "cut"
    elif sym in sim_cut:
        if dpct > 0 and np.isfinite(drop):
            d["drop_margin_pct_of_price"] = round(drop - dpct, 2)
        cls = "sim_pick_edge" if (dpct > 0 and np.isfinite(drop) and drop - dpct <= 2.0) else "sim_pick"
    d["class"] = cls
    return d


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    a, b = set(a), set(b)
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def compare(panel: ls.DailyPanel, settings: Dict[str, Any], records: Sequence[Dict[str, Any]],
            beh: ls.LiveBehaviour) -> Dict[str, Any]:
    """Per-day comparison of the simulation with the LIVE SELECTION records (``records`` already matched to
    the instance).  Returns ``{"days": [...], "summary": {...}}``; every residual is explained."""
    days = []
    for r in records:
        day = r["day"]
        try:
            panel.pos(day)
        except ls.SimulationRefusal:
            days.append({"day": day, "skipped": "day outside the panel"})
            continue
        diag: Dict[str, int] = {}
        sim_cut = simulate_day(panel, day, settings, beh, diag=diag)
        sim_all = simulate_day(panel, day, settings, beh, cut=False)
        live = r["symbols"]
        live_only = sorted(set(live) - set(sim_cut))
        sim_only = sorted(set(sim_cut) - set(live))
        days.append({
            "day": day, "live": len(live), "sim": len(sim_cut), "common": len(set(live) & set(sim_cut)),
            "jaccard": round(jaccard(live, sim_cut), 4), "sim_stages": dict(diag),
            "live_only": [explain_symbol(panel, day, s, settings, beh, sim_all, sim_cut) for s in live_only],
            "sim_only": [explain_symbol(panel, day, s, settings, beh, sim_all, sim_cut) for s in sim_only],
        })
    ok = [d for d in days if "jaccard" in d]
    classes: Dict[str, int] = defaultdict(int)
    for d in ok:
        for e in d["live_only"] + d["sim_only"]:
            classes[e["class"]] += 1
    tot_live = sum(d["live"] for d in ok); tot_common = sum(d["common"] for d in ok)
    summary = {"days": len(ok), "live_picks": tot_live, "reproduced": tot_common,
               "mean_jaccard": round(float(np.mean([d["jaccard"] for d in ok])), 4) if ok else None,
               "residual_classes": dict(classes)}
    return {"days": days, "summary": summary}
