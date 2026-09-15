# Scheduled expert priority

Implemented 2026-09-15. Configure **Priority** under the expert's General Settings.
Higher numbers take precedence. The default is **1**.

## Behavior

Experts scheduled at the same instant on the same account form one group. All
members are registered before any work starts. Higher-priority work is dequeued
first; other experts can analyze in parallel when workers are available. Their
recommendation processing waits until every higher-priority member has finished
its analysis and order-processing work, including the Smart Risk Manager and
its scheduled position-management pass. It does not wait for broker fills.

Experts that submit orders directly inside their analysis wait before that
analysis starts. Equal priorities keep normal queue fairness. Different accounts
and scheduled instants have no priority dependency. Manual runs and continuously
running experts are outside this scheduled-group feature.

The queue displays **Waiting for priority** for completed analyses awaiting a
higher-priority expert, and shows expert priority separately from the queue's
internal task ordering. Empty or failed expansions and cancelled queued tasks
release their group membership. Waiting does not occupy an analysis worker.

The scheduler uses its actual scheduled fire time, including delayed/coalesced
callbacks. Account refresh and IV snapshot jobs retain their own scheduling.
Priorities are captured for each group, so changing a setting affects future runs.

An expert scheduled at 10:30 does not acquire a priority dependency on a 09:30
expert that is still analyzing, even on the same day and account. Queue priority
and the completion barrier both use the exact scheduled time plus account.
Ordinary worker availability, existing order-processing locks and account buying
power can still affect execution. A later run is not reserved capital.

A Monday-only priority-100 expert is excluded from Tuesday's scheduled group.
The key includes the full date as well as time: even an unfinished Monday run
does not create a priority dependency for Tuesday's experts at the same hour.

## Compatibility and recovery

- Existing database rows and new experts default to 1.
- Legacy imports with an omitted or null priority create new experts at 1.
- Importing an older file over an existing expert preserves its current priority.
- Export and clone operations preserve the configured priority.
- No backtest decision or sizing algorithm changed. Six existing golden parity
  checks pass. Account competition can intentionally change which live expert
  gets scarce capital first.
- Completed analyses waiting for priority retain their queue recovery records.
  Explicit queue restore reconstructs scheduled groups before submitting work,
  using current priorities. Recovery retains the platform's existing re-analysis
  semantics; it is not an exactly-once trade replay system.
- There is no timeout that silently bypasses a running higher-priority expert.
  A truly stuck running analysis must be investigated before its dependents proceed.

## Migration and local deployment

Alembic revision: `d9e3b72a10fc` (parent `c8f2a41d67be`). Adds the non-null integer
`expertinstance.priority`, with database default 1. The checked migration utility
is `tools/migrations/apply_expert_priority.py`; it verifies the current revision,
takes a SQLite backup including WAL data, applies the migration, and optionally
sets a priority after checking both the expert ID and alias.

Applied and independently verified on 2026-09-15:

| Database | Result |
| --- | --- |
| `C:/Users/basti/Documents/ba2/trade/db.sqlite` | 26 experts at priority 1 |
| `C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite` | Expert 11 `goal2020-mid_ED_S1top1` at 100; eight other experts at 1 |

Backups are beside the databases:

- Development: `db.sqlite.bak-expert-priority-20260915T202627381681Z`
- Production: `db.sqlite.bak-expert-priority-20260915T202646752881Z`

Existing app processes need a restart to load the new scheduler code. This change
does not restart applications, submit trades, commit, or push automatically.

## Validation

Result: **91 focused tests passed**. The final backend golden and margin parity
gate passed **24 tests**, with the **2 existing expected failures** unchanged.
The subsequent unscheduled-day check passed **54 priority/scheduler tests**,
including Monday-only exclusion on Tuesday and an unfinished previous-day run.

The focused suite covers legacy imports and tasks, default/model migration,
concurrent completions, low-priority analysis finishing first, expansion children,
Smart RM completion and recovery dispatch, self-trading experts, cancellation,
failed/empty runs, entry-before-management, equal priorities, separate accounts
and times, the real APScheduler executor, maintenance-job exclusion, and the
existing order-processing/deadlock regressions. The backend golden parity suite
also passes. Tests use mocked accounts and never submit broker orders.
