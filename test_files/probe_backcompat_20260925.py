"""Backward-compatibility gate (2026-09-25): re-run stored EQUITY backtests on the OLD code and on
the NEW code and prove the results are byte-identical.

WHAT IT RUNS
    For each ``--bt`` id and each side (old/new), a SEPARATE child process:
      * puts ONLY that side's tree on sys.path (packages/common, packages/providers,
        packages/experts, testplatform/backend, testplatform, <tree>) ahead of the venv's
        editable installs (which point at the MAIN checkout) and drops the editable meta-path
        finders; then VERIFIES every imported module file lives under that tree (refuses if not);
      * rebuilds the run config through the REAL re-run path
        ``app.services.backtest.rerun_handler.rebuild_config_for_backtest(bt, db)`` (the same
        call ``handle_rerun_backtest`` makes) and runs ``run_daily_backtest(config)``;
      * then ``compute_fitness(opt.fitness_metric, results)`` exactly as the TOP-N persist does
        (it writes fitness_raw / fitness_robust / robustness into ``results``).
    Nothing is written anywhere except the scratchpad output folder:
      * the test DB is opened READ-ONLY (sqlite ``mode=ro`` + ``PRAGMA query_only``; any write
        raises) -- rerun_handler's row updates / _persist_results are NOT called;
      * TMP/TEMP point into the output folder, so the per-run trading sqlite
        (``persist_trading_db=True`` -> ``%TEMP%/ba2_backtest_dbs/run_<id>.sqlite``) never
        touches the stored row's own post-mortem DB;
      * hermetic: ``run_daily_backtest`` already runs under ``hermetic_fmp_history()`` and
        ``cached_only=True``; on top of that every outbound ``socket.connect``/``connect_ex`` is
        refused and counted.

COMPARISON
    Canonical JSON (sort_keys, exact float repr) of: the rebuilt config, the results scalars
    (results minus the three blobs), trades, equity_curve, drawdown_curve, the fitness value and
    the post-fitness results. Byte-identical = equal canonical strings. Keys that are inherently
    run-specific (``RUN_SPECIFIC_KEYS``) are stripped before the verdict -- and the report lists
    which of them were actually present. Key paths present on only one side are reported
    separately (an option-only key leaking into an equity run shows up there).
    OLD vs the STORED row is reported too (informational).

USAGE (main checkout venv)
    python test_files/probe_backcompat_20260925.py                       # both sides, bt 1225 1578
    python test_files/probe_backcompat_20260925.py --sides new --new-tree C:/.../BA2-obt-int
    python test_files/probe_backcompat_20260925.py --compare-only
    The OLD outputs are kept in --out, so re-running just NEW after later commits compares
    against them (the base worktree itself may have been removed; recreate it with
    ``git worktree add <path> bba4e0bf`` to re-run OLD).

Exit code: 0 = every pair byte-identical, 1 = a difference, 2 = a run could not be made.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

MAIN_REPO = r"C:\Users\basti\Documents\dev\BA2TradePlatform"
SCRATCH = (r"C:\Users\basti\AppData\Local\Temp\claude\C--Users-basti-Documents-dev-"
           r"BA2TradePlatform\820f80e0-b6ea-41a0-8f76-d0176d9f7156\scratchpad")
DEFAULT_OLD_TREE = os.path.join(SCRATCH, "bc_base")
DEFAULT_NEW_TREE = r"C:\Users\basti\Documents\dev\BA2-obt-int"
DEFAULT_OUT = os.path.join(SCRATCH, "backcompat")
TEST_DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"
DEFAULT_BTS = (1225, 1578)

#: Stripped at ANY depth before the verdict: a run's clock / identity, never a computed result.
#: Only the ones actually present are reported as "ignored".
RUN_SPECIFIC_KEYS = ("run_seconds", "elapsed", "elapsed_s", "started_at", "completed_at",
                     "created_at", "timestamp", "secs", "wall_seconds", "duration_s")

BLOBS = ("trades", "equity_curve", "drawdown_curve")
MAX_DIFFS = 12


def tree_paths(tree: str) -> List[str]:
    return [os.path.join(tree, "packages", "common"), os.path.join(tree, "packages", "providers"),
            os.path.join(tree, "packages", "experts"), os.path.join(tree, "testplatform", "backend"),
            os.path.join(tree, "testplatform"), tree]


# =================================================================================================
# canonical JSON
# =================================================================================================
def _default(o: Any) -> Any:
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if hasattr(o, "item"):
        return o.item()
    # never a repr with a memory address: that would differ between processes for no reason
    return f"<unserialisable {type(o).__module__}.{type(o).__name__}>"


def canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=_default, allow_nan=True,
                      separators=(",", ":"), ensure_ascii=True)


# =================================================================================================
# CHILD
# =================================================================================================
def run_child(tree: str, bt_id: int, out_path: str) -> int:
    import logging
    logging.disable(logging.INFO)          # standalone backtest: 10x+ slower otherwise

    tree = os.path.abspath(tree)
    for p in reversed(tree_paths(tree)):
        while p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    # The venv's editable finders (ba2_trade_platform, ba2test_launcher -> MAIN checkout) are
    # appended to meta_path after PathFinder, so sys.path already wins; drop them anyway.
    sys.meta_path[:] = [f for f in sys.meta_path if "__editable__" not in repr(f)
                        and "_EditableFinder" not in repr(f)]

    # ---- read-only test DB ---------------------------------------------------------------------
    import pathlib
    import sqlite3
    import sqlite3.dbapi2 as _d2
    test_db_norm = os.path.normcase(os.path.abspath(TEST_DB))
    _orig_connect = sqlite3.connect
    ro_hits = [0]

    def _ro_connect(database, *a, **kw):
        if isinstance(database, (str, os.PathLike)):
            s = str(database)
            if s != ":memory:" and not s.startswith("file:") and \
                    os.path.normcase(os.path.abspath(s)) == test_db_norm:
                ro_hits[0] += 1
                kw["uri"] = True
                con = _orig_connect(pathlib.Path(test_db_norm).as_uri() + "?mode=ro", *a, **kw)
                con.execute("PRAGMA query_only=ON")
                return con
        return _orig_connect(database, *a, **kw)

    sqlite3.connect = _ro_connect
    _d2.connect = _ro_connect
    os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"

    # ---- network off ---------------------------------------------------------------------------
    import socket
    net_attempts: List[str] = []

    def _blocked(self, address, *a, **kw):
        net_attempts.append(repr(address))
        raise OSError(f"backcompat probe: network disabled (connect to {address!r})")

    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
    if os.environ.get("BA2_HERMETIC_ALLOW_NETWORK"):
        print("REFUSING: BA2_HERMETIC_ALLOW_NETWORK is set")
        return 2

    # ---- mirror ba2test_launcher._enter_backend (chdir + ba2_common at the test DB + FMP key) ---
    os.chdir(os.path.join(tree, "testplatform", "backend"))
    from app.models.database import DATABASE_URL as _DB_URL
    from ba2_common.core import db as _ba2_db
    _ba2_db.configure_db(_DB_URL.replace("sqlite:///", "", 1))
    if not os.getenv("FMP_API_KEY"):
        from ba2_common.config import get_app_setting
        _k = get_app_setting("FMP_API_KEY")
        if _k:
            os.environ["FMP_API_KEY"] = _k

    import app.models  # noqa: F401 -- registers mappers
    import ba2_common, ba2_providers, ba2_experts, ba2test_launcher  # noqa: E401
    import ba2_trade_platform
    import app.services.backtest as _bt_pkg
    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.backtest import rerun_handler, daily_backtest_handler, results as _res_mod
    from app.services.backtest.daily_backtest_handler import run_daily_backtest
    from app.services.backtest.rerun_handler import rebuild_config_for_backtest
    from app.services import strategy_fitness, strategy_optimization_handler, strategy_param_space
    from app.services.strategy_fitness import compute_fitness
    import importlib
    _ds = importlib.import_module("ba2_experts.DeterministicScorer.combine")

    mods = {m.__name__: os.path.abspath(m.__file__) for m in (
        ba2_common, ba2_providers, ba2_experts, ba2test_launcher, ba2_trade_platform, _bt_pkg,
        rerun_handler, daily_backtest_handler, _res_mod, strategy_fitness,
        strategy_optimization_handler, strategy_param_space, _ds, _ba2_db)}
    bad = {k: v for k, v in mods.items()
           if not os.path.normcase(v).startswith(os.path.normcase(tree) + os.sep)}
    for k, v in mods.items():
        print(f"[import] {k:55s} {v}", flush=True)
    if bad:
        print(f"REFUSING: modules imported from outside {tree}: {bad}", flush=True)
        return 2

    head = subprocess.run(["git", "-C", tree, "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", tree, "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip().splitlines()

    db = SessionLocal()
    try:
        bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
        if bt is None:
            print(f"no backtest {bt_id}")
            return 2
        opt = (db.query(StrategyOptimization).filter(StrategyOptimization.id == bt.optimization_id)
               .first() if bt.optimization_id else None)
        fitness_metric = ((opt.fitness_metric if opt is not None else None) or bt.fitness_metric
                          or "consistent_annual_return")
        # THE re-run path: the exact reconstruction handle_rerun_backtest uses.
        cfg = rebuild_config_for_backtest(bt, db)
        bt_meta = {"id": bt.id, "name": bt.name, "expert": bt.expert_name,
                   "optimization_id": bt.optimization_id,
                   "start": str(bt.start_date), "end": str(bt.end_date),
                   "stored_total_trades": bt.total_trades}
    finally:
        db.close()
    if ro_hits[0] == 0:
        print("REFUSING: the test DB was not opened through the read-only wrapper")
        return 2
    config_canon = canon(cfg)
    print(f"[run] bt={bt_id} {bt_meta['name']} expert={bt_meta['expert']} "
          f"persist_trading_db={cfg.get('persist_trading_db')} backtest_id={cfg.get('backtest_id')} "
          f"n_instruments={len(cfg.get('enabled_instruments') or [])}", flush=True)

    from ba2_providers import fmp_common as _fc
    if hasattr(_fc, "reset_hermetic_misses"):
        _fc.reset_hermetic_misses()
    t0 = time.perf_counter()
    results = run_daily_backtest(cfg)
    elapsed = time.perf_counter() - t0
    raw = json.loads(canon(results))            # frozen BEFORE compute_fitness mutates results
    fitness = compute_fitness(fitness_metric, results)
    post = json.loads(canon(results))
    misses = sorted(_fc.hermetic_miss_symbols()) if hasattr(_fc, "hermetic_miss_symbols") else None

    out = {
        "meta": {"tree": tree, "git_head": head, "tracked_dirty_files": dirty,
                 "python": sys.executable, "pythonhashseed": os.environ.get("PYTHONHASHSEED"),
                 "modules": mods, "backtest": bt_meta, "fitness_metric": fitness_metric,
                 "elapsed_s": round(elapsed, 1), "network_attempts": net_attempts,
                 "hermetic_misses": misses, "ro_db_opens": ro_hits[0],
                 "tmp": os.environ.get("TEMP")},
        "config": json.loads(config_canon),
        "results_raw": raw,
        "fitness": fitness,
        "results_post_fitness": post,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(canon(out))
    print(f"[done] bt={bt_id} trades={raw.get('total_trades')} total_return={raw.get('total_return')} "
          f"final_equity={raw.get('final_equity')} fitness={fitness!r} elapsed={elapsed:.0f}s "
          f"net_attempts={len(net_attempts)} hermetic_misses={misses}", flush=True)
    return 0


# =================================================================================================
# COMPARISON (pure)
# =================================================================================================
def strip(obj: Any, keys=RUN_SPECIFIC_KEYS, found: Optional[set] = None, path: str = "") -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in keys:
                if found is not None:
                    found.add(_norm_path(f"{path}.{k}"))
                continue
            out[k] = strip(v, keys, found, f"{path}.{k}")
        return out
    if isinstance(obj, list):
        return [strip(v, keys, found, f"{path}[{i}]") for i, v in enumerate(obj)]
    return obj


def _norm_path(p: str) -> str:
    import re
    return re.sub(r"\[\d+\]", "[]", p)


def key_paths(obj: Any, path: str = "", acc: Optional[set] = None) -> set:
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(f"{path}.{k}")
            key_paths(v, f"{path}.{k}", acc)
    elif isinstance(obj, list):
        for v in obj:
            key_paths(v, f"{path}[]", acc)
    return acc


def diff_leaves(a: Any, b: Any, path: str, out: List[str]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append(f"{path}.{k}: <absent> vs {_r(b[k])}")
            elif k not in b:
                out.append(f"{path}.{k}: {_r(a[k])} vs <absent>")
            else:
                diff_leaves(a[k], b[k], f"{path}.{k}", out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: list length {len(a)} vs {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            diff_leaves(x, y, f"{path}[{i}]", out)
        return
    if canon(a) != canon(b):
        out.append(f"{path}: {_r(a)} vs {_r(b)}")


def _r(v: Any) -> str:
    s = canon(v)
    return s if len(s) <= 140 else s[:137] + "..."


def split_results(res: Dict[str, Any]) -> Dict[str, Any]:
    return {"results_scalars": {k: v for k, v in res.items() if k not in BLOBS},
            **{b: res.get(b) for b in BLOBS}}


def compare_sections(a: Dict[str, Any], b: Dict[str, Any], label_a: str, label_b: str
                     ) -> Tuple[bool, List[str]]:
    lines: List[str] = []
    all_same = True
    for sec in a:
        sa, sb = a[sec], b.get(sec)
        fa, fb = set(), set()
        xa, xb = strip(sa, found=fa), strip(sb, found=fb)
        same_strict = canon(sa) == canon(sb)
        same = canon(xa) == canon(xb)
        all_same &= same
        ign = sorted(fa | fb)
        status = "IDENTICAL" if same else "DIFFERENT"
        extra = ""
        if same and not same_strict:
            extra = " (only in ignored run-specific keys)"
        size = len(canon(sa))
        lines.append(f"  {sec:28s} {status}{extra}  [{size} bytes canonical"
                     f"{'; ignored keys present: ' + ', '.join(ign) if ign else ''}]")
        ka, kb = key_paths(xa), key_paths(xb)
        only_a, only_b = sorted(ka - kb), sorted(kb - ka)
        if only_a:
            lines.append(f"      key paths only in {label_a}: {only_a[:40]}")
        if only_b:
            lines.append(f"      key paths only in {label_b}: {only_b[:40]}")
        if not same:
            d: List[str] = []
            diff_leaves(xa, xb, sec, d)
            lines.append(f"      {len(d)} differing leaves; first {min(len(d), MAX_DIFFS)}:")
            lines.extend(f"        {x}" for x in d[:MAX_DIFFS])
    return all_same, lines


def sections_from_child(o: Dict[str, Any]) -> Dict[str, Any]:
    raw = split_results(o["results_raw"])
    post = o["results_post_fitness"]
    added = {k: v for k, v in post.items() if k not in o["results_raw"] or
             canon(v) != canon(o["results_raw"].get(k))}
    return {"config": o["config"], **raw, "fitness": o["fitness"],
            "fitness_written_keys": added}


def load_stored(bt_id: int) -> Dict[str, Any]:
    import sqlite3
    con = sqlite3.connect(f"file:{TEST_DB}?mode=ro", uri=True)
    try:
        row = con.execute("select results, trades, equity_curve, drawdown_curve, total_trades, "
                          "total_return, final_equity, max_drawdown, sharpe_ratio, ga_fitness "
                          "from backtests where id=?", (bt_id,)).fetchone()
    finally:
        con.close()
    return {"results_scalars": json.loads(row[0]) if row[0] else None,
            "trades": json.loads(row[1]) if row[1] else None,
            "equity_curve": json.loads(row[2]) if row[2] else None,
            "drawdown_curve": json.loads(row[3]) if row[3] else None,
            "_cols": {"total_trades": row[4], "total_return": row[5], "final_equity": row[6],
                      "max_drawdown": row[7], "sharpe_ratio": row[8], "ga_fitness": row[9]}}


def headline(res: Dict[str, Any]) -> str:
    keys = ("total_trades", "total_return", "final_equity", "max_drawdown", "sharpe_ratio",
            "fitness_raw", "fitness_robust")
    return ", ".join(f"{k}={res.get(k)!r}" for k in keys)


# =================================================================================================
# PARENT
# =================================================================================================
def child_out(out_dir: str, bt_id: int, side: str) -> str:
    return os.path.join(out_dir, f"bt{bt_id}_{side}.json")


def spawn(side: str, tree: str, bt_id: int, out_dir: str, timeout_h: float) -> int:
    tmp = os.path.join(out_dir, "tmp", f"{side}_bt{bt_id}")
    os.makedirs(tmp, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "logs"), exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(tree_paths(os.path.abspath(tree)))
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = tmp
    env["PYTHONHASHSEED"] = "0"            # same for both sides: removes hash-order as a confound
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("BA2_SHARED_ARRAYS", None)
    log = os.path.join(out_dir, "logs", f"bt{bt_id}_{side}.log")
    cmd = [sys.executable, os.path.abspath(__file__), "--_child", "--tree", tree,
           "--bt", str(bt_id), "--out-file", child_out(out_dir, bt_id, side)]
    print(f"[{side}] bt {bt_id}: {' '.join(cmd)}\n        log -> {log}", flush=True)
    t0 = time.time()
    with open(log, "w", encoding="utf-8") as lf:
        try:
            rc = subprocess.run(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT,
                                timeout=timeout_h * 3600, cwd=MAIN_REPO).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    print(f"[{side}] bt {bt_id}: rc={rc} in {time.time() - t0:.0f}s", flush=True)
    return rc


def compare(out_dir: str, bts: List[int]) -> int:
    report: List[str] = []
    verdict = 0
    for bt_id in bts:
        po, pn = child_out(out_dir, bt_id, "old"), child_out(out_dir, bt_id, "new")
        report.append("=" * 100)
        if not (os.path.exists(po) and os.path.exists(pn)):
            report.append(f"bt {bt_id}: missing output (old={os.path.exists(po)} new={os.path.exists(pn)})")
            verdict = max(verdict, 2)
            continue
        with open(po, encoding="utf-8") as f:
            o = json.load(f)
        with open(pn, encoding="utf-8") as f:
            n = json.load(f)
        m = o["meta"]["backtest"]
        report.append(f"bt {bt_id}: {m['name']}  expert={m['expert']}  opt={m['optimization_id']}  "
                      f"window={m['start'][:10]}..{m['end'][:10]}  stored_trades={m['stored_total_trades']}")
        for side, x in (("OLD", o), ("NEW", n)):
            mm = x["meta"]
            report.append(f"  {side}: head={mm['git_head']} dirty={len(mm['tracked_dirty_files'])} "
                          f"elapsed={mm['elapsed_s']}s net_attempts={len(mm['network_attempts'])} "
                          f"hermetic_misses={mm['hermetic_misses']} fitness_metric={mm['fitness_metric']} "
                          f"ro_db_opens={mm['ro_db_opens']}")
            report.append(f"       {headline(x['results_post_fitness'])}")
        same, lines = compare_sections(sections_from_child(o), sections_from_child(n), "OLD", "NEW")
        report.append(f"  OLD vs NEW: {'BYTE-IDENTICAL' if same else 'DIFFERENT'}")
        report.extend(lines)
        if not same:
            verdict = max(verdict, 1)
        # informational: OLD vs STORED
        st = load_stored(bt_id)
        cols = st.pop("_cols")
        old_post = split_results(o["results_post_fitness"])
        st_same, st_lines = compare_sections(st, {k: old_post[k] for k in st}, "STORED", "OLD")
        report.append(f"  OLD vs STORED (informational): {'IDENTICAL' if st_same else 'DIFFERENT'}")
        report.append(f"       STORED cols: {cols}")
        report.append(f"       STORED results: {headline(st['results_scalars'] or {})}")
        report.extend(st_lines)
    text = "\n".join(report)
    print(text)
    with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-tree", default=DEFAULT_OLD_TREE)
    ap.add_argument("--new-tree", default=DEFAULT_NEW_TREE)
    ap.add_argument("--bt", type=int, nargs="+", default=list(DEFAULT_BTS))
    ap.add_argument("--sides", default="old,new")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--compare-only", action="store_true")
    ap.add_argument("--timeout-h", type=float, default=4.0)
    ap.add_argument("--_child", action="store_true")
    ap.add_argument("--tree")
    ap.add_argument("--out-file")
    a = ap.parse_args()
    if a._child:
        return run_child(a.tree, a.bt[0], a.out_file)
    os.makedirs(a.out, exist_ok=True)
    if not a.compare_only:
        trees = {"old": a.old_tree, "new": a.new_tree}
        for bt_id in a.bt:
            for side in [s.strip() for s in a.sides.split(",") if s.strip()]:
                rc = spawn(side, trees[side], bt_id, a.out, a.timeout_h)
                if rc != 0:
                    print(f"child failed (rc={rc}); see log", flush=True)
                    return 2
    return compare(a.out, a.bt)


if __name__ == "__main__":
    sys.exit(main())
