#!/usr/bin/env python
"""Live status of distributed GA workers + running optimization progress.

Probes each worker in the test DB's ``workers`` table via its ``/health`` endpoint
(live — ignores the stale DB ``status`` column), then prints every running/pending
``strategy_optimizations`` row with progress, best fitness, individuals done, the
workers it's distributed to, and a rate/ETA derived from pop*gen and elapsed time.

Usage (test venv):
    C:/Users/basti/ba2-venvs/test/Scripts/python.exe test_files/workers_status.py
    ...                                                test_files/workers_status.py --watch 30
    ...                                                test_files/workers_status.py --db <path>

--watch N refreshes every N seconds (Ctrl-C to stop). --all also lists recently
completed scr-* (cap-band matrix) jobs.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

DEFAULT_DB = os.environ.get("DB_FILE") or r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"


def _probe(url: str, password: str, timeout: float = 5.0) -> dict:
    """Live /health probe. Returns {ok, capacity, app_version, error}."""
    if httpx is None:
        return {"ok": False, "error": "httpx not available"}
    try:
        r = httpx.get(f"{url.rstrip('/')}/health",
                      headers={"Authorization": f"Bearer {password or ''}"}, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        return {"ok": bool(d.get("ok")), "capacity": d.get("capacity"),
                "app_version": (d.get("version") or {}).get("app_version"), "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _parse_dt(s):
    # strategy_optimizations.started_at is written with datetime.now() -> naive LOCAL time,
    # so parse naive and compare against a naive local now (do NOT tag as UTC).
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _fmt_dur(secs: float) -> str:
    if secs is None or secs < 0:
        return "?"
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _total_individuals(opt_config: str) -> int | None:
    """pop * gen from optimization_config (keys vary: populationSize/generations)."""
    if not opt_config:
        return None
    try:
        c = json.loads(opt_config)
    except (ValueError, TypeError):
        return None
    pop = c.get("populationSize") or c.get("population") or c.get("pop_size")
    gen = c.get("generations") or c.get("numGenerations") or c.get("gen")
    if pop and gen:
        return int(pop) * int(gen)
    return None


def render(db: str, show_all: bool) -> str:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    out = []
    now = datetime.now()  # naive local — matches started_at written via datetime.now()

    # --- workers (live health) ---
    out.append("WORKERS")
    workers = cur.execute("select id,name,url,password,is_local from workers order by id").fetchall()
    wname = {w["id"]: w["name"] for w in workers}
    for w in workers:
        if w["is_local"]:
            out.append(f"  [{w['id']}] {w['name']:<14} online (this host)")
        else:
            h = _probe(w["url"], w["password"])
            if h["ok"]:
                out.append(f"  [{w['id']}] {w['name']:<14} ONLINE  cap={h['capacity']}  "
                           f"v{h['app_version']}  {w['url']}")
            else:
                out.append(f"  [{w['id']}] {w['name']:<14} OFFLINE  {w['url']}  ({h['error']})")

    # --- running / pending jobs ---
    out.append("")
    out.append("RUNNING JOBS")
    rows = cur.execute(
        "select id,name,status,best_fitness,progress,all_results,worker_ids,"
        "optimization_config,started_at from strategy_optimizations "
        "where status in ('running','pending') order by id"
    ).fetchall()
    if not rows:
        out.append("  (none running)")
    for r in rows:
        try:
            nind = len(json.loads(r["all_results"])) if r["all_results"] else 0
        except (ValueError, TypeError):
            nind = 0
        total = _total_individuals(r["optimization_config"])
        prog = r["progress"] or 0.0
        # map worker_ids -> names
        wids = r["worker_ids"]
        try:
            widlist = json.loads(wids) if wids else []
        except (ValueError, TypeError):
            widlist = []
        # worker_ids records the REMOTE workers selected; local consumers always run too.
        wlabel = (", ".join(f"{wname.get(i, i)}" for i in widlist) + " + local") if widlist else "local-only"
        started = _parse_dt(r["started_at"])
        elapsed = (now - started).total_seconds() if started else None
        rate = (nind / (elapsed / 60.0)) if (elapsed and nind) else None  # ind/min
        eta = None
        if rate and total and total > nind:
            eta = (total - nind) / rate * 60.0  # secs
        bf = r["best_fitness"]
        bf_s = f"{bf:.2f}" if isinstance(bf, (int, float)) else str(bf)
        tot_s = f"/{total}" if total else ""
        line = (f"  #{r['id']} {r['name']:<28} [{r['status']}]  "
                f"{nind}{tot_s} ind ({prog:.0f}%)  best={bf_s}  workers=[{wlabel}]")
        out.append(line)
        meta = f"       elapsed {_fmt_dur(elapsed)}"
        if rate:
            meta += f"  rate {rate:.2f} ind/min"
        if eta:
            meta += f"  ETA ~{_fmt_dur(eta)}"
        if started:
            out.append(meta)

    # --- cap-band matrix summary (optional) ---
    scr_done = cur.execute(
        "select count(*) from strategy_optimizations where name like 'scr-%' and status='completed'"
    ).fetchone()[0]
    if scr_done or show_all:
        out.append("")
        out.append(f"CAP-BAND MATRIX (scr-*): {scr_done} completed")
        if show_all:
            for r in cur.execute(
                "select name,best_fitness from strategy_optimizations "
                "where name like 'scr-%' and status='completed' order by id"
            ).fetchall():
                bf = r["best_fitness"]
                out.append(f"    {r['name']:<30} best={bf:.2f}" if isinstance(bf, (int, float))
                           else f"    {r['name']:<30} best={bf}")
    con.close()
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Distributed worker + optimization progress.")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"Test DB path (default: $DB_FILE or {DEFAULT_DB})")
    ap.add_argument("--watch", type=int, default=0, metavar="N",
                    help="Refresh every N seconds (Ctrl-C to stop).")
    ap.add_argument("--all", action="store_true", help="Also list completed cap-band jobs.")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 2
    if args.watch:
        try:
            while True:
                os.system("cls" if os.name == "nt" else "clear")
                print(f"=== workers_status @ {datetime.now():%H:%M:%S} (every {args.watch}s) ===")
                print(render(args.db, args.all), flush=True)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    print(render(args.db, args.all))
    return 0


if __name__ == "__main__":
    sys.exit(main())
