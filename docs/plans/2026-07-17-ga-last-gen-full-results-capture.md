# GA Last-Generation Full-Results Capture Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the slow, hang-prone "re-run top-N from scratch" phase that runs after every
GA optimization, by capturing each individual's full backtest results (trades/equity/drawdown)
directly during the **last generation's** normal evaluation, and persisting the top-N Backtests
from that already-computed data — falling back to a real re-run only for the rare top-N member
whose full results weren't captured (an elite individual reused via the GA's memo, so it wasn't
freshly evaluated in the last generation).

**Architecture:** Three layers, bottom-up:
1. `_trial_worker` (the function BOTH the local `ProcessPoolExecutor` workers and the remote
   `/run-trial` HTTP endpoint call) gains an opt-in "also return full results" mode, triggered by
   a special key embedded in the trial `config` dict — this crosses the process/network boundary
   for free (config is already pickled/JSON-serialized to workers), so no new endpoint or wire
   contract change is needed.
2. `handle_strategy_optimization`'s `batch_fitness` closure sets that flag only for the
   population being evaluated in the GA's final generation, and accumulates each returned
   `full_results` blob into an in-memory `last_gen_full_results: Dict[str, dict]` (keyed by the
   trial's cache key) that lives for the duration of the run. **This buffer is stashed in a
   module-level, process-local, `opt_id`-keyed scratch dict
   (`_last_gen_full_results_by_opt`) — it is NOT added to `handle_strategy_optimization`'s
   return value.** That return dict flows into `TaskQueue.result` (a JSON DB column,
   `_process_task_inline` in `task_queue.py:719-726`) for every UI/API-submitted optimization —
   the main task queue registers this handler with `use_subprocess=False`
   (`app/main.py:311`, `task_queue.py:56` default) — so anything added to the return dict gets
   persisted to the DB for every real job, not just the CLI path. The buffer can hold a full
   generation's worth of trade/equity/drawdown blobs (~100-140 individuals), so it must never
   flow through that generic path. Only the CLI's direct, in-process caller (Task 3, below) pops
   it from the module-level dict right after `handle_strategy_optimization` returns.
3. `_persist_top_backtests` (the CLI's post-GA persist step) reads and pops the buffer from
   `_last_gen_full_results_by_opt` (keyed by `opt_id`, defaulting to `{}`/`None` if absent — e.g.
   a UI-submitted run, or a CLI run whose process was interrupted before reaching this step). For
   each of the top-N ranked param sets it now persists DIRECTLY from the buffered full results
   when the trial's key is present, and falls back to the existing re-run codepath only for the
   (typically 0-2) top-N members whose key is missing.

**Why this is safe / doesn't slow the GA:** `run_daily_backtest` already computes the full
results dict internally for EVERY trial today — `_trial_worker` just discards it down to
`{ok, fitness, trades}` before returning, specifically to keep the pickled payload small across
~140 individuals × 8 generations. This plan does not add any new computation; it only skips the
discard for the ~100-140 individuals of the *last* generation, adding pure serialization/transfer
cost for that one generation (JSON-friendly plain data: numbers, dicts, short lists) instead of
the several-minutes-each cost of re-running full backtests from scratch for 5 individuals (the
current, hang-prone behavior observed live on 2026-07-17: `_persist_top_backtests`'s
`ProcessPoolExecutor`+`ThreadPoolExecutor` fan-out hung mid-run with only 1/5 saved).

**Known limitations (explicitly in scope, not silently traded away):**
- The last generation's population may not contain the GA's true best-ever individual if it was
  out-competed after elitism stopped carrying it forward. The *existing* re-run code sidesteps
  this by picking top-N from `opt.all_results` (every generation). This plan keeps that exact
  selection logic — `last_gen_full_results` is only a *fast path* for individuals that happen to
  be available; the fallback re-run still runs for anything not in the buffer, so correctness
  (which individuals get persisted) is unchanged from today. Only the speed/reliability of *how*
  they get persisted changes.
- `is_last_gen` is computed as `gen_state["gen"] == n_gens - 1` — i.e. "the generation the GA's
  configured budget says is last." When a run exits via **early stopping**
  (`GeneticOptimizer.optimize()`'s `no_improvement_count >= early_stopping_generations` break,
  `genetic.py`) before reaching that generation, the buffer stays empty for the whole run — early
  stopping is a normal, commonly-hit exit path (not an edge case), so this means the fast path
  does nothing for a meaningful fraction of real runs. This degrades gracefully (Task 3's
  fallback-to-full-rerun handles an empty/missing buffer correctly — same behavior as today, just
  without the speedup) so it is NOT a correctness bug, only a missed optimization. Deliberately
  not fixed in this pass: the alternative (capture full results on EVERY generation, keeping only
  the most-recently-evaluated one) would pay the serialization/network cost on every generation
  instead of once, which risks reintroducing exactly the kind of remote-worker payload/timeout
  strain this session already fought once (the Senate matrix cold-cache timeouts). Worth
  revisiting as a follow-up if early-stopped runs turn out to be the common case in practice.

**Scope:** Only the `parallelIndividuals > 1` / `batch_fitness` path (the one every real grid job
uses — confirmed live: `--parallel 4`). The `parallel <= 1` sequential path is unaffected/out of
scope; it keeps today's re-run behavior.

**Tech Stack:** Python, `concurrent.futures` (ProcessPoolExecutor/ThreadPoolExecutor), FastAPI
(`worker_server.py` — wire contract unchanged), SQLAlchemy (`Backtest`/`StrategyOptimization`),
pytest (reusing patterns already established in `test_persist_top_backtests.py` and
`test_strategy_optimization_handler.py`).

---

### Task 1: `_trial_worker` opt-in full-results capture

**Files:**
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py:109-139`
- Test: `testplatform/backend/tests/test_strategy_optimization_handler.py`

**Step 1: Write the failing tests**

Add near the other `_trial_worker`-adjacent tests (e.g. after `test_memo_returns_same_fitness_on_reselection`, ~line 445-465):

```python
def test_trial_worker_default_omits_full_results(monkeypatch):
    """Regression guard: the hot GA path (no flag) must NOT grow a payload — full_results must
    be absent, not just empty/None, so the pickled return stays the same small shape it is today."""
    from app.services import strategy_optimization_handler as H

    monkeypatch.setattr(
        H, "run_daily_backtest",
        lambda cfg: {"total_trades": 3, "sharpe_ratio": 1.5},
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.backtest.daily_backtest_handler.run_daily_backtest",
        lambda cfg: {"total_trades": 3, "sharpe_ratio": 1.5},
    )
    out = H._trial_worker({"backtest_id": 1}, "sharpe")
    assert out["ok"] is True
    assert "full_results" not in out


def test_trial_worker_want_full_flag_attaches_results_and_is_stripped_from_config():
    """When the caller marks a config `_want_full_results`, the worker (a) returns the full
    results blob under `full_results` alongside the normal {fitness,trades} summary, and (b)
    pops the flag before handing the config to run_daily_backtest — a real backtest config
    builder has no idea about this internal signal and must never see it."""
    from app.services import strategy_optimization_handler as H

    seen_configs = []

    def _fake_run_daily_backtest(cfg):
        seen_configs.append(dict(cfg))
        return {"total_trades": 7, "sharpe_ratio": 2.0}

    import app.services.backtest.daily_backtest_handler as _dbh
    orig = _dbh.run_daily_backtest
    _dbh.run_daily_backtest = _fake_run_daily_backtest
    try:
        out = H._trial_worker({"backtest_id": 1, "_want_full_results": True}, "sharpe")
    finally:
        _dbh.run_daily_backtest = orig

    assert out["ok"] is True
    assert out["fitness"] == 2.0
    assert out["trades"] == 7
    assert out["full_results"] == {"total_trades": 7, "sharpe_ratio": 2.0}
    assert "_want_full_results" not in seen_configs[0]
```

**Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_strategy_optimization_handler.py -k trial_worker_want_full -v`
Expected: FAIL — `full_results` not in `out` / `KeyError` (flag not yet implemented).

**Step 3: Implement**

`testplatform/backend/app/services/strategy_optimization_handler.py:109-139` — current code:

```python
def _trial_worker(config: Dict[str, Any], fitness_metric: str) -> Dict[str, Any]:
    """Run ONE deterministic daily backtest in a worker PROCESS and return a tiny summary.

    Only the CPU-bound backtest runs here (no GIL contention with the GA loop); the result is
    reduced to ``{ok, fitness, trades, error}`` so the pickled payload back to the parent is
    small (the full equity/trade blobs are re-derived later for the persisted top-N only).

    The cross-job OHLCV-memo eviction (the remote/local worker memory leak fix) lives in
    ``run_daily_backtest`` — the single chokepoint EVERY path goes through — so it covers the pool
    workers here AND the master's in-process top-N persist / parallel=1 runs uniformly.
    """
    try:
        from app.services.backtest.daily_backtest_handler import run_daily_backtest
        from app.services.strategy_fitness import compute_fitness

        results = run_daily_backtest(config)
        fit = compute_fitness(fitness_metric, results)
        return {"ok": True, "fitness": float(fit),
                "trades": int(results.get("total_trades") or 0), "error": None,
                "mem": _trial_memory_snapshot()}
    except Exception as e:  # noqa: BLE001 — surface as a failed trial, don't kill the pool
        fatal = type(e).__name__ in ("BacktestCacheMiss", "FMPHistoryCacheMiss")
        return {"ok": False, "fitness": 0.0, "trades": 0, "error": str(e) if fatal else repr(e),
                "fatal": fatal, "mem": _trial_memory_snapshot()}
```

New code:

```python
def _trial_worker(config: Dict[str, Any], fitness_metric: str) -> Dict[str, Any]:
    """Run ONE deterministic daily backtest in a worker PROCESS and return a tiny summary.

    Only the CPU-bound backtest runs here (no GIL contention with the GA loop); the result is
    reduced to ``{ok, fitness, trades, error}`` so the pickled payload back to the parent is
    small (the full equity/trade blobs are re-derived later for the persisted top-N only) --
    UNLESS the caller sets ``config["_want_full_results"] = True`` (used only for the GA's final
    generation, see ``handle_strategy_optimization``'s ``batch_fitness``), in which case the full
    ``run_daily_backtest`` results dict is ALSO returned under ``full_results``. The flag is
    popped before the config reaches ``run_daily_backtest`` -- it is an internal signal between
    the GA loop and this worker, not a real backtest setting.

    The cross-job OHLCV-memo eviction (the remote/local worker memory leak fix) lives in
    ``run_daily_backtest`` — the single chokepoint EVERY path goes through — so it covers the pool
    workers here AND the master's in-process top-N persist / parallel=1 runs uniformly.
    """
    want_full = config.pop("_want_full_results", False)
    try:
        from app.services.backtest.daily_backtest_handler import run_daily_backtest
        from app.services.strategy_fitness import compute_fitness

        results = run_daily_backtest(config)
        fit = compute_fitness(fitness_metric, results)
        out = {"ok": True, "fitness": float(fit),
               "trades": int(results.get("total_trades") or 0), "error": None,
               "mem": _trial_memory_snapshot()}
        if want_full:
            out["full_results"] = results
        return out
    except Exception as e:  # noqa: BLE001 — surface as a failed trial, don't kill the pool
        fatal = type(e).__name__ in ("BacktestCacheMiss", "FMPHistoryCacheMiss")
        return {"ok": False, "fitness": 0.0, "trades": 0, "error": str(e) if fatal else repr(e),
                "fatal": fatal, "mem": _trial_memory_snapshot()}
```

**Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_strategy_optimization_handler.py -k trial_worker -v`
Expected: PASS (both new tests + the pre-existing ones in the same file, unaffected).

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/strategy_optimization_handler.py \
        testplatform/backend/tests/test_strategy_optimization_handler.py
git commit -m "feat(ga): opt-in full-results capture in _trial_worker"
```

---

### Task 2: Last-generation buffering inside `batch_fitness`

**Files:**
- Modify: `testplatform/backend/app/services/strategy_optimization_handler.py`
  (inside `handle_strategy_optimization`, the `make_batch_fitness` closure ~line 456-538, plus
  the completion return dict ~line 676-681)
- Test: `testplatform/backend/tests/test_strategy_optimization_handler.py`

**Step 1: Write the failing tests**

These two helpers are extracted as small, module-level, pure(ish) functions specifically so they
are unit-testable without spinning up a real `ProcessPoolExecutor` (Windows uses spawn — an
in-process `monkeypatch` is invisible to a spawned child, so hermetically testing the *real*
multiprocess path is impractical; the existing suite avoids this the same way — see
`test_persist_top_backtests.py`'s docstring on function-local imports).

Add to `testplatform/backend/tests/test_strategy_optimization_handler.py`:

```python
def test_mark_want_full_only_tags_last_generation():
    from app.services import strategy_optimization_handler as H

    cfg = {"backtest_id": 1}
    untouched = H._maybe_mark_want_full(cfg, is_last_gen=False)
    assert untouched is cfg  # no copy needed when untagged
    assert "_want_full_results" not in untouched

    tagged = H._maybe_mark_want_full(cfg, is_last_gen=True)
    assert tagged["_want_full_results"] is True
    assert "_want_full_results" not in cfg  # original dict must not be mutated


def test_capture_full_result_stores_only_when_present():
    from app.services import strategy_optimization_handler as H

    buf: dict = {}
    H._capture_full_result(buf, "key-a", {"ok": True, "fitness": 1.0, "trades": 3})
    assert buf == {}  # no full_results key on the worker output -> nothing stored

    H._capture_full_result(buf, "key-b", {"ok": True, "fitness": 1.0, "trades": 3,
                                           "full_results": {"total_trades": 3}})
    assert buf == {"key-b": {"total_trades": 3}}
```

**Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_strategy_optimization_handler.py -k "mark_want_full or capture_full_result" -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_maybe_mark_want_full'`.

**Step 3: Implement**

3a. Add the two module-level helpers right above `_trial_worker`
(`testplatform/backend/app/services/strategy_optimization_handler.py`, just before line 109):

```python
def _maybe_mark_want_full(config: Dict[str, Any], is_last_gen: bool) -> Dict[str, Any]:
    """Attach the opt-in full-results flag (see ``_trial_worker``) to a trial config when this
    job belongs to the GA's FINAL generation. Returns a shallow copy when tagging (never mutates
    the caller's config in place) so the same base dict can be reused for other jobs."""
    if not is_last_gen:
        return config
    tagged = dict(config)
    tagged["_want_full_results"] = True
    return tagged


def _capture_full_result(buffer: Dict[str, Any], key: str, out: Dict[str, Any]) -> None:
    """Stash a trial's full results blob (if the worker computed one — see
    ``_maybe_mark_want_full``) into the last-generation buffer, keyed by trial key. Used by
    ``_persist_top_backtests`` to skip a re-run for any top-N individual that was already fully
    computed as part of the final generation."""
    full = out.get("full_results")
    if full is not None:
        buffer[key] = full
```

3b. Wire them into `make_batch_fitness`. Add the buffer declaration alongside `all_results`
(search for where `all_results` and `memo` are declared before `make_batch_fitness`'s
definition, ~line 456) — the buffer must be declared in the SAME enclosing scope as
`all_results` so both the closure and the final return statement can see it:

```python
last_gen_full_results: Dict[str, Any] = {}
```

In the job-building loop inside `batch_fitness` (`strategy_optimization_handler.py:462-471`),
current code:

```python
                for i, flat in enumerate(param_dicts):
                    key = _trial_key_for(flat)
                    cached = memo.get(key)
                    if cached is not None:
                        fits[i] = cached
                        continue
                    config = _build_daily_trial_config(
                        backtest_cfg, decode_params(strategy, flat), hoisted
                    )
                    jobs.append((i, flat, key, config))
```

New code (adds the last-gen tag):

```python
                n_gens = int(ga["generations"])
                is_last_gen = gen_state["gen"] == n_gens - 1
                for i, flat in enumerate(param_dicts):
                    key = _trial_key_for(flat)
                    cached = memo.get(key)
                    if cached is not None:
                        fits[i] = cached
                        continue
                    config = _build_daily_trial_config(
                        backtest_cfg, decode_params(strategy, flat), hoisted
                    )
                    config = _maybe_mark_want_full(config, is_last_gen)
                    jobs.append((i, flat, key, config))
```

Note: `n_gens` is already computed a few lines below this point in the existing code (originally
at what is now a later line, next to `total_in_batch`) — when applying this change, delete the
now-duplicate `n_gens = int(ga["generations"])` a few lines down (it's the same value,
recomputing it twice is harmless but redundant; keep whichever placement reads cleaner, just
don't declare it twice with two different reads of `ga["generations"]` that could theoretically
diverge if `ga` were mutated mid-call, which it isn't, so a single declaration is preferred).

In the results-consumption loop (`strategy_optimization_handler.py:498-506`), current code:

```python
                for i, flat, key, out in execute_jobs(jobs):
                    if out["ok"]:
                        fit = float(out["fitness"])
                        fits[i] = fit
                        memo.put(key, fit)
                        all_results.append(
                            {"params": flat, "fitness": fit, "key": key, "trades": out["trades"]}
                        )
```

New code (adds the capture call):

```python
                for i, flat, key, out in execute_jobs(jobs):
                    if out["ok"]:
                        fit = float(out["fitness"])
                        fits[i] = fit
                        memo.put(key, fit)
                        all_results.append(
                            {"params": flat, "fitness": fit, "key": key, "trades": out["trades"]}
                        )
                        if is_last_gen:
                            _capture_full_result(last_gen_full_results, key, out)
```

3c. **Do NOT add `last_gen_full_results` to `handle_strategy_optimization`'s return dict.** That
return value flows into `TaskQueue.result` (a JSON DB column) for EVERY UI/API-submitted
optimization — the main task queue registers this handler with `use_subprocess=False`
(`app/main.py:311`; `TaskQueueService.__init__` defaults `use_subprocess=False`,
`task_queue.py:56`), so `_process_task_inline` (`task_queue.py:706-726`) does
`result = handler(task_id, task.payload)` then `db_task.result = result; db.commit()` — anything
in the return dict gets persisted to the DB for every real job, not just the CLI's direct-call
path. `last_gen_full_results` can hold a full generation's worth of trade/equity/drawdown blobs
(~100-140 individuals), so it must never flow through that path.

Instead, add a module-level, process-local, `opt_id`-keyed scratch dict — declare it near the top
of `strategy_optimization_handler.py`, alongside the other module-level state (e.g. near where
`_trial_worker`/`_maybe_mark_want_full` are defined):

```python
# Process-local scratch space for the CLI's post-optimization top-N persist step (see
# _persist_top_backtests in ba2test_launcher.py). Deliberately NOT part of
# handle_strategy_optimization's return value: that return dict flows into TaskQueue.result (a
# JSON DB column) for every UI/API-submitted job via the main task queue's inline handler path
# (use_subprocess=False), and this buffer can hold a full generation's worth of trade/equity/
# drawdown blobs -- it must never be serialized to the DB. Only the CLI's direct, same-process
# caller ever reads this (via pop, right after handle_strategy_optimization returns), so a
# long-lived worker process handling many jobs over time doesn't accumulate entries for jobs
# nobody ever collected.
_last_gen_full_results_by_opt: Dict[int, Dict[str, Any]] = {}
```

Then, at the point in `handle_strategy_optimization` where the completion return dict is built
(`strategy_optimization_handler.py:676-681`), current code:

```python
        return {
            "status": "completed",
            "optimization_id": opt_id,
            "best_fitness": result["best_fitness"],
            "best_params": result["best_params"],
        }
```

New code (return dict is UNCHANGED — the buffer is stashed as a side effect just before
returning, not added to the dict):

```python
        if last_gen_full_results:
            _last_gen_full_results_by_opt[opt_id] = last_gen_full_results
        return {
            "status": "completed",
            "optimization_id": opt_id,
            "best_fitness": result["best_fitness"],
            "best_params": result["best_params"],
        }
```

Also handle the `parallel <= 1` path (no `batch_fitness` at all, per this plan's stated scope) —
`last_gen_full_results` stays an empty `{}` there (declared once, unconditionally, at the same
scope as `all_results = []`, before the `if parallel > 1:` branch), so the
`if last_gen_full_results:` guard above naturally skips stashing anything for that path — nothing
extra to do.

3d. Add one more test proving the return dict does NOT carry the buffer (this is the regression
guard for the bug this redesign exists to prevent — a future edit must not silently reintroduce
it). Add to `testplatform/backend/tests/test_strategy_optimization_handler.py`, near the other
`handle_strategy_optimization`-level tests (e.g. next to `test_handler_completes_and_persists_best`):

```python
def test_completion_return_dict_never_carries_last_gen_full_results(monkeypatch):
    """Regression guard: handle_strategy_optimization's return value flows into
    TaskQueue.result (a JSON DB column) for every UI/API-submitted job — last_gen_full_results
    must NEVER be a key in that dict, no matter how large or small it is. The buffer is
    consumed via the module-level _last_gen_full_results_by_opt dict instead (popped by the
    CLI's direct caller, not returned)."""
    from app.services import strategy_optimization_handler as H

    opt_id = _seed_optimization_row()  # reuse whatever fixture/seed helper this file already
                                        # uses to build a minimal runnable StrategyOptimization +
                                        # Strategy pair for handle_strategy_optimization tests —
                                        # follow the exact pattern test_handler_completes_and_
                                        # persists_best already uses in this same file, including
                                        # its monkeypatch of the per-trial backtest stub.
    res = H.handle_strategy_optimization("test-task", {"optimization_id": opt_id})
    assert res["status"] == "completed"
    assert "last_gen_full_results" not in res
```

(The implementer should look at the existing `test_handler_completes_and_persists_best` test
immediately above this one in the file for the real fixture/monkeypatch setup to copy — don't
invent a new one; match its exact pattern for building a runnable optimization row and stubbing
the backtest so this test actually exercises the real `handle_strategy_optimization` completion
path, including a `parallelIndividuals` value that engages `batch_fitness` if that existing test
already does, or add `parallelIndividuals: 2` to the config if the existing fixture defaults to
1 — either way, the point of this test is that the return dict is checked directly, not the
module-level buffer.)

**Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_strategy_optimization_handler.py -v`
Expected: PASS — all tests including the three new ones, and NO regression in
`test_seeded_run_is_reproducible` / `test_handler_completes_and_persists_best`.

**Step 5: Commit**

```bash
git add testplatform/backend/app/services/strategy_optimization_handler.py \
        testplatform/backend/tests/test_strategy_optimization_handler.py
git commit -m "feat(ga): buffer full results for the GA's last generation"
```

---

### Task 3: `_persist_top_backtests` consumes the buffer, re-runs only what's missing

**Files:**
- Modify: `testplatform/ba2test_launcher.py:2425-2596` (`_persist_top_backtests`) and its two
  call sites (`:2248`, `:2411`)
- Test: `testplatform/backend/tests/test_persist_top_backtests.py`

**Step 1: Write the failing tests**

Add to `testplatform/backend/tests/test_persist_top_backtests.py` (reusing `_seed_opt_for_top_n`
and `_RESULTS` already defined there):

```python
def test_persist_top_uses_buffered_full_results_without_rerun(monkeypatch):
    """When every top-N individual's full results are already in the buffer (the common case —
    they came from the GA's own last generation), _persist_top_backtests must NOT invoke
    _persist_trial_worker (the re-run path) at all."""
    opt_id = _seed_opt_for_top_n()
    # Overwrite all_results with an entry carrying a real trial `key` so the buffer lookup works.
    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter_by(id=opt_id).first()
        opt.all_results = [{"params": {}, "fitness": 1.23, "trades": 10, "key": "trial-key-1"}]
        db.commit()
    finally:
        db.close()

    calls = []
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: calls.append(bt.name))
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: (_ for _ in ()).throw(AssertionError("must not re-run — buffer had this key")),
    )

    persisted = mod._persist_top_backtests(
        opt_id, "FMPRating", n=1, parallel=1,
        last_gen_full_results={"trial-key-1": dict(_RESULTS)},
    )

    assert persisted == 1
    assert calls == ["TOP1-top-n-opt"]


def test_persist_top_reruns_only_the_missing_members(monkeypatch):
    """Two top-N individuals: one's key is in the buffer (persist directly), the other's is not
    (e.g. an elite reused via the GA memo, never freshly evaluated in the last generation) — only
    the missing one goes through the re-run path."""
    opt_id = _seed_opt_for_top_n()
    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter_by(id=opt_id).first()
        opt.all_results = [
            {"params": {"a": 1}, "fitness": 2.0, "trades": 10, "key": "buffered-key"},
            {"params": {"a": 2}, "fitness": 1.0, "trades": 5, "key": "missing-key"},
        ]
        db.commit()
    finally:
        db.close()

    rerun_calls = []
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: None)
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: rerun_calls.append(cfg) or {"ok": True, "results": dict(_RESULTS)},
    )

    persisted = mod._persist_top_backtests(
        opt_id, "FMPRating", n=2, parallel=1,
        last_gen_full_results={"buffered-key": dict(_RESULTS)},
    )

    assert persisted == 2
    assert len(rerun_calls) == 1  # only the "missing-key" individual was re-run


def test_persist_top_no_buffer_falls_back_to_full_rerun(monkeypatch):
    """Backward compatibility: calling without last_gen_full_results (or with None) behaves
    exactly like today — every top-N individual is re-run."""
    opt_id = _seed_opt_for_top_n()
    calls = []
    monkeypatch.setattr(_sync_client, "push_backtest", lambda bt, db: calls.append(bt.name))
    monkeypatch.setattr(
        _soh, "_persist_trial_worker",
        lambda cfg: {"ok": True, "results": dict(_RESULTS)},
    )

    persisted = mod._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1)

    assert persisted == 1
    assert calls == ["TOP1-top-n-opt"]
