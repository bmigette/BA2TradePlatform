"""Re-run the backtests behind dev's deployed experts on a remote worker; report what moved.

WHY. The classic RM's per-instrument ceiling changed on 2026-09-16 from ``available x ratio``
to ``virtual x ratio`` (finding 4). Every stored classic-RM result was produced under the old
rule, so the numbers the dev deployment was chosen on no longer describe what that genome does
today. This re-measures each deployed source under the corrected engine and prints the delta.

READ-ONLY, deliberately. It writes NO Backtest row, adds NO label, and touches neither the test
DB nor any live DB -- the stored sources stay exactly as they are, because they are the baseline
the new numbers are judged against. The output is a report; acting on it is a separate decision.
(``tools/run_ok1000.py`` is the tool that DOES persist re-runs, as new rows.)

WHY A WORKER. remote227 has 32 cores and 251 GB against this box's live apps, and the trial
itself is 4-12 min at ~2.5-12 GB RSS. The worker holds no optimization DB, so the trial config
is rebuilt HERE -- exactly as ``_persist_top_backtests`` does: fitness-dedup ranking,
``decode_params`` then ``_build_daily_trial_config`` -- and shipped whole to ``/submit-trial-full``.

POLLING IS OURS ON PURPOSE. ``worker_client._submit_and_poll`` cancels a job that reports no
``bars`` for 420 s, and ``_persist_trial_worker`` never reports bars, so every remote re-run
longer than 7 minutes dies as "accepted but never started" (measured on opt 361/362/364/372/
377/379/380/424). The loop below polls ``/job-status`` itself and ignores ``bars`` entirely.

Usage:
    python tools/rerun_dev_deployed_on_worker.py --list
    python tools/rerun_dev_deployed_on_worker.py --only 1088
    python tools/rerun_dev_deployed_on_worker.py --all [--parallel 6] [--worker remote227]
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "testplatform" / "backend"
for p in (str(BACKEND), str(ROOT / "testplatform"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(BACKEND)

from app.models.database import DATABASE_URL as _DB_URL  # noqa: E402
if _DB_URL.startswith("sqlite:///"):
    from ba2_common.core import db as _ba2_db  # noqa: E402
    _ba2_db.configure_db(_DB_URL.replace("sqlite:///", "", 1))

logging.disable(logging.WARNING)

TEST_DB = os.path.expanduser(r"~\Documents\ba2\test\dl_forecasting.db")
DEV_DB = os.path.expanduser(r"~\Documents\ba2\trade\db.sqlite")


# ─── targets ────────────────────────────────────────────────────────

def deployed_targets():
    """Every enabled dev expert's source backtest, with the metrics it has on record.

    The source id comes from the instance ALIAS (``...-bt1182``), which the 2026-09-14 deploy
    wrote for exactly this kind of question. An instance whose alias carries no id is reported
    rather than skipped silently: it would be a deployed strategy this re-run cannot cover.
    """
    dev = sqlite3.connect(f"file:{DEV_DB}?mode=ro", uri=True)
    rows = list(dev.execute("select id, alias, account_id from expertinstance where enabled=1"))
    targets, unmatched = [], []
    test = sqlite3.connect(f"file:{TEST_DB}?mode=ro", uri=True)
    for inst_id, alias, account_id in rows:
        m = re.search(r"bt(\d+)$", alias or "")
        if not m:
            unmatched.append((inst_id, alias))
            continue
        bt_id = int(m.group(1))
        r = test.execute(
            "select b.id, b.name, b.optimization_id, b.total_return, b.annualized_return, "
            "       b.max_drawdown, b.total_trades, o.fitness_metric "
            "from backtests b left join strategy_optimizations o on o.id = b.optimization_id "
            "where b.id = ?", (bt_id,)).fetchone()
        if not r:
            unmatched.append((inst_id, alias))
            continue
        rank = re.match(r"TOP(\d+)-", r[1] or "")
        targets.append({"instance": inst_id, "account": account_id, "alias": alias,
                        "bt": r[0], "name": r[1], "opt": r[2],
                        "rank": int(rank.group(1)) if rank else None,
                        "fitness_metric": r[7] or "consistent_annual_return",
                        "stored": {"total_return": r[3], "annualized_return": r[4],
                                   "max_drawdown": r[5], "total_trades": r[6]}})
    return sorted(targets, key=lambda t: t["bt"]), unmatched


# ─── trial config, rebuilt exactly as the persist phase does ────────

def build_trial_config(opt_id: int, rank: int, label: str) -> dict:
    con = sqlite3.connect(f"file:{TEST_DB}?mode=ro", uri=True)
    cfg_json, all_results, strategy_id = con.execute(
        "select optimization_config, all_results, strategy_id "
        "from strategy_optimizations where id = ?", (opt_id,)).fetchone()
    cfg, res = json.loads(cfg_json), json.loads(all_results)

    # The launcher's own fitness-dedup selection: equal fitness is ONE rank, so TOP3 here is
    # the same genome TOP3 was when the row was written.
    seen, ranked = set(), []
    for r in sorted(res, key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9),
                    reverse=True):
        k = round(r["fitness"], 6)
        if k in seen:
            continue
        seen.add(k)
        ranked.append(r)
        if len(ranked) >= rank:
            break
    if len(ranked) < rank:
        raise RuntimeError(f"opt {opt_id} has no rank {rank} after fitness dedup")
    trial = ranked[rank - 1]

    import app.models  # noqa: F401
    from app.models.database import SessionLocal
    from app.models.strategy import Strategy
    from app.services.strategy_optimization_handler import (_build_daily_trial_config,
                                                            _build_hoisted_state)
    from app.services.strategy_param_space import decode_params

    db = SessionLocal()
    try:
        strat = db.query(Strategy).filter_by(id=strategy_id).first()
        bt_block = dict(cfg["backtest"])
        decoded = decode_params(strat, trial["params"])
        # THE HOISTED SCREENER STATE, exactly as ba2test_launcher._persist_top_backtests does.
        # Every row here is a screener run (scr-*), and the GA scored each individual against a
        # hoisted universe; rebuilding without it does not reproduce the stored backtest -- it
        # fails outright (a float(None) deep in the screener path), which is the honest outcome:
        # a re-run that silently used a different universe would be worse than no number at all.
        # run_genome_once.py passes {"backtest_cfg": bt_block} instead, which is why it is a
        # determinism probe rather than the persist path.
        hoisted = _build_hoisted_state(bt_block) if bt_block.get("screener_opt") else None
        trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted)
    finally:
        db.close()
    trial_cfg["name"] = f"RERUN-{label}"
    return trial_cfg, trial


# ─── worker ─────────────────────────────────────────────────────────

def get_worker(name: str) -> dict:
    con = sqlite3.connect(f"file:{TEST_DB}?mode=ro", uri=True)
    row = con.execute("select id, name, url, password, is_enabled from workers where name = ?",
                      (name,)).fetchone()
    if not row:
        raise SystemExit(f"worker {name!r} not found in the test DB")
    return {"id": row[0], "name": row[1], "url": row[2], "password": row[3]}


def submit_and_poll(worker: dict, config: dict, fitness_metric: str,
                    timeout: float = 5400.0, poll: float = 10.0) -> dict:
    """Submit one full trial and wait for it, IGNORING the bars heartbeat.

    See the module docstring: the shared poller treats "no bars yet" as a stalled pool and
    cancels the job at 420 s, which is fatal for exactly this kind of re-run.
    """
    import httpx
    from ba2_common.config import CACHE_FOLDER
    from app.services.backtest.backtest_db import _inmem_trades_enabled

    base = str(worker["url"]).rstrip("/")
    headers = {"Authorization": f"Bearer {worker.get('password') or ''}"}
    payload = {"config": config, "fitness_metric": fitness_metric,
               "cache_root": CACHE_FOLDER, "inmem_trades": _inmem_trades_enabled()}

    # RETRY THE SUBMIT. A worker that just self-updated is restarting and its pool is not
    # initialized for a few seconds -- it answers 503 "Worker pool not initialized". The same
    # 503 is its admission control refusing work when the box is under the memory floor, which
    # is equally worth waiting out rather than failing the whole re-run.
    job_id = None
    for attempt in range(1, 13):
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{base}/submit-trial-full", headers=headers, json=payload)
        if r.status_code == 503:
            if attempt == 12:
                raise RuntimeError(f"worker refused the submit 12x: {r.text[:200]}")
            time.sleep(10.0)
            continue
        r.raise_for_status()
        job_id = r.json()["job_id"]
        break

    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=30.0) as c:
        while True:
            if time.monotonic() >= deadline:
                try:
                    c.post(f"{base}/cancel-job/{job_id}", headers=headers)
                except Exception:  # noqa: BLE001 -- best effort, the job is already lost to us
                    pass
                raise TimeoutError(f"job {job_id} exceeded {timeout:.0f}s")
            try:
                s = c.get(f"{base}/job-status/{job_id}", headers=headers)
                if s.status_code == 404:
                    raise RuntimeError(f"job {job_id} unknown (worker restarted?)")
                s.raise_for_status()
                body = s.json()
                if body.get("status") == "done":
                    return body["result"]
                if body.get("status") == "failed":
                    raise RuntimeError(f"job {job_id} failed: {str(body.get('error'))[:300]}")
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                # A blip mid-poll is not a dead job; keep waiting inside the budget.
                print(f"    [{job_id[:8]}] poll hiccup: {type(e).__name__}", flush=True)
            time.sleep(poll)


# ─── run ────────────────────────────────────────────────────────────

def run_one(worker: dict, t: dict) -> dict:
    label = f"bt{t['bt']}-opt{t['opt']}-top{t['rank']}"
    t0 = time.perf_counter()
    try:
        cfg, trial = build_trial_config(t["opt"], t["rank"], label)
        out = submit_and_poll(worker, cfg, t["fitness_metric"])
        res = out.get("results") if isinstance(out, dict) and "results" in out else out
        new = {"total_return": res.get("total_return"),
               "annualized_return": res.get("annualized_return"),
               "max_drawdown": res.get("max_drawdown"),
               "total_trades": res.get("total_trades")}
        return {**t, "new": new, "ga_fitness": trial.get("fitness"),
                "elapsed_s": round(time.perf_counter() - t0, 1), "error": None}
    except Exception as e:  # noqa: BLE001 -- one failed re-run must not lose the other 25
        return {**t, "new": None, "elapsed_s": round(time.perf_counter() - t0, 1),
                "error": f"{type(e).__name__}: {e}"}


def _pct(v):
    return "—" if v is None else f"{v:+.2f}"


def report(rows, path: Path):
    lines = ["# Dev-deployed backtests re-run under the corrected per-instrument ceiling", "",
             f"Generated {datetime.now(timezone.utc).isoformat()}", "",
             "The stored numbers were produced when the classic RM's per-instrument ceiling was",
             "`available x ratio`; the new ones use `virtual x ratio` (finding 4, 2026-09-16).",
             "No stored row was modified.", "",
             "| bt | strategy | trades old→new | total% old→new | CAR% old→new | maxDD% old→new |",
             "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["bt"]):
        if r["error"]:
            lines.append(f"| {r['bt']} | {r['name'][:44]} | ERROR | {r['error'][:60]} | | |")
            continue
        s, n = r["stored"], r["new"]
        lines.append(
            f"| {r['bt']} | {r['name'][:44]} | "
            f"{s['total_trades']} → {n['total_trades']} | "
            f"{_pct(s['total_return'])} → {_pct(n['total_return'])} | "
            f"{_pct(s['annualized_return'])} → {_pct(n['annualized_return'])} | "
            f"{_pct(s['max_drawdown'])} → {_pct(n['max_drawdown'])} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worker", default="remote227")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--only", type=int, action="append")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--skip-sync", action="store_true",
                    help="do not force the worker to match TEST_APP_VERSION first")
    args = ap.parse_args()

    targets, unmatched = deployed_targets()
    if unmatched:
        print(f"WARNING: {len(unmatched)} enabled dev expert(s) carry no resolvable source "
              f"backtest and are NOT covered: {unmatched}")
    if args.only:
        targets = [t for t in targets if t["bt"] in set(args.only)]
    if args.list or not (args.all or args.only):
        print(f"{len(targets)} deployed source backtest(s):")
        for t in targets:
            s = t["stored"]
            print(f"  bt{t['bt']:<5} opt{t['opt']:<4} TOP{t['rank']}  trades={s['total_trades']:<5}"
                  f" total={_pct(s['total_return'])}%  CAR={_pct(s['annualized_return'])}%"
                  f"  dd={_pct(s['max_drawdown'])}%  {t['name'][:46]}")
        if not (args.all or args.only):
            print("\nNothing run. Pass --all (or --only <bt_id>).")
            return 0

    worker = get_worker(args.worker)
    if not args.skip_sync:
        from testplatform.version import TEST_APP_VERSION
        from app.services import worker_client
        print(f"syncing {worker['name']} to TEST_APP_VERSION {TEST_APP_VERSION} ...", flush=True)
        if not worker_client.ensure_synced(worker, TEST_APP_VERSION, log=lambda m: print("   ", m)):
            raise SystemExit(f"worker {worker['name']} could not be synced; refusing to run "
                             f"(its results would come from the OLD ceiling)")
        print("   worker is on the master's version", flush=True)

        # SECRETS, the other half of the GA's pre-flight (distributed_eval._preflight_worker).
        # A worker without the master's FMP key builds no OHLCV provider at all and every trial
        # dies -- as "FMP API key not configured" when you run it by hand, and as a bare
        # PermissionError(13) through the pool, because the child then falls back to a DB file
        # it may not own. Cache push is deliberately NOT done here: this worker already carries
        # the 73 GB cache these exact genomes were optimized against, and a hermetic miss fails
        # loudly rather than silently substituting data.
        from ba2_common.config import get_app_setting
        secrets = {k: v for k in ("FMP_API_KEY", "finnhub_api_key")
                   if (v := get_app_setting(k))}
        if not secrets:
            raise SystemExit("no master credentials resolved; the worker would fail every trial")
        worker_client.push_secrets(worker, secrets, log=lambda m: print("   ", m))
        print(f"   pushed {len(secrets)} credential(s)", flush=True)

    print(f"\nre-running {len(targets)} backtest(s) on {worker['name']}, "
          f"{args.parallel} at a time\n", flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(run_one, worker, t): t for t in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            rows.append(r)
            if r["error"]:
                print(f"[{i}/{len(targets)}] bt{r['bt']} FAILED after {r['elapsed_s']}s: "
                      f"{r['error'][:140]}", flush=True)
            else:
                s, n = r["stored"], r["new"]
                print(f"[{i}/{len(targets)}] bt{r['bt']} {r['elapsed_s']}s  "
                      f"trades {s['total_trades']}->{n['total_trades']}  "
                      f"total {_pct(s['total_return'])}->{_pct(n['total_return'])}  "
                      f"CAR {_pct(s['annualized_return'])}->{_pct(n['annualized_return'])}",
                      flush=True)

    out_dir = ROOT / "reports" / "strategy_research"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    json_path = out_dir / f"dev_rerun_ceiling_fix_{stamp}.json"
    json_path.write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    md = report(rows, out_dir / f"dev_rerun_ceiling_fix_{stamp}.md")
    # This console is cp1252: the report FILE keeps its arrows, stdout gets ASCII, because
    # losing a finished 26-backtest run to a UnicodeEncodeError on the last line would be absurd.
    print("\n" + md.replace("→", "->"))
    print(f"\nwrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
