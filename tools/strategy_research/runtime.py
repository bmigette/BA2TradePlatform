"""Execution adapter. Heavy backend imports happen only when explicitly running a job."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
import importlib
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import sys

from tools.strategy_research.profiles import fingerprint

ROOT = Path(__file__).resolve().parents[2]


def add_source_paths():
    for path in (ROOT, ROOT / "testplatform/backend", ROOT / "testplatform",
                 ROOT / "packages/common", ROOT / "packages/providers", ROOT / "packages/experts"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


@contextmanager
def job_lock(path):
    """OS releases the advisory lock on exit/crash; never kill or guess other PIDs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Another research driver holds {path}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def check_database(path):
    """Read-only schema check before any backend initialization can write to a DB."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Backtest database does not exist: {path}")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"backtests", "strategies", "strategy_optimizations"}
    if not required <= tables:
        raise ValueError(f"Not a BA2 backtest database: missing {sorted(required - tables)}")
    return path


def code_signature():
    files = []
    for directory in ("packages", "testplatform/backend/app/services", "tools/strategy_research"):
        files.extend((ROOT / directory).rglob("*.py"))
    return fingerprint([(p.relative_to(ROOT).as_posix(), fingerprint(p.read_text(encoding="utf-8-sig")))
                        for p in sorted(files)])


def preflight(job, cache_dir, *, sample=150, min_covered_pct=75.0):
    """Resolve the *fixed* screened union, require files, and sample date coverage.

    This is a read-only cache check, not a fetch. It cannot certify every intraday gap
    or point-in-time fundamental record; the engine's hermetic cache checks still apply.
    """
    add_source_paths()
    import pandas as pd
    from ba2_providers.screener.metric_store import load_store, screened_symbol_union

    ready = deepcopy(job)
    bt = ready["optimization_config"]["backtest"]
    cache_dir = Path(cache_dir).resolve()
    start, end = date.fromisoformat(bt["start_date"]), date.fromisoformat(bt["end_date"])
    if not cache_dir.is_dir():
        raise ValueError(f"Missing OHLCV directory: {cache_dir}")
    store_files = []
    if "screener_opt" in bt:
        store = Path(bt["screener_opt"]["store"])
        if not store.is_dir():
            raise ValueError(f"Missing screener store: {store}")
        frame = load_store(str(store))
        if frame.empty:
            raise ValueError("Screener metric store is empty")
        first = pd.Timestamp(frame["date"].min()).date()
        last = pd.Timestamp(frame["date"].max()).date()
        if first > start + timedelta(days=7) or last < end - timedelta(days=7):
            raise ValueError(f"Screener store covers {first} to {last}, outside requested {start} to {end}")
        symbols = sorted(set(screened_symbol_union(frame, str(start), str(end),
                                                   bt["screener_opt"]["base_settings"])))
        store_files = sorted(store.rglob("*.parquet"))
    else:
        symbols = sorted(bt["enabled_instruments"])
    if not symbols:
        raise ValueError(f"{job['name']}: fixed screen selects no symbols")
    intervals = tuple(dict.fromkeys(("1d", bt["execution_interval"])))
    files = [cache_dir / f"{symbol}_{interval}.parquet" for symbol in symbols for interval in intervals]
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise ValueError(f"{len(missing)} missing OHLCV files; no symbols silently removed: {missing[:12]}")
    sampled = random.Random(bt["seed"]).sample(symbols, min(sample, len(symbols)))
    coverage = {interval: {"covered": 0, "eligible": 0, "late_listing": 0, "failures": []}
                for interval in intervals}
    for symbol in sampled:
        bounds = {}
        for interval in intervals:
            dates = pd.to_datetime(pd.read_parquet(cache_dir / f"{symbol}_{interval}.parquet",
                                                  columns=["Date"])["Date"], utc=True)
            if dates.empty or dates.isna().any():
                raise ValueError(f"Empty/invalid dates: {symbol} {interval}")
            bounds[interval] = (dates.min().date(), dates.max().date(),
                                bool(((dates >= pd.Timestamp(start, tz="UTC")) &
                                      (dates < pd.Timestamp(end + timedelta(days=1), tz="UTC"))).any()))
        for interval in intervals:
            record = coverage[interval]
            lo, hi, overlaps = bounds[interval]
            # Daily history is an existence proxy. Flag late listings separately; do
            # not call their missing pre-IPO warmup a cache defect.
            needed = start - timedelta(days=bt["warmup_days"]) if interval == "1d" else start
            late = bounds["1d"][0] > needed + timedelta(days=30)
            if late and interval == "1d":
                record["late_listing"] += 1
                continue
            if late and bounds["1d"][0] > start + timedelta(days=30):
                record["late_listing"] += 1
                continue
            record["eligible"] += 1
            if overlaps and lo <= needed + timedelta(days=30) and hi >= end - timedelta(days=30):
                record["covered"] += 1
            else:
                record["failures"].append(symbol)
    for interval, record in coverage.items():
        record["covered_pct"] = (100 * record["covered"] / record["eligible"]
                                  if record["eligible"] else None)
        if record["covered_pct"] is None or record["covered_pct"] < min_covered_pct:
            raise ValueError(f"{interval} coverage fails: {record}")
    bt["enabled_instruments"] = symbols
    # Data identity uses file metadata (not a content-level historical-data snapshot).
    evidence = {"symbols": len(symbols), "sample": len(sampled), "coverage": coverage,
                "code_signature": code_signature(),
                "cache_signature": fingerprint([(str(p), p.stat().st_size, p.stat().st_mtime_ns)
                                                 for p in sorted(files + store_files)])}
    ready["preflight"] = evidence
    ready["planned_name"] = job["name"]
    ready["name"] = f"{job['name']}-data{fingerprint(evidence)[:10]}"
    bt["name"] = ready["name"]
    bt["backtest_id"] = "research-" + fingerprint(ready)[:16]
    ready.pop("fingerprint")
    ready["fingerprint"] = fingerprint(ready)
    return ready


def ranked_results(rows, limit):
    """Deduplicate by parameters, not rounded fitness: tied neighbours remain inspectable."""
    valid = [r for r in rows if isinstance(r["fitness"], (int, float)) and math.isfinite(r["fitness"])]
    ranked, seen = [], set()
    for row in sorted(valid, key=lambda r: (-r["fitness"], fingerprint(r["params"]))):
        key = fingerprint(row["params"])
        if key not in seen:
            ranked.append(row)
            seen.add(key)
        if len(ranked) == limit:
            break
    if not ranked:
        raise RuntimeError("Optimization produced no finite evaluated candidates")
    return ranked


def persist_top(db, opt, job):
    """Resume partially saved TOP rows without rerunning the search or duplicating rows."""
    from app.models.backtest import Backtest
    from app.services.strategy_param_space import decode_params
    from app.services.strategy_optimization_handler import (
        _build_daily_trial_config, _build_hoisted_state, _persist_trial_worker)
    from app.services.backtest.daily_backtest_handler import _persist_results
    from app.services.strategy_fitness import compute_fitness
    from types import SimpleNamespace

    block = opt.optimization_config["backtest"]
    hoisted = _build_hoisted_state(block)
    candidates = list(opt.all_results)
    if opt.best_params is not None and opt.best_fitness is not None:
        candidates.append({"params": opt.best_params, "fitness": opt.best_fitness})
    ranked = ranked_results(candidates, job["save_top"])
    ids = []
    for rank, result in enumerate(ranked, 1):
        name = f"TOP{rank}-{opt.name}"
        rows = db.query(Backtest).filter_by(optimization_id=opt.id, name=name).all()
        if len(rows) > 1:
            raise RuntimeError(f"Duplicate saved backtests: {name}")
        bt = rows[0] if rows else None
        if bt is not None and bt.status == "completed":
            ids.append(bt.id)
            continue
        decoded = decode_params(SimpleNamespace(**job["strategy"]), result["params"])
        trial = _build_daily_trial_config(block, decoded, hoisted)
        params = {**result["params"], "expertFixedSettings": block["experts"][0]["settings"],
                  "entryRules": decoded["entry_rules"], "exitRules": decoded["exit_rules"],
                  "equityCap": block["account_settings"]["equity_cap"]}
        if bt is None:
            bt = Backtest(name=name, model_id=None, engine_type="daily_expert", expert_name=job["expert"],
                          optimization_id=opt.id, labels=block["labels"], strategy_params=params,
                          start_date=datetime.fromisoformat(block["start_date"]),
                          end_date=datetime.fromisoformat(block["end_date"]), initial_capital=block["initial_capital"])
            db.add(bt)
        bt.status = "running"
        bt.started_at = datetime.now()
        bt.error_message = None
        db.commit()
        trial.update(backtest_id=bt.id, name=name, persist_trading_db=True)
        try:
            output = _persist_trial_worker(trial)
            if not output["ok"]:
                raise RuntimeError(output["error"])
            compute_fitness(opt.fitness_metric, output["results"])
            _persist_results(db, bt, output["results"])
            bt.ga_fitness = result["fitness"]
            bt.status = "completed"
            bt.completed_at = datetime.now()
            bt.is_saved = True
            db.commit()
        except Exception as exc:
            db.rollback()
            bt.status = "failed"
            bt.error_message = str(exc)[:1000]
            db.commit()
            raise
        ids.append(bt.id)
        print(f"Saved {name}: backtest {bt.id}", flush=True)
    return ids


def execute_ready(job, database, *, resume=False):
    """Only called after explicit --run and successful cache preflight."""
    database = check_database(database)
    os.environ["DATABASE_URL"] = "sqlite:///" + database.as_posix()
    add_source_paths()
    import ba2test_launcher as launcher
    launcher._enter_backend()
    from app.models.database import SessionLocal
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.strategy_optimization_handler import handle_strategy_optimization
    from app.services.strategy_param_space import collect_param_space
    from types import SimpleNamespace

    expert_cls = getattr(importlib.import_module("ba2_experts." + job["expert"]), job["expert"])
    definitions = expert_cls.get_merged_settings_definitions()
    settings = job["optimization_config"]["backtest"]["experts"][0]["settings"]
    unknown = (set(settings) | set(job["optimization_config"]["expert_params"])) - set(definitions)
    if unknown:
        raise ValueError(f"Unknown expert settings: {sorted(unknown)}")
    collect_param_space(SimpleNamespace(**job["strategy"]), job["optimization_config"]["expert_params"])
    worker_ids = [w["id"] for w in launcher._resolve_worker_names(job["worker_names"])] if job["worker_names"] else []
    with SessionLocal() as db:
        rows = db.query(StrategyOptimization).filter_by(name=job["name"]).all()
        if len(rows) > 1:
            raise RuntimeError(f"Duplicate optimization names: {job['name']}")
        opt = rows[0] if rows else None
        if opt is not None:
            stored_strategy = db.get(Strategy, opt.strategy_id)
            if (opt.optimization_config != job["optimization_config"]
                    or opt.fitness_metric != job["fitness_metric"]
                    or opt.optimization_type != job["optimization_type"]
                    or stored_strategy is None
                    or stored_strategy.entry_rules != job["strategy"]["entry_rules"]
                    or stored_strategy.exit_rules != job["strategy"]["exit_rules"]):
                raise RuntimeError("Stored optimization differs from the prepared manifest")
            if opt.status != "completed" and not resume:
                raise RuntimeError(f"Optimization {opt.id} is {opt.status}; stop its other runner, then use --resume")
        else:
            strategy = Strategy(name=job["name"], description=job["hypothesis"], **job["strategy"])
            db.add(strategy)
            db.flush()
            opt = StrategyOptimization(name=job["name"], strategy_id=strategy.id,
                                       fitness_metric=job["fitness_metric"],
                                       optimization_type=job["optimization_type"],
                                       optimization_config=job["optimization_config"],
                                       worker_ids=worker_ids or None, status="pending")
            db.add(opt)
            db.commit()
        opt_id = opt.id
        if opt.status != "completed":
            print(f"Running optimization {opt_id}: {opt.name}", flush=True)
            result = handle_strategy_optimization(f"research10-{opt_id}", {"optimization_id": opt_id})
            if result["status"] != "completed":
                raise RuntimeError(f"Optimization {opt_id} did not complete: {result}")
            db.expire_all()
            opt = db.get(StrategyOptimization, opt_id)
        return {"optimization_id": opt_id, "backtest_ids": persist_top(db, opt, job)}