```

**Step 2: Run tests to verify they fail**

Run: `./venv/bin/python -m pytest tests/test_persist_top_backtests.py -k "buffered_full_results or reruns_only_the_missing or no_buffer_falls_back" -v`
Expected: FAIL — `TypeError: _persist_top_backtests() got an unexpected keyword argument 'last_gen_full_results'`.

**Step 3: Implement**

3a. `testplatform/ba2test_launcher.py:2425` — add the parameter:

```python
def _persist_top_backtests(opt_id: int, expert: str, n: int = 5, parallel: int = 1,
                            last_gen_full_results: Optional[Dict[str, Any]] = None) -> int:
```

(Confirm `Optional`/`Dict`/`Any` are already imported at module scope in `ba2test_launcher.py` —
they are used elsewhere in the file already, e.g. `_do_senate_scores`'s signature added earlier
this session.)

3b. `testplatform/ba2test_launcher.py:2466-2482` — carry the trial `key` alongside params so the
buffer can be looked up. Current code:

```python
        seen, ranked = set(), []
        for r in sorted(opt.all_results or [], key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9), reverse=True):
            fit = r.get("fitness")
            key = round(fit, 6) if isinstance(fit, (int, float)) else _json.dumps(r.get("params"), sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            ranked.append(r["params"])
            if len(ranked) >= n:
                break
        if not ranked and opt.best_params:
            ranked = [opt.best_params]
```

New code (renames the dedup-by-fitness local to `dedup_key` to stop shadowing the trial's own
`"key"` field, and carries that trial key through):

```python
        last_gen_full_results = last_gen_full_results or {}
        seen, ranked = set(), []
        for r in sorted(opt.all_results or [], key=lambda r: (r.get("fitness") if r.get("fitness") is not None else -1e9), reverse=True):
            fit = r.get("fitness")
            dedup_key = round(fit, 6) if isinstance(fit, (int, float)) else _json.dumps(r.get("params"), sort_keys=True, default=str)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            ranked.append((r["params"], r.get("key")))
            if len(ranked) >= n:
                break
        if not ranked and opt.best_params:
            ranked = [(opt.best_params, None)]  # no trial key known -> always falls back to re-run
```

3c. `testplatform/ba2test_launcher.py:2484-2505` (the spec-building loop) — update the unpack
and split specs into "already have full results" vs. "need a re-run". Current code:

```python
        specs = []  # (rank, trial_cfg, strategy_params)
        for rank, params in enumerate(ranked, start=1):
            decoded = decode_params(strat, params)
            trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted)
            trial_cfg["name"] = f"TOP{rank}-{opt.name or expert}"
            trial_cfg["persist_trading_db"] = True
            strategy_params = dict(params)
            if decoded.get("entry_rules") is not None:
                strategy_params["entryRules"] = decoded["entry_rules"]
            if decoded.get("exit_rules") is not None:
                strategy_params["exitRules"] = decoded["exit_rules"]
            specs.append((rank, trial_cfg, strategy_params))
