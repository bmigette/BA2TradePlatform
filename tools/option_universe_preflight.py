#!/usr/bin/env python
"""Preflight an OPTION universe before a grid: one report file, cache-only, no network.

The checks the stage-2 data repair (2026-09-25/26) ran as scratch scripts, committed:

  coverage  ThetaData tree per symbol: partitions, unfinished ones (no manifest), partitions a
            ROOT_HISTORY entry makes stale (``thetadata.root_history_refetch_reason``), the
            first/last option bar inside the window; the FMP daily file's first/last bar.
  split     the run's split basis (``option_split_basis._resolve_one``: the cached split
            calendar, the full-fetch marker, the shipped overrides) -- OK or REFUSED.
  guard     the REAL option basis guard (``option_basis_guard.BasisGuard``) replayed over the
            window: for each sampled start and each of N start phases, a FRESH guard is handed
            every FMP session from that start (the worst case: a chain read every session) with
            the run's as-traded spot, split dates and dividend source. A refusal is what would
            abort a job started there.
  ds        DeterministicScorer inputs: the fmp_history file of every namespace its prewarm
            warms (a MISSING file is a hermetic hard miss; ``fmp_common._HERMETIC_MISS_LIMIT``
            distinct misses abort a run) and the share of sample dates each section can score.
  gate      the screener metric store (the market-cap gate): per symbol, the weekly rows inside
            the window and how many lack a market cap.

    python tools/option_universe_preflight.py --universe-file U --start 2020-01-01 \
        --end 2025-12-31 --out preflight.json [--sections coverage,split,guard,ds,gate] \
        [--guard-starts 2020-01-01,2023-01-01 --guard-phases 5] [--procs 2]

FINDINGS carry a severity: ``blocking`` (a job would abort or read wrong data: no option tree,
unfinished or root-history-stale partitions, a refused split basis, a guard refusal, DS misses
at or over the abort limit) or ``review`` (worth a look, not fatal: no option bar in the
window, a first option bar well after the stock's own history starts, a missing DS file, a
symbol absent from the gate store). Exit codes:
0 no blocking finding, 1 at least one, 2 a configuration error. The full report goes to
``--out``; a summary (counts + blocking findings) is printed.

Run it with the test platform's interpreter (it imports the backtest guard); memory is bounded
by ``--procs`` worker processes, each holding one symbol's option arrays at a time.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_REPO, os.path.join(_REPO, "testplatform", "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

EXIT_OK, EXIT_BLOCKING, EXIT_CONFIG = 0, 1, 2
SECTIONS = ("coverage", "split", "guard", "ds", "gate")
#: The fmp_history namespaces the DeterministicScorer prewarm warms (``ba2-test prewarm
#: --experts DeterministicScorer``; pinned against ``PrewarmFetchers`` by the tests).
DS_NAMESPACES = ("income_statement_annual", "balance_sheet_annual", "cashflow_statement_annual",
                 "grades_historical", "past_earnings_quarterly", "price_target")
#: A first option bar this long after BOTH the window start and the stock's own first FMP bar is
#: worth a look: a vendor gap (the early-2020 one) or a chain listed under another ticker before
#: (a missing ROOT_HISTORY alias). A recent listing whose options follow within this is not.
LATE_FIRST_BAR_DAYS = 30


def _universe(path):
    with open(path, "r", encoding="utf-8") as f:
        syms = [line.split("#", 1)[0].strip().upper() for line in f]
    return list(dict.fromkeys(s for s in syms if s))


def _date(text):
    try:
        return date.fromisoformat(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r} is not an ISO date: {e}")


def _cache_root():
    from ba2_common import config
    return config.CACHE_FOLDER


# ------------------------------------------------------------------------------ per symbol
def _fmp_daily(cache_root, sym):
    """(sorted session dates, closes) of the FMP daily file, or None."""
    import pandas as pd
    path = os.path.join(cache_root, "FMPOHLCVProvider", f"{sym}_1d.parquet")
    if not os.path.exists(path):
        return None, path
    df = pd.read_parquet(path, columns=["Date", "Close"])
    d = pd.to_datetime(df["Date"])
    d = d.dt.tz_localize(None) if getattr(d.dt, "tz", None) is not None else d
    days = list(d.dt.date)
    closes = df["Close"].astype(float).tolist()
    order = sorted(range(len(days)), key=lambda i: days[i])
    return ([days[i] for i in order], [closes[i] for i in order]), path


def _coverage(sym, cfg, fmp):
    from ba2_providers.options import thetadata as T
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore, PartitionState

    start, end = cfg["start"], cfg["end"]
    out = {"fmp_first": None, "fmp_last": None}
    if fmp is not None:
        days = fmp[0]
        out["fmp_first"] = days[0].isoformat() if days else None
        out["fmp_last"] = days[-1].isoformat() if days else None
    tree = os.path.join(cfg["theta_root"], sym)
    if not os.path.isdir(tree):
        out["theta"] = None
        return out
    store = OptionHistoryParquetStore(root=cfg["theta_root"], **T.parquet_store_hooks())
    exps = sorted(date.fromisoformat(x[4:]) for x in os.listdir(tree) if x.startswith("exp="))
    in_window = [e for e in exps if start <= e]
    unfinished, stale, empty = [], [], 0
    for e in in_window:
        m = store.read_manifest(sym, e)
        if m is None:
            unfinished.append(e.isoformat())
            continue
        store.partition_state(sym, e, start, e)
        if (sym, e) in store.stale_reasons:
            stale.append(e.isoformat())
        if m.get("status") == "empty":
            empty += 1
    first = last = None
    # The first bar: the partitions expiring early in the window hold it; only when none of them
    # has a bar (a late listing, a vendor gap) are all of them read.
    early = [x for x in in_window if x <= start + timedelta(days=180)]
    for batch in (early, [x for x in in_window if x not in set(early)]):
        for e in batch:
            fb = store.first_bar_date(sym, e)
            if fb is not None and fb <= end:
                fb = max(fb, start)
                first = fb if first is None else min(first, fb)
        if first is not None:
            break
    out.update(theta=True, partitions=len(exps), partitions_in_window=len(in_window),
               unfinished=unfinished, root_history_stale=stale,
               root_history_stale_reason=(store.stale_reasons.get((sym, date.fromisoformat(stale[0])))
                                          if stale else None),
               empty_partitions=empty, first_bar=first.isoformat() if first else None)
    # The last bar: the partitions expiring around the window end.
    import pyarrow.parquet as pq
    for e in [x for x in in_window if end - timedelta(days=45) <= x <= end + timedelta(days=90)]:
        p = store.bars_path(sym, e)
        if os.path.exists(p):
            col = pq.read_table(p, columns=["bar_date"]).column("bar_date").to_pylist()
            ds = [date.fromisoformat(str(v)[:10]) for v in col]
            ds = [d for d in ds if d <= end]
            if ds:
                last = max(ds) if last is None else max(last, max(ds))
    out["last_bar"] = last.isoformat() if last else None
    return out


def _split(sym, path):
    from ba2_common.core.split_basis import SplitBasisRefused
    from app.services.backtest.option_split_basis import _resolve_one
    try:
        return {"ok": True, "basis": _resolve_one(sym, path)}
    except SplitBasisRefused as e:
        return {"ok": False, "refused": str(e)[:600]}


def _guard(sym, cfg, fmp, basis):
    from app.services.backtest import option_basis_guard as G
    from app.services.backtest.option_split_basis import RunSplitBasis
    from app.services.backtest.options_store import _dividends_of, _split_dates_of
    from app.services.backtest.parquet_options_provider import _Underlying, _raw_underlying
    import bisect

    days, closes = fmp

    def spot(_s, day):
        # options_store.price_source_spot: the close as of the day x the as-traded factor of
        # the close's OWN date.
        k = bisect.bisect_right(days, day) - 1
        return None if k < 0 else closes[k] * basis.factor(days[k])

    u = _Underlying(_raw_underlying(cfg["theta_root"], sym), 0.0)
    if not u.n_rows:
        return {"skipped": "no option rows"}
    rb = RunSplitBasis({sym: basis})
    sd, dv = _split_dates_of(rb), _dividends_of(rb)
    res = {"refusals": [], "runs": 0, "checks": 0, "carry_passed": 0}
    for st in cfg["guard_starts"]:
        sess = [x for x in days if st <= x <= cfg["end"]]
        for ph in range(cfg["guard_phases"]):
            g = G.BasisGuard(spot, split_dates=sd, dividends=dv)
            res["runs"] += 1
            try:
                for x in sess[ph:]:
                    g.check(u, sym, x)
            except G.OptionSpotBasisMismatch as e:
                res["refusals"].append({"start": st.isoformat(), "phase": ph,
                                        "session": x.isoformat(), "why": str(e)[:400]})
            s = g.stats()
            res["checks"] += int(s.get("checks", 0))
            res["carry_passed"] += int(s.get("carry_passed", 0))
    return res


def scan_symbol(sym, cfg):
    """Coverage + split basis + guard of one symbol (one worker task)."""
    import gc
    import logging
    logging.disable(logging.WARNING)
    t0 = time.time()
    out = {"symbol": sym}
    try:
        fmp, path = _fmp_daily(cfg["cache_root"], sym)
        if "coverage" in cfg["sections"]:
            out["coverage"] = _coverage(sym, cfg, fmp)
        basis = None
        if "split" in cfg["sections"] or "guard" in cfg["sections"]:
            if fmp is None:
                out["split"] = {"ok": False, "refused": f"no FMP daily file at {path}"}
            else:
                r = _split(sym, path)
                basis = r.pop("basis", None)
                out["split"] = r
        if "guard" in cfg["sections"]:
            if basis is None or not os.path.isdir(os.path.join(cfg["theta_root"], sym)):
                out["guard"] = {"skipped": "no split basis" if basis is None else "no option tree"}
            else:
                out["guard"] = _guard(sym, cfg, fmp, basis)
    except MemoryError as e:
        out["error"] = f"MemoryError: {e}"
    except Exception as e:  # noqa: BLE001 -- a per-symbol failure is a finding, not a crash
        out["error"] = f"{type(e).__name__}: {e}"[:600]
    finally:
        if "guard" in cfg["sections"]:
            from app.services.backtest import option_basis_guard as G
            from app.services.backtest import parquet_options_provider as P
            P._WORKER_RAW_CACHE.clear()
            P._WORKER_UNDERLYING_CACHE.clear()
            G.clear_basis_guard_cache()
        gc.collect()
    out["elapsed_s"] = round(time.time() - t0, 2)
    return out


def _run_one(args):
    return scan_symbol(*args)


# ------------------------------------------------------------------------------ whole universe
def _ds_eff(row):
    for k in ("fillingDate", "filingDate", "acceptedDate"):
        v = _as_date(row.get(k))
        if v:
            return v
    v = _as_date(row.get("fiscalDateEnding") or row.get("date"))
    return v + timedelta(days=75) if v else None


def _as_date(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def ds_section(universe, cache_root, sample_dates, bars_of):
    """File presence per DS namespace + per-section availability on ``sample_dates`` (the
    rules of the stage-2 scan: technical >= 260 bars; fundamental = every statement has a row
    filed <= D and <= 450 days old; fund_accel >= 3 income rows; earnings = a reported EPS <= D;
    analyst_grades = a grade in [D-90, D]; analyst_targets >= 3 targets in [D-90, D])."""
    hist = os.path.join(cache_root, "fmp_history")
    per, missing, corrupt = {}, [], []
    avail = {k: 0 for k in ("technical", "fundamental", "fund_accel", "earnings",
                            "analyst_grades", "analyst_targets")}
    for sym in universe:
        rows, files = {}, {}
        for ns in DS_NAMESPACES:
            p = os.path.join(hist, f"{ns}__{sym}.json")
            if not os.path.exists(p):
                files[ns] = "missing"
                missing.append(f"{ns}/{sym}")
                rows[ns] = []
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    x = json.load(f)
            except (OSError, ValueError):
                files[ns] = "corrupt"
                corrupt.append(f"{ns}/{sym}")
                rows[ns] = []
                continue
            if isinstance(x, dict):
                x = next((x[k] for k in ("historical", "data", "rows") if isinstance(x.get(k), list)), [])
            rows[ns] = x if isinstance(x, list) else []
            files[ns] = len(rows[ns])
        stm = {ns: sorted(e for e in (_ds_eff(r) for r in rows[ns]) if e) for ns in DS_NAMESPACES[:3]}
        earn = sorted(d for d in (_as_date(r.get("date")) for r in rows["past_earnings_quarterly"]
                                  if r.get("eps") is not None) if d)
        grades = sorted(d for d in (_as_date(r.get("date")) for r in rows["grades_historical"]) if d)
        targets = sorted(d for d in (_as_date(r.get("publishedDate")) for r in rows["price_target"]) if d)
        bars = bars_of(sym)
        for D in sample_dates:
            lo = D - timedelta(days=90)
            avail["technical"] += sum(1 for b in bars if b <= D) >= 260
            avail["fundamental"] += all(any(e <= D and (D - e).days <= 450 for e in stm[ns])
                                        for ns in DS_NAMESPACES[:3])
            avail["fund_accel"] += sum(1 for e in stm["income_statement_annual"] if e <= D) >= 3
            avail["earnings"] += any(d <= D for d in earn)
            avail["analyst_grades"] += any(lo <= d <= D for d in grades)
            avail["analyst_targets"] += sum(1 for d in targets if lo <= d <= D) >= 3
        per[sym] = files
    n = max(1, len(universe) * len(sample_dates))
    return {"namespaces": list(DS_NAMESPACES), "missing_files": missing, "corrupt_files": corrupt,
            "distinct_misses": len(set(missing)), "per_symbol": per,
            "section_availability": {k: round(v / n, 4) for k, v in avail.items()},
            "sample_dates": [d.isoformat() for d in sample_dates]}


def gate_section(universe, store_dir, start, end):
    """Per symbol: the metric-store rows inside the window and the ones lacking a market cap."""
    import pyarrow.dataset as ds
    import pyarrow.compute as pc
    if not os.path.isdir(store_dir):
        return {"store": store_dir, "error": "no metric store"}
    parts = []
    for name in sorted(os.listdir(store_dir)):
        if not name.startswith("ym="):
            continue
        ym = name[3:]
        if f"{start:%Y-%m}" <= ym <= f"{end:%Y-%m}":
            parts.append(os.path.join(store_dir, name))
    want = set(universe)
    per = {}
    for p in parts:
        t = ds.dataset(p, format="parquet").to_table(columns=["symbol", "date", "market_cap"])
        t = t.filter(pc.is_in(t["symbol"], value_set=__import__("pyarrow").array(sorted(want))))
        for sym, d, mc in zip(t["symbol"].to_pylist(), t["date"].to_pylist(), t["market_cap"].to_pylist()):
            d = d.date() if isinstance(d, datetime) else d if isinstance(d, date) else _as_date(d)
            if d is None or not (start <= d <= end):
                continue
            r = per.setdefault(sym, {"rows": 0, "mcap_missing": 0, "first": d, "last": d})
            r["rows"] += 1
            r["mcap_missing"] += mc is None or mc != mc
            r["first"], r["last"] = min(r["first"], d), max(r["last"], d)
    for r in per.values():
        r["first"], r["last"] = r["first"].isoformat(), r["last"].isoformat()
    return {"store": store_dir, "months": len(parts), "present": len(per),
            "absent": sorted(want - set(per)), "per_symbol": per}


def _findings(rows, ds, gate, cfg):
    out = []

    def add(sev, section, sym, what):
        out.append({"severity": sev, "section": section, "symbol": sym, "what": what})

    for r in rows:
        s = r["symbol"]
        if "error" in r:
            add("blocking", "scan", s, r["error"])
        c = r.get("coverage")
        if c is not None:
            if c.get("theta") is None:
                add("blocking", "coverage", s, "no ThetaData tree")
            else:
                if c["unfinished"]:
                    add("blocking", "coverage", s, f"{len(c['unfinished'])} unfinished partition(s) "
                        f"(no manifest), e.g. {c['unfinished'][0]}: re-run the ThetaData warm")
                if c["root_history_stale"]:
                    add("blocking", "coverage", s, f"{len(c['root_history_stale'])} partition(s) "
                        f"stale under ROOT_HISTORY ({c['root_history_stale_reason']}): re-run the "
                        f"ThetaData warm")
                if c["first_bar"] is None:
                    add("review", "coverage", s, "no option bar in the window")
                else:
                    listed = max(cfg["start"], date.fromisoformat(c["fmp_first"])) if c.get("fmp_first")                         else cfg["start"]
                    if date.fromisoformat(c["first_bar"]) > listed + timedelta(days=LATE_FIRST_BAR_DAYS):
                        add("review", "coverage", s, f"first option bar {c['first_bar']}, stock "
                            f"history from {c.get('fmp_first')}: a vendor gap or an earlier "
                            f"ticker (ROOT_HISTORY)?")
        sp = r.get("split")
        if sp is not None and not sp["ok"]:
            add("blocking", "split", s, sp["refused"])
        g = r.get("guard")
        if g is not None and g.get("refusals"):
            f = g["refusals"][0]
            add("blocking", "guard", s, f"{len(g['refusals'])} of {g['runs']} sampled runs refused, "
                f"first {f['session']} (start {f['start']} phase {f['phase']}): {f['why'][:200]}")
    if ds is not None:
        from ba2_providers.fmp_common import _HERMETIC_MISS_LIMIT
        for m in ds["missing_files"]:
            add("review", "ds", m.split("/", 1)[1], f"missing {m.split('/', 1)[0]}")
        for m in ds["corrupt_files"]:
            add("blocking", "ds", m.split("/", 1)[1], f"unreadable {m.split('/', 1)[0]}")
        if ds["distinct_misses"] >= _HERMETIC_MISS_LIMIT:
            add("blocking", "ds", None, f"{ds['distinct_misses']} distinct missing DS files: a "
                f"hermetic DS run aborts at {_HERMETIC_MISS_LIMIT}. Run the DS prewarm.")
    if gate is not None:
        if gate.get("error"):
            add("blocking", "gate", None, f"{gate['error']} at {gate['store']}")
        for s in gate.get("absent", []):
            add("review", "gate", s, "absent from the screener metric store in the window")
    return out


def run(universe, start, end, sections, *, out_path, guard_starts, guard_phases, procs,
        cache_root=None, theta_root=None, gate_store=None, log=print):
    cache_root = cache_root or _cache_root()
    theta_root = theta_root or os.path.join(cache_root, "ThetaDataOptionsProvider")
    if gate_store is None:
        from ba2_common.config import SCREENER_STORE_DIR
        gate_store = SCREENER_STORE_DIR
    t0 = time.time()
    cfg = {"start": start, "end": end, "sections": list(sections), "cache_root": cache_root,
           "theta_root": theta_root, "guard_starts": list(guard_starts), "guard_phases": guard_phases}
    rows = []
    if {"coverage", "split", "guard"} & set(sections):
        os.environ.setdefault("BT_OPTION_PARQUET_CACHE_MAX", "2")
        log(f"per-symbol scan of {len(universe)} symbol(s), {procs} process(es)")
        if procs <= 1:
            rows = [scan_symbol(s, cfg) for s in universe]
        else:
            with ProcessPoolExecutor(procs) as ex:
                rows = list(ex.map(_run_one, [(s, cfg) for s in universe], chunksize=1))
    ds = None
    if "ds" in sections:
        log("DS inputs")

        def bars_of(sym):
            f, _p = _fmp_daily(cache_root, sym)
            return f[0] if f else []
        samples = [date(y, m, 15) for y in range(start.year, end.year + 1) for m in (1, 4, 7, 10)
                   if start <= date(y, m, 15) <= end]
        ds = ds_section(universe, cache_root, samples, bars_of)
    gate = gate_section(universe, gate_store, start, end) if "gate" in sections else None
    findings = _findings(rows, ds, gate, cfg)
    blocking = [f for f in findings if f["severity"] == "blocking"]
    summary = {
        "symbols": len(universe), "sections": list(sections),
        "window": [start.isoformat(), end.isoformat()],
        "guard_starts": [d.isoformat() for d in guard_starts], "guard_phases": guard_phases,
        "blocking": len(blocking), "review": len(findings) - len(blocking),
        "split_refused": sorted(r["symbol"] for r in rows if r.get("split") and not r["split"]["ok"]),
        "guard_refused": sorted(r["symbol"] for r in rows if (r.get("guard") or {}).get("refusals")),
        "guard_carry_cleared": sorted(r["symbol"] for r in rows
                                      if (r.get("guard") or {}).get("carry_passed")),
        "no_option_tree": sorted(r["symbol"] for r in rows
                                 if r.get("coverage") and r["coverage"].get("theta") is None),
        "ds_distinct_misses": ds["distinct_misses"] if ds else None,
        "gate_absent": gate.get("absent") if gate else None,
        "elapsed_s": round(time.time() - t0, 1),
    }
    report = {"summary": summary, "findings": findings, "symbols": rows, "ds": ds, "gate": gate,
              "created_at": datetime.now().astimezone().isoformat()}
    if out_path:
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1, default=str)
        os.replace(tmp, out_path)
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe-file", required=True)
    ap.add_argument("--start", required=True, type=_date)
    ap.add_argument("--end", required=True, type=_date)
    ap.add_argument("--out", required=True, help="where to write the full JSON report")
    ap.add_argument("--sections", default=",".join(SECTIONS),
                    help=f"comma list of {', '.join(SECTIONS)} (default: all)")
    ap.add_argument("--guard-starts", default=None,
                    help="comma list of ISO start dates to replay the guard from (default: the "
                         "window start and its midpoint)")
    ap.add_argument("--guard-phases", type=int, default=5,
                    help="start offsets (in sessions) replayed per start (default 5)")
    ap.add_argument("--procs", type=int, default=2, help="worker processes (default 2)")
    ap.add_argument("--gate-store", default=None, help="screener metric store dir (default: the platform's)")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_CONFIG if e.code else EXIT_OK
    if not os.path.isfile(args.universe_file):
        print(f"universe file not found: {args.universe_file}", file=sys.stderr)
        return EXIT_CONFIG
    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    bad = [s for s in sections if s not in SECTIONS]
    if bad or args.end < args.start:
        print(f"configuration error: unknown section(s) {bad}" if bad else "end is before start",
              file=sys.stderr)
        return EXIT_CONFIG
    if args.guard_starts:
        starts = [_date(s.strip()) for s in args.guard_starts.split(",") if s.strip()]
    else:
        starts = [args.start, args.start + (args.end - args.start) / 2]
    report = run(_universe(args.universe_file), args.start, args.end, sections, out_path=args.out,
                 guard_starts=starts, guard_phases=args.guard_phases, procs=args.procs,
                 gate_store=args.gate_store,
                 log=lambda m: print(f"[{datetime.now():%H:%M:%S}] {m}", file=sys.stderr))
    print(json.dumps({"summary": report["summary"],
                      "blocking": [f for f in report["findings"] if f["severity"] == "blocking"],
                      "out": args.out}, indent=1, default=str))
    return EXIT_BLOCKING if report["summary"]["blocking"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
