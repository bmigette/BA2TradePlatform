# Remote Result Sync for Backtest/Optimization — Design

## Problem

The testplatform's distributed worker system (`testplatform/backend/app/services/worker_client.py`,
`worker_server.py`, `distributed_eval.py`) already pushes cache *files* (OHLCV, screener metric
store) between the master and remote workers, checked with a CRC32 manifest diff
(`cache_sync.py`). But it never syncs the *results* it produces: `StrategyOptimization` and
`Backtest` rows live only in the master's own database. If the master's disk is lost, or you want
to inspect a run from a worker machine directly, there's no copy anywhere else.

We want every remote worker to hold its own replica of these result rows, kept current as jobs
run, so any worker host can independently browse and re-run past optimizations and backtests.

## Scope

Three tables get replicated: `Strategy`, `StrategyOptimization`, `Backtest`
(`testplatform/backend/app/models/{strategy,strategy_optimization,backtest}.py`). `Strategy`
syncs only as a dependency of the other two — a worker needs a copy of a strategy's parameter
space to decode genes and replay a run, but strategies themselves aren't the object of interest.

**Triggers:**

1. **Every GA generation** — `ga_callback` in `strategy_optimization_handler.py` pushes the
   current `StrategyOptimization` row (status, progress, best_fitness, best_params, all_results).
2. **Whenever a `Backtest.is_saved` flips to `True`** — the GA's top-N persist
   (`_persist_top_backtests`/`_persist_one` in `ba2test_launcher.py`), the
   `POST /api/backtests/{id}/save` endpoint (`backtests.py:1329`), and the CLI `--save` flag
   (`scripts/run_daily_backtest.py`) all funnel through one push call.
3. **On master restart**, after correcting any `StrategyOptimization` left stuck at
   `status='running'` by a crash (see below), the corrected row gets pushed too, so a worker's
   copy doesn't keep showing "running" forever.

**Fan-out:** every push goes to *all* `Worker` rows with sync enabled — not just the workers that
executed trials for that particular job. Sync is fleet-wide replication, independent of trial
routing.

**Out of scope for now:** dedicated retry queue for failed pushes (see Failure Handling);
automatic backfill of historical rows on deploy (see Migration & Rollout — a manual script covers
this instead).

**Known limitation:** `Backtest` has two engine types — `daily_expert` (the current expert-engine
path, and the only one this feature targets) and legacy `ml` (model-driven). A synced `ml`-engine
row still carries its raw `model_id`/`prediction_dataset_id`/`execution_dataset_id` columns
(the push client dumps every column on the row), but this feature doesn't sync `TrainedModel` or
`Dataset` — those ids are meaningless/dangling on the worker for that row. Deliberately not fixed:
building a sync path for the legacy `ml` engine's dependency graph is out of proportion to a path
that isn't the active target of this feature. If `ml`-engine backtests start getting synced in
practice (e.g. someone saves one via the API), revisit — either filter those columns out of the
payload for non-`daily_expert` rows, or extend replication to `TrainedModel`/`Dataset`.

## Data Model Changes

One new column, `testplatform/backend/app/models/worker.py`:

```python
sync_results_enabled = Column(Boolean, default=True)
```

One migration, following the existing house pattern
(`db_migrate/023_add_worker_password_and_optimization_workers.py`): idempotent
`ALTER TABLE workers ADD COLUMN`, guarded by a column-existence check, plus an explicit
`UPDATE workers SET sync_results_enabled = 1 WHERE sync_results_enabled IS NULL` so every
worker that already existed before this feature ships starts with sync **on** — belt-and-suspenders
alongside SQLite's own column-default backfill, in case a future migration author copies this
pattern onto a database engine where `ALTER TABLE ... ADD COLUMN ... DEFAULT` doesn't backfill
existing rows.

No schema changes needed on `Strategy`, `StrategyOptimization`, or `Backtest` — all three already
carry `name` and `created_at`, which is all the matching logic needs.

## Remote Storage

`worker_server.py` is deliberately DB-less today: it runs trials and returns results over HTTP,
nothing more. Rather than invent a second, separate database for synced rows, the new sync
endpoints reuse testplatform's own `app.models.database.SessionLocal` / `init_db()` — the exact
module `main.py`'s startup event already uses. That module resolves, by default, to
`TEST_DIR/dl_forecasting.db`: the same file a full `ba2-test serve` on that same host would open.

Practical effect: `worker_server.py` calls `init_db()` once, lazily, on the first sync request, to
ensure the three tables exist, then writes through that same session. If someone later starts the
full backend on that worker host, it opens the identical file and the synced rows are already
there in the normal UI — no import step. This is unrelated to the existing `/secrets` endpoint's
trick of pointing `ba2_common`'s separate ORM at the same file for provider API keys (a different
table, a different stack) — the two mechanisms just happen to share one SQLite file, which WAL
mode plus the existing 30-second busy-timeout (`database.py:45-54`) already handles.

## Matching and Upsert Semantics

Matching key: **`(name, created_at)`**, checked on the worker side, since that's where "don't
override" must be enforced. If a row with a matching name and created_at already exists locally,
update its mutable fields in place. Otherwise, insert a new row and let the worker's own database
assign a fresh local id — never reuse the master's id, since ids aren't stable across separate
databases.

Because every push carries the row's full current state, not a diff, a push that a temporarily
unreachable worker misses gets healed automatically by the next one — no separate reconciliation
pass is needed for that case.

### Cross-reference resolution