```

New code (splits into `ready` — persist immediately, no worker dispatch — and `specs` — the
existing re-run path, now only for what's missing):

```python
        ready = []  # (rank, trial_cfg, strategy_params, full_results) -- no re-run needed
        specs = []  # (rank, trial_cfg, strategy_params) -- must be re-run (existing path)
        for rank, (params, trial_key_) in enumerate(ranked, start=1):
            decoded = decode_params(strat, params)
            trial_cfg = _build_daily_trial_config(bt_block, decoded, hoisted)
            trial_cfg["name"] = f"TOP{rank}-{opt.name or expert}"
            trial_cfg["persist_trading_db"] = True
            strategy_params = dict(params)
            if decoded.get("entry_rules") is not None:
                strategy_params["entryRules"] = decoded["entry_rules"]
            if decoded.get("exit_rules") is not None:
                strategy_params["exitRules"] = decoded["exit_rules"]
            buffered = last_gen_full_results.get(trial_key_) if trial_key_ else None
            if buffered is not None:
                ready.append((rank, trial_cfg, strategy_params, buffered))
            else:
                specs.append((rank, trial_cfg, strategy_params))
        if ready:
            print(f"    {len(ready)}/{len(ranked)} top individual(s) available from the GA's "
                  f"last generation (no re-run); re-running the remaining {len(specs)}.")
```

3d. Persist the `ready` ones directly, right before the existing dispatch block (which now only
handles `specs`, the leftovers). Insert just above the `persisted = 0` line
(`testplatform/ba2test_launcher.py:2543`):

```python
        persisted = 0
        for rank, trial_cfg, strategy_params in [(r, c, s) for r, c, s, _ in ready]:
            pass  # placeholder removed below -- see actual insertion
```

(Replace the placeholder above with the real block — write it directly, no placeholder, shown
here separately only to make the insertion point unambiguous:)

```python
        persisted = 0
        for rank, trial_cfg, strategy_params, full_results in ready:
            if _persist_one(rank, trial_cfg, strategy_params, {"ok": True, "results": full_results}):
                persisted += 1
                print(f"    persisted TOP{rank} ({persisted}/{len(ranked)}) [no re-run]")
```

This reuses `_persist_one` UNCHANGED (it already accepts an `out` dict shaped
`{"ok": True, "results": {...}}` — that's exactly `_persist_trial_worker`'s return shape, which
is what we're constructing here from the buffer instead of an actual worker call).

Everything below this (the `n_local`, `remote_workers`, `ProcessPoolExecutor`/
`ThreadPoolExecutor` dispatch, the `else:` sequential branch) stays exactly as-is, just operating
on the now-possibly-empty `specs` list instead of all of `ranked`. When `specs` is empty (the
common case — everything came from the buffer), the `if (n_local > 1 or remote_workers) and
len(specs) > 1:` condition is false and the loop bodies simply don't execute (zero re-runs, zero
worker-pool spin-up, zero hang risk).

3e. Update both call sites to pass the buffer through.

`testplatform/ba2test_launcher.py:2236-2248` — current code:

```python
    res = handle_strategy_optimization("cli-optimize", {"optimization_id": opt_id})
    if res.get("status") != "completed":
        print(json.dumps(res, indent=2, default=str))
        sys.exit(f"ba2-test: optimization {opt_id} did not complete")

    # Re-run the best params as ONE tracked, tagged Backtest so it lands in runs/report.
    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        print(f"optimize: done. best_fitness={opt.best_fitness} best_params={json.dumps(opt.best_params, default=str)}")
    finally:
        db.close()
    nsaved = _persist_top_backtests(opt_id, expert, n=int(args.save_top), parallel=int(args.parallel))