`Backtest.optimization_id` and `StrategyOptimization.strategy_id` are bare integers pointing at
the *master's* ids. Once a worker assigns its own local id on insert, those integers would point
at the wrong row, or no row, locally. Every sync payload therefore carries the natural key of its
parent alongside the raw id: syncing a `Backtest` includes `optimization_name` and
`optimization_created_at`, not just `optimization_id`; syncing a `StrategyOptimization` includes
`strategy_name` and `strategy_created_at`. Before inserting or updating a row, the worker resolves
`optimization_id`/`strategy_id` by looking up the parent locally via its natural key and
substitutes the local id. No id-mapping table required.

Not every such column tolerates a missing parent the same way: `Backtest.strategy_id` and
`Backtest.optimization_id` are nullable (a standalone backtest legitimately has no optimization),
but `StrategyOptimization.strategy_id` is `NOT NULL` in the schema — every optimization requires
a strategy. If the referenced parent hasn't synced yet (the strategy-first ordering below is meant
to prevent this, but a single push is fire-and-forget per worker, so a narrow race is possible: the
`Strategy` POST to a worker fails while the very next `StrategyOptimization` POST to the same
worker succeeds), resolving a required column to `None` would violate the column's constraint.
The worker-side upsert must check whether the column is nullable: if it is, substitute `None`
(exactly as above); if it isn't and the parent can't be resolved, skip the write entirely rather
than raise, logging that it was skipped. Nothing is lost — the next full-state push (next
generation, in this case) retries the parent first and self-heals.

This requires strict ordering on the master side: a parent is always synced before, or in the
same request as, its child. `push_strategy` runs before `push_optimization`; `push_optimization`
(and transitively `push_strategy`) runs before `push_backtest`.

## Implementation Surface

**New file**, `testplatform/backend/app/services/sync_client.py`, mirroring `worker_client.py`'s
style: `push_strategy(strat)`, `push_optimization(opt, db)`, `push_backtest(bt, db)`. Each
resolves parent rows and their natural keys, iterates `Worker` rows where
`is_enabled and sync_results_enabled`, and POSTs to that worker — catching and logging any
per-worker failure without raising, so one unreachable worker never blocks the caller.

**New endpoints on `worker_server.py`**: `POST /sync/strategy`, `/sync/optimization`,
`/sync/backtest` — bearer-checked like the existing endpoints, each performing the
upsert-by-natural-key logic above.

**Four hook points**, all fire-and-forget:

- `ga_callback` (`strategy_optimization_handler.py`) — `push_optimization(opt, db)` at each
  generation boundary.
- `_persist_one`, inside `_persist_top_backtests` (`ba2test_launcher.py`) — `push_backtest(bt, db)`
  right after each top-N row commits.
- `POST /api/backtests/{id}/save` (`backtests.py:1329`) — `push_backtest(backtest, db)` after
  `db.commit()`.
- The CLI `--save` path in `scripts/run_daily_backtest.py` — same call after persist.

All four read `Worker.sync_results_enabled` live, through `sync_client.py`, at call time. There is
no separate "is this feature on" flag — the per-worker column *is* the switch.

## Crash Recovery

Today, `recover_interrupted_jobs()` (`main.py:354`) already marks `TaskQueue` rows crashed
mid-run as `stopped` on restart, but it never touches the parent `StrategyOptimization.status` —
a crashed run stays reported as `running` forever, even locally, regardless of this feature.

Fix, as a prerequisite: extend `recover_interrupted_jobs()` so that for each `TaskQueue` row with
`task_type == 'strategy_optimization'` found `RUNNING` at startup, it pulls `optimization_id` from
that row's `payload`, loads the matching `StrategyOptimization`, and if its status is still
`running`, sets `status='failed'`, `error_message='Interrupted by server restart'`, and commits.
This reuses the existing status enum (`pending`/`running`/`completed`/`failed`) rather than adding
a new value — resuming, where supported, already keys off `TaskQueue.status == 'stopped'` plus its
checkpoint data, independently of this field.

Each corrected row is then pushed via `push_optimization`, exactly like any other optimization
sync, so remote copies stop showing `running` too.

## Failure Handling

A push failure — offline worker, timeout, connection refused — is logged and otherwise ignored.
Nothing blocks or fails the optimization or backtest job it's attached to. Since every push sends
full current state, the next natural sync point (next generation, next saved backtest, or the next
job's startup reconciliation) automatically re-sends anything a temporarily offline worker missed.
No dedicated retry queue.

## Migration and Rollout

The migration sets `sync_results_enabled = True` for every existing worker (see Data Model
Changes above) — sync turns on by default for the whole existing fleet the moment this ships.

Pre-existing saved backtests and completed optimizations — rows that existed before this feature
shipped — do not get backfilled automatically. Instead, a manual script,
`testplatform/backend/scripts/sync_backfill.py`, iterates existing `is_saved=True` Backtest rows
and terminal-status `StrategyOptimization` rows and calls the same `push_backtest`/
`push_optimization` functions. Run it once, on demand, if you want history replicated too.

## Testing Plan

- **Unit tests**: natural-key matching (insert-if-absent / update-if-present) against a temp
  SQLite file, independent of HTTP; cross-reference resolution with deliberately mismatched
  local/remote ids, to prove the remap works; the crash-recovery status fix (seed a `RUNNING`
  TaskQueue row and a `running` StrategyOptimization, call the startup function, assert
  `status == 'failed'` and that a push was attempted).
- **Integration test**: start a real `worker_server.py` instance, push a
  Strategy → StrategyOptimization → Backtest chain end-to-end, then open a second `SessionLocal`
  against that same file and confirm the rows are queryable exactly as the full backend would see
  them.
- **Live smoke test**: run a small real optimization job against a real remote worker, confirm
  generation-by-generation sync lands, confirm top-N backtests sync, kill the master mid-run,
  restart, and confirm the remote's copy of that run flips to `failed`.