```

New code (pops from the module-level scratch dict added in Task 2 — NOT from `res`, since `res`
never carries this data, by design, to avoid it leaking into `TaskQueue.result` for UI/API-driven
runs; see Task 2 step 3c):

```python
    res = handle_strategy_optimization("cli-optimize", {"optimization_id": opt_id})
    if res.get("status") != "completed":
        print(json.dumps(res, indent=2, default=str))
        sys.exit(f"ba2-test: optimization {opt_id} did not complete")

    # Re-run the best params as ONE tracked, tagged Backtest so it lands in runs/report.
    db = SessionLocal()
    try:
        opt = db.query(StrategyOptimization).filter(StrategyOptimization.id == opt_id).first()
        print(f"optimize: done. best_fitness={opt.best_fitness} best_params={json.dumps(opt.best_params, default=str)}")
    finally:
        db.close()
    from app.services.strategy_optimization_handler import _last_gen_full_results_by_opt
    nsaved = _persist_top_backtests(opt_id, expert, n=int(args.save_top), parallel=int(args.parallel),
                                     last_gen_full_results=_last_gen_full_results_by_opt.pop(opt_id, None))
```

Apply the identical change at the second call site, `testplatform/ba2test_launcher.py:2411`
(`_cmd_optimize_batch`) — find its preceding `handle_strategy_optimization(...)` call in the same
function and thread it the same way (import `_last_gen_full_results_by_opt` and `.pop(opt_id,
None)` it, same as above).

**Step 4: Run tests to verify they pass**

Run: `./venv/bin/python -m pytest tests/test_persist_top_backtests.py -v`
Expected: PASS — all tests, including the 3 new ones and the 2 pre-existing ones (which call
`_persist_top_backtests` without the new kwarg at all, proving the default `None` behaves
identically to today).

**Step 5: Commit**

```bash
git add testplatform/ba2test_launcher.py testplatform/backend/tests/test_persist_top_backtests.py
git commit -m "feat(ga): skip top-N re-run for individuals already captured in the last generation"
```

---

### Task 4: Manual end-to-end smoke test against a real grid job

**Files:** none (verification only)

**Step 1:** Run a small real optimize job (short population/generations so it finishes in a
couple minutes) against an already-warmed universe, e.g.:

```bash
ba2-test.exe optimize --expert FMPEarningsDrift --universe AAPL,MSFT,NVDA \
  --start 2024-01-01 --end 2024-06-01 --population 20 --generations 3 --parallel 2 \
  --fitness consistent_annual_return --name smoke-last-gen-capture --save-top 3
```

**Step 2:** Confirm the log prints `"N/3 top individual(s) available from the GA's last
generation (no re-run)"` with N > 0, and that the whole `optimize` command finishes noticeably
faster than before (no multi-minute re-run phase, no risk of the local
`ProcessPoolExecutor`/`ThreadPoolExecutor` hang observed on 2026-07-17).

**Step 3:** `ba2-test.exe runs list --group <opt_id>` — confirm N tagged, saved Backtests exist
with real (non-zero) trade counts, matching what `bestFitness`/`topIndividuals` reported live via
`GET /api/strategies/optimizations/{id}` during the run.

**Step 4:** No commit — this is a verification step, not a code change.

---

### Task 5: Docs + version bump

**Files:**
- Modify: `ba2_trade_platform/version.py`

**Step 1:** Bump `APP_VERSION`'s build number by 1 (per `CLAUDE.md`'s "before every push"
convention).

**Step 2: Commit**

```bash
git add ba2_trade_platform/version.py
git commit -m "chore: version bump for GA last-gen full-results capture"
```
