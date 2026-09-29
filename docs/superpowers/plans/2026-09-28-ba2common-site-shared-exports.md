# ba2_common shared exports (site P0a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the four pieces of BA2 logic the public site needs (the backtest export payload, live performance metrics, the rules/settings decoding behind the `expert_batch` export, and the deploy-payload entry) out of the two apps into `ba2_common` as pure functions. The apps delegate to them and their output is byte-identical.

**Architecture:** Each piece becomes a pure function in `packages/common/ba2_common/` that takes plain values or duck-typed row objects (no DB, no filesystem, no app imports). The existing app functions keep their names and signatures. They resolve their DB-bound inputs and then call the shared function. There are **no module-aliasing shims** (see `docs/2026-08-17-alias-shim-race.md`). Behavior is pinned **before** each move by golden or characterization tests, then re-checked after it.

**Tech Stack:** Python 3.12, SQLModel/SQLAlchemy, pytest, import-linter. Venvs: `~/ba2-venvs/trade` (repo-root `tests/` and `packages/*/tests`) and `~/ba2-venvs/test` (`testplatform/backend/tests`).

**Spec:** `/Users/bmigette/Documents/dev/BA2/BA2TradePlatform-site/docs/superpowers/specs/2026-09-28-ba2-site-design.md` (§2.2).

## Global Constraints

- Work in the worktree `/Users/bmigette/Documents/dev/BA2/BA2TradePlatform/.worktrees/site-shared-exports`, on branch `feat/site-shared-exports`, which was cut from `dev` at `9ed2c553`. Never push without the user's approval. Never touch the main checkout at `/Users/bmigette/Documents/dev/BA2/BA2TradePlatform`: another session works there.
- `ba2_common` must not import `ba2_providers`, `ba2_experts` or `ba2_trade_platform` (enforced by `packages/common/.importlinter`).
- No module-aliasing shims (`sys.modules[__name__] = …`). An app keeps a thin wrapper function, or a plain `from ba2_common… import name` binding.
- Output must be **byte-identical**: the same dict content and the same key insertion order as before, for every existing caller.
- Changed error types stay invisible to existing callers. The test-platform endpoint still raises `HTTPException(400)` exactly where it did before, and no new `ValueError`→400 conversions appear.
- Run commands from the worktree root `/Users/bmigette/Documents/dev/BA2/BA2TradePlatform/.worktrees/site-shared-exports` unless a step says otherwise. pytest there imports the worktree's code (verified); standalone scripts must put the worktree's `packages/*` first on `sys.path` themselves.
- A copy of the user's live-trade DB backup is at `/tmp/ba2-backups/dev_2026-09-28.sqlite`. It contains API keys: never commit it, print its `appsetting`/`accountsetting` values, or point a tool at it directly. Always work on a fresh copy.
- Commit messages end with: `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
- Before any `git push` (only with the user's approval), bump the build number in BOTH `testplatform/version.py` (`TEST_APP_VERSION`, since `packages/` changed) and `ba2_trade_platform/version.py` (`APP_VERSION`, since `ba2_trade_platform/` changed), per `CLAUDE.md`.

## Review Focus

- **The real test DB:** every one of its ~588 backtests must still export identically for both kinds. The golden fixtures cover a sample; the Task 1/Task 8 capture-and-diff over the real DB copy covers them all.
- **A screener-optimized FactorRanker run with `apply_to_expert_settings`:** the bypass overlay must still apply, lazily, only on that branch (Task 3 golden case plus a pure test with `bypass_check` returning True/False).
- **Transactions with `close_price == 0.0`, such as expired worthless options:** they must count as a measured P&L, not a missing one. Pinned in Task 4's characterization and pure tests.
- **Legacy stored setting spellings** (bool `"1"`, int in `value_str`, the string `"None"`) must decode as before. Pinned in Task 5.
- **Rulesets whose rules have generic names** (`rule 1`, empty) must still get generated display names on export. Pinned in Task 6.

---

## File map

| File | Change | Responsibility |
|---|---|---|
| `packages/common/ba2_common/core/schedule_genes.py` | create | Schedule gene constants, repair rule, `schedule_override_from_genes` |
| `testplatform/backend/app/services/strategy_param_space.py` | modify | Import the schedule pieces from ba2_common instead of defining them |
| `packages/common/ba2_common/export/__init__.py` | create | Package marker |
| `packages/common/ba2_common/export/backtest_export.py` | create | `derive_export_payload`, `needs_legacy_reconstruction`, `build_deploy_entry`, errors |
| `testplatform/backend/app/api/backtests.py` | modify | `_derive_export_payload` becomes a wrapper |
| `tools/export_deploy_payload.py` | modify | Use `build_deploy_entry` |
| `tools/export_golden_capture.py` | create | Capture every backtest's two export payloads from a DB copy (before/after diff) |
| `testplatform/backend/tests/fixtures/export_golden/make_cases.py` | create | Build golden cases from a DB copy |
| `testplatform/backend/tests/fixtures/export_golden/cases.json` | create (generated) | Golden inputs |
| `testplatform/backend/tests/fixtures/export_golden/golden/*.json` | create (generated) | Golden outputs |
| `testplatform/backend/tests/test_export_payload_golden.py` | create | Golden test |
| `packages/common/ba2_common/analytics/__init__.py` | create | Package marker |
| `packages/common/ba2_common/analytics/performance.py` | create | Metric functions, `expert_performance`, `monthly_pnl` |
| `ba2_trade_platform/ui/components/performance_charts.py` | modify | Re-bind the metric names from ba2_common |
| `ba2_trade_platform/ui/pages/performance.py` | modify | Use `expert_performance` and `monthly_pnl` |
| `packages/common/ba2_common/core/interfaces/ExtendableSettingsInterface.py` | modify | Add `decode_setting_rows`; the `settings` property uses it |
| `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py` | modify | Add `enabled_instruments_config`; the method uses it |
| `packages/common/ba2_common/core/rules_export_import.py` | modify | Add `ruleset_export_body` and `rulesets_export_envelope`; the exporter uses them |
| `packages/common/ba2_common/export/expert_batch.py` | create | `expert_batch` v1.0 constants, entry builder, envelope |
| `ba2_trade_platform/core/expert_batch_export_import.py` | modify | `_export_one` and `build_batch_export` delegate |
| Tests under `packages/common/tests/` and `tests/` | create | See each task |

---

### Task 1: Pin the backtest export with golden fixtures

**Files:**
- Create: `tools/export_golden_capture.py`, `tools/live_export_capture.py`
- Create: `testplatform/backend/tests/fixtures/export_golden/make_cases.py`
- Create (generated): `testplatform/backend/tests/fixtures/export_golden/cases.json`, `…/golden/*.json`
- Create: `testplatform/backend/tests/test_export_payload_golden.py`

**Interfaces:**
- Consumes: `app.api.backtests._derive_export_payload(backtest, kind, db)` (unchanged in this task).
- Produces: the golden test `tests/test_export_payload_golden.py` (Tasks 2 and 3 must keep it green), plus `/tmp/ba2-export-before.json` and `/tmp/ba2-live-before.json` (both diffed in Task 8).

- [ ] **Step 1: Confirm the worktree and make a private copy of the test DB**

The branch and worktree already exist. Do not run `git checkout` or `git pull`.

```bash
cd /Users/bmigette/Documents/dev/BA2/BA2TradePlatform/.worktrees/site-shared-exports && git branch --show-current   # feat/site-shared-exports
~/ba2-venvs/test/bin/python - <<'EOF'
import sqlite3
src = sqlite3.connect("file:/Users/bmigette/Documents/ba2/test/dl_forecasting.db?mode=ro", uri=True)
dst = sqlite3.connect("/tmp/ba2-test-copy.db")
src.backup(dst); dst.close(); src.close()
print("copied")
EOF
```
Expected: `copied`. The copy is the only DB the capture tools touch.

- [ ] **Step 2: Write the capture tool**

`tools/export_golden_capture.py`:

```python
"""Capture BOTH export payloads (expert_settings, ruleset) for EVERY backtest in a test DB.

Used to prove a refactor of the export derivation is byte-identical: run it before the change
and after, then diff the two files. Point it at a COPY of the test DB -- the test app's engine
sets WAL pragmas on connect.

Usage:
    DATABASE_URL=sqlite:////tmp/ba2-test-copy.db \
      ~/ba2-venvs/test/bin/python tools/export_golden_capture.py /tmp/out.json
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "testplatform", "backend")
# THIS checkout's packages first: the venv's editable installs may point at another checkout.
for p in (BACKEND, os.path.join(REPO, "testplatform"),
          *(os.path.join(REPO, "packages", n) for n in ("experts", "providers", "common"))):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(BACKEND)

if not os.environ.get("DATABASE_URL"):
    sys.exit("Set DATABASE_URL to a COPY of the test DB (sqlite:////tmp/ba2-test-copy.db)")

import app.models  # noqa: F401,E402  -- registers every model
from app.api.backtests import _derive_export_payload  # noqa: E402
from app.models.backtest import Backtest  # noqa: E402
from app.models.database import SessionLocal  # noqa: E402


def main() -> int:
    import ba2_common
    print(f"ba2_common from {ba2_common.__file__}")
    out_path = sys.argv[1]
    db = SessionLocal()
    out = {}
    try:
        ids = [row[0] for row in db.query(Backtest.id).order_by(Backtest.id).all()]
        for bt_id in ids:
            bt = db.query(Backtest).filter(Backtest.id == bt_id).first()
            entry = {}
            for kind in ("expert_settings", "ruleset"):
                try:
                    entry[kind] = _derive_export_payload(bt, kind, db)
                except Exception as e:  # noqa: BLE001 -- the error IS the captured behaviour
                    entry[kind] = {"__error__": type(e).__name__,
                                   "detail": str(getattr(e, "detail", e))}
            out[str(bt_id)] = entry
    finally:
        db.close()
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True, default=str)
    print(f"captured {len(out)} backtests -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Capture the BEFORE state**

```bash
DATABASE_URL=sqlite:////tmp/ba2-test-copy.db ~/ba2-venvs/test/bin/python tools/export_golden_capture.py /tmp/ba2-export-before.json
```
Expected: `ba2_common from …/.worktrees/site-shared-exports/packages/common/…` (the worktree, not the main checkout), then `captured N backtests -> /tmp/ba2-export-before.json`, where N equals the row count (588 on this Mac). Keep the file for Task 8.

- [ ] **Step 3b: Write the live capture tool and capture the live BEFORE state**

`tools/live_export_capture.py`:

```python
"""Capture the live app's expert_batch export (every instance) and the Performance page's
per-expert and monthly metrics from a COPY of a live trade DB. Run before and after a refactor
of those code paths, then diff. The tool writes to its copy (WAL); never point it at a real DB.

Usage:
    cp /tmp/ba2-backups/dev_2026-09-28.sqlite /tmp/ba2-live-copy.sqlite
    BA2_HOME=/tmp/ba2-backups/home DB_FILE=/tmp/ba2-live-copy.sqlite \
      ~/ba2-venvs/trade/bin/python tools/live_export_capture.py /tmp/out.json
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# THIS checkout's code first: the venv's editable installs may point at another checkout.
for p in (*(os.path.join(REPO, "packages", n) for n in ("experts", "providers", "common")), REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

DB = os.environ.get("DB_FILE")
if not DB:
    sys.exit("Set DB_FILE to a COPY of a live trade DB")

from ba2_trade_platform.core.seam_wiring import wire_all_seams  # noqa: E402
wire_all_seams()
from ba2_common.core import db  # noqa: E402
db.configure_db(DB)

import ba2_common  # noqa: E402
from sqlmodel import select  # noqa: E402
from ba2_trade_platform.core.expert_batch_export_import import build_batch_export  # noqa: E402
from ba2_trade_platform.core.models import ExpertInstance, Transaction  # noqa: E402
from ba2_trade_platform.core.types import TransactionStatus  # noqa: E402
from ba2_trade_platform.ui.pages.performance import PerformanceTab  # noqa: E402

_NORMALIZED = "<normalized>"


def main() -> int:
    print(f"ba2_common from {ba2_common.__file__}")
    with db.get_db() as s:
        ids = [e.id for e in s.exec(select(ExpertInstance).order_by(ExpertInstance.id)).all()]
        closed = s.exec(select(Transaction).where(Transaction.status == TransactionStatus.CLOSED)
                        .order_by(Transaction.id)).all()
    batch = build_batch_export(ids)
    batch["export_timestamp"] = _NORMALIZED
    for entry in batch["experts"]:
        if entry.get("rulesets"):
            entry["rulesets"]["export_timestamp"] = _NORMALIZED
    tab = PerformanceTab(None)
    metrics = tab._calculate_transaction_metrics(closed)
    for m in metrics.values():
        m.pop("transactions", None)
    monthly = {month: {name: dict(v) for name, v in per.items()}
               for month, per in tab._calculate_monthly_metrics(closed).items()}
    with open(sys.argv[1], "w") as f:
        json.dump({"batch": batch, "metrics": metrics, "monthly": monthly}, f, indent=1,
                  sort_keys=True, default=str)
    print(f"captured {len(ids)} experts, {len(closed)} closed transactions -> {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

```bash
cp /tmp/ba2-backups/dev_2026-09-28.sqlite /tmp/ba2-live-copy.sqlite
BA2_HOME=/tmp/ba2-backups/home DB_FILE=/tmp/ba2-live-copy.sqlite ~/ba2-venvs/trade/bin/python tools/live_export_capture.py /tmp/ba2-live-before.json 2>&1 | grep -v "^20[0-9][0-9]-"
rm -f /tmp/ba2-live-copy.sqlite*
```
Expected: `ba2_common from …/.worktrees/site-shared-exports/…`, then `captured 28 experts, 42 closed transactions -> /tmp/ba2-live-before.json`. Keep the file for Task 8. Do not commit it: it may contain expert settings but never keys, and it is still private data.

- [ ] **Step 4: Write the case builder**

`testplatform/backend/tests/fixtures/export_golden/make_cases.py`:

```python
"""Build tests/fixtures/export_golden/cases.json from a COPY of the test DB.

Cases: for every distinct expert_name, the lowest-id optimization-linked backtest (its
optimization's run-level `backtest` block is embedded), the lowest-id row whose optimization
carries a screener_opt block (if any), plus two synthetic rows derived from the first case:
`standalone_fallback` (no optimization link) and `unified_rules` (legacy trees converted to
entryRules/exitRules). Re-run only when intentionally re-baselining.

Usage (from testplatform/backend):
    ~/ba2-venvs/test/bin/python tests/fixtures/export_golden/make_cases.py /tmp/ba2-test-copy.db
"""
import json
import os
import sqlite3
import sys

from ba2_common.core.rule_models import trade_rules_from_legacy

HERE = os.path.dirname(os.path.abspath(__file__))
FIELDS = ("id", "name", "expert_name", "engine_type", "strategy_params", "start_date",
          "end_date", "initial_capital", "optimization_id")


def _row_case(con, row, case_id):
    bt = dict(zip(FIELDS, row))
    bt["strategy_params"] = json.loads(bt["strategy_params"] or "{}")
    opt_block = None
    strategy_name = None
    if bt["optimization_id"] is not None:
        o = con.execute("select optimization_config, strategy_id from strategy_optimizations "
                        "where id=?", (bt["optimization_id"],)).fetchone()
        if o is not None:
            cfg = json.loads(o[0] or "{}")
            opt_block = cfg.get("backtest")
            s = con.execute("select name from strategies where id=?", (o[1],)).fetchone()
            strategy_name = s[0] if s else None
    return {"case_id": case_id, "backtest": bt, "opt_backtest_block": opt_block,
            "strategy_name": strategy_name}


def main() -> int:
    con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    cols = ", ".join(FIELDS)
    cases = []
    for (expert,) in con.execute("select distinct expert_name from backtests "
                                 "where expert_name is not null order by expert_name"):
        row = con.execute(f"select {cols} from backtests where expert_name=? and "
                          f"optimization_id is not null order by id limit 1", (expert,)).fetchone()
        if row:
            cases.append(_row_case(con, row, f"opt_{expert}"))
    for row in con.execute(f"select {cols} from backtests where optimization_id is not null "
                           f"order by id"):
        o = con.execute("select optimization_config from strategy_optimizations where id=?",
                        (row[FIELDS.index("optimization_id")],)).fetchone()
        if o and "screener_opt" in json.loads(o[0] or "{}").get("backtest", {}):
            cases.append(_row_case(con, row, "opt_screener"))
            break
    base = cases[0]
    standalone = json.loads(json.dumps(base))
    standalone["case_id"] = "standalone_fallback"
    standalone["backtest"]["optimization_id"] = None
    standalone["opt_backtest_block"] = None
    sp = standalone["backtest"]["strategy_params"]
    sp.update({"universe": {"mode": "static", "symbols": ["AAPL", "MSFT"]}, "seed": 7,
               "fillModel": "next_open", "warmupDays": 20, "commission": 1.0,
               "slippage": 5, "enableShort": False, "executionInterval": "1d"})
    cases.append(standalone)
    unified = json.loads(json.dumps(base))
    unified["case_id"] = "unified_rules"
    usp = unified["backtest"]["strategy_params"]
    conv = trade_rules_from_legacy(
        buy_tree=usp.pop("buyEntryConditions", None), sell_tree=usp.pop("sellEntryConditions", None),
        entry_actions=usp.pop("entryActions", None), exit_conditions=usp.pop("exitConditions", None))
    usp["entryRules"], usp["exitRules"] = conv["entry_rules"], conv["exit_rules"]
    cases.append(unified)
    with open(os.path.join(HERE, "cases.json"), "w") as f:
        json.dump(cases, f, indent=1, sort_keys=True, default=str)
    print(f"wrote {len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Generate cases.json**

```bash
cd testplatform/backend && ~/ba2-venvs/test/bin/python tests/fixtures/export_golden/make_cases.py /tmp/ba2-test-copy.db; cd -
```
Expected: `wrote K cases` with K ≥ 3. Open `cases.json` and confirm it contains no secrets: only genes, rules, universes and account execution settings (commission, slippage, fill model). If a key looks like a credential, delete that key from the file by hand.

- [ ] **Step 6: Write the golden test**

`testplatform/backend/tests/test_export_payload_golden.py`:

```python
"""Golden pin for _derive_export_payload (both kinds). The derivation moves to ba2_common
(site plan P0a); these goldens were captured from the pre-move code and must never change
unless a behaviour change is intended. Re-baseline: BA2_UPDATE_GOLDEN=1 pytest this file."""
import json
import os
from datetime import datetime

import pytest

from app.api.backtests import _derive_export_payload
from app.models.backtest import Backtest
from app.models.database import Base, SessionLocal, engine
from app.models.strategy import Strategy
from app.models.strategy_optimization import StrategyOptimization

HERE = os.path.join(os.path.dirname(__file__), "fixtures", "export_golden")
CASES = json.load(open(os.path.join(HERE, "cases.json")))
UPDATE = os.environ.get("BA2_UPDATE_GOLDEN") == "1"


@pytest.fixture(scope="module", autouse=True)
def _host_db():
    Base.metadata.create_all(bind=engine)
    yield


def _dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


def _run(case, kind):
    db = SessionLocal()
    try:
        fields = dict(case["backtest"])
        if case["opt_backtest_block"] is not None:
            strat = Strategy(name=f"golden-{case['case_id']}-{kind}", entry_rules=[], exit_rules=[])
            db.add(strat); db.commit(); db.refresh(strat)
            opt = StrategyOptimization(
                strategy_id=strat.id, name=f"golden-{case['case_id']}-{kind}",
                fitness_metric="sharpe", optimization_type="genetic",
                optimization_config={"backtest": case["opt_backtest_block"]},
                all_results=[], best_params={}, best_fitness=0.0, status="completed")
            db.add(opt); db.commit(); db.refresh(opt)
            fields["optimization_id"] = opt.id
        fields["start_date"] = _dt(fields["start_date"])
        fields["end_date"] = _dt(fields["end_date"])
        bt = Backtest(**fields)
        try:
            payload = _derive_export_payload(bt, kind, db)
        except Exception as e:  # noqa: BLE001
            payload = {"__error__": type(e).__name__, "detail": str(getattr(e, "detail", e))}
        # backtest_id is the ORIGINAL row id carried in the case; optimization ids are
        # host-DB artefacts and are not part of any payload.
        return json.dumps(payload, indent=1, sort_keys=True, default=str)
    finally:
        db.close()


@pytest.mark.parametrize("case", CASES, ids=[c["case_id"] for c in CASES])
@pytest.mark.parametrize("kind", ["expert_settings", "ruleset"])
def test_export_payload_matches_golden(case, kind):
    got = _run(case, kind)
    path = os.path.join(HERE, "golden", f"{case['case_id']}__{kind}.json")
    if UPDATE:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(got)
    assert got == open(path).read()
```

- [ ] **Step 7: Generate the goldens from the unmodified code, then run the test**

```bash
cd testplatform/backend
BA2_UPDATE_GOLDEN=1 ~/ba2-venvs/test/bin/python -m pytest tests/test_export_payload_golden.py -q
~/ba2-venvs/test/bin/python -m pytest tests/test_export_payload_golden.py -q
cd -
```
Expected: both runs PASS, with 2×K tests.

- [ ] **Step 8: Commit**

```bash
git add tools/export_golden_capture.py tools/live_export_capture.py testplatform/backend/tests/fixtures/export_golden testplatform/backend/tests/test_export_payload_golden.py docs/superpowers/plans/2026-09-28-ba2common-site-shared-exports.md
git commit -m "test(export): golden pin for _derive_export_payload before the ba2_common move

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Move the schedule-gene helpers to ba2_common

**Files:**
- Create: `packages/common/ba2_common/core/schedule_genes.py`
- Modify: `testplatform/backend/app/services/strategy_param_space.py:76-106` (constants and `_repair_no_weekday`) and `:1017-1067` (`schedule_override_from_genes`)
- Test: `packages/common/tests/test_schedule_genes.py`

**Interfaces:**
- Produces: `ba2_common.core.schedule_genes.SCHEDULE_DAYS: tuple[str, ...]`, `WEEKDAYS: tuple[str, ...]`, `repair_no_weekday(days: dict[str, bool], option_run: bool) -> dict[str, bool]`, `schedule_override_from_genes(strategy_params, base_override=None, weekdays_only=False, option_run=False) -> dict | None`.

- [ ] **Step 1: Write the failing test**

`packages/common/tests/test_schedule_genes.py`:

```python
from ba2_common.core.schedule_genes import (
    SCHEDULE_DAYS, WEEKDAYS, repair_no_weekday, schedule_override_from_genes,
)


def test_constants():
    assert SCHEDULE_DAYS == ("monday", "tuesday", "wednesday", "thursday", "friday",
                             "saturday", "sunday")
    assert WEEKDAYS == SCHEDULE_DAYS[:5]


def test_no_schedule_genes_returns_none():
    assert schedule_override_from_genes({"model:x": 1}) is None
    assert schedule_override_from_genes(None) is None


def test_genes_replace_days_and_keep_base_times():
    sp = {"schedule:thursday": 1, "schedule:monday": 0}
    out = schedule_override_from_genes(sp, {"times": ["10:00"]})
    assert out["times"] == ["10:00"]
    assert out["days"]["thursday"] is True and out["days"]["monday"] is False


def test_default_times_when_base_has_none():
    assert schedule_override_from_genes({"schedule:friday": 1})["times"] == ["09:30"]


def test_weekend_only_genome_deploys_as_monday_under_weekdays_only():
    out = schedule_override_from_genes({"schedule:saturday": 1}, weekdays_only=True)
    assert out["days"]["monday"] is True
    assert out["days"]["saturday"] is False


def test_equity_repair_only_when_all_seven_off():
    days = {d: False for d in SCHEDULE_DAYS}
    days["sunday"] = True
    assert repair_no_weekday(dict(days), option_run=False)["monday"] is False
    assert repair_no_weekday(dict(days), option_run=True)["monday"] is True
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_schedule_genes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ba2_common.core.schedule_genes'`.

- [ ] **Step 3: Create the module**

Create `packages/common/ba2_common/core/schedule_genes.py`. Move in, **verbatim** (docstrings included):
- `SCHEDULE_DAYS`, from `strategy_param_space.py` line 76.
- `_WEEKDAYS`, renamed to public `WEEKDAYS`, from line 80.
- `_repair_no_weekday`, renamed to public `repair_no_weekday`, from lines 83-106. Inside the body, replace `_WEEKDAYS` with `WEEKDAYS`.
- `schedule_override_from_genes`, from lines 1017-1067. Inside the body, replace `_WEEKDAYS` with `WEEKDAYS` and `_repair_no_weekday` with `repair_no_weekday`.

The module header:

```python
"""Schedule genes (``schedule:<day>``): the cadence a stored genome actually ran with.

Moved from testplatform/backend/app/services/strategy_param_space.py (2026-09) so exports
built outside the test app (the public site) reconstruct the same run_schedule_override.
Pure: no DB, no app imports.
"""
from typing import Any, Dict, Optional
```

- [ ] **Step 4: Make strategy_param_space import them**

In `strategy_param_space.py`, delete the four definitions you moved and add, next to the existing `ba2_common` imports (after line 71):

```python
from ba2_common.core.schedule_genes import (  # noqa: F401 -- re-bound for existing callers
    SCHEDULE_DAYS,
    WEEKDAYS as _WEEKDAYS,
    repair_no_weekday as _repair_no_weekday,
    schedule_override_from_genes,
)
```
Keep the `INERT_RM_TOGGLES` block that followed `schedule_override_from_genes` exactly where it is.

- [ ] **Step 5: Run the new test and the strategy_param_space suites**

```bash
~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_schedule_genes.py -q
cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests -q -k "strategy_param_space or schedule or golden" ; cd -
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/common/ba2_common/core/schedule_genes.py packages/common/tests/test_schedule_genes.py testplatform/backend/app/services/strategy_param_space.py
git commit -m "refactor(common): schedule-gene helpers move to ba2_common.core.schedule_genes

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Move the backtest export derivation to ba2_common

**Files:**
- Create: `packages/common/ba2_common/export/__init__.py`, `packages/common/ba2_common/export/backtest_export.py`
- Modify: `testplatform/backend/app/api/backtests.py:1173-1446` (`_derive_export_payload`)
- Modify: `tools/export_deploy_payload.py:82-91` (the entry dict literal)
- Test: `packages/common/tests/test_backtest_export.py`; the golden test from Task 1 stays green.

**Interfaces:**
- Consumes: `ba2_common.core.schedule_genes.schedule_override_from_genes` (Task 2).
- Produces:
  - `ba2_common.export.backtest_export.derive_export_payload(backtest, kind, *, opt_backtest_block=None, bypass_check=None, reconstruct_legacy_ruleset=None) -> dict`. `backtest` is any object with attributes `id, name, expert_name, engine_type, strategy_params, start_date, end_date, initial_capital`.
  - `needs_legacy_reconstruction(strategy_params) -> bool`
  - `build_deploy_entry(*, backtest_id, target_instance_id, account_id, virtual_equity_pct, expert_name, label, ruleset, settings) -> dict`
  - `class ExportRefused(ValueError)`, `class UnsupportedExportKind(ValueError)`
  - `EXPORT_KINDS = ("expert_settings", "ruleset")`

- [ ] **Step 1: Write the failing pure tests**

`packages/common/tests/test_backtest_export.py`:

```python
from datetime import datetime
from types import SimpleNamespace

import pytest

from ba2_common.core.deploy_parity import BacktestRunFacts, forced_expert_settings
from ba2_common.export import backtest_export as be


def _bt(**kw):
    base = dict(id=11, name="TOP1", expert_name="FMPRating", engine_type="daily_expert",
                strategy_params={"model:profit_ratio": 1.2}, start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 6, 1), initial_capital=10_000.0)
    base.update(kw)
    return SimpleNamespace(**base)


GATES = forced_expert_settings(BacktestRunFacts(enable_short=False, hold_assigned_stock=False,
                                                entry_action=None))


def test_fallback_branch_expert_settings():
    p = be.derive_export_payload(_bt(), "expert_settings")
    assert p["backtest_id"] == 11 and p["expert"] == "FMPRating"
    assert p["settings"]["expert_params"] == {"profit_ratio": 1.2, **GATES}
    assert p["start_date"] == "2024-01-01T00:00:00"
    assert list(p.keys()) == ["backtest_id", "name", "expert", "engine_type", "settings",
                              "backtest_only", "execution", "universe", "execution_interval",
                              "start_date", "end_date", "initial_capital"]


def test_opt_block_static_universe_and_base_settings():
    block = {"experts": [{"class": "FMPRating", "settings": {"sizing_mode": "notional"}}],
             "account_settings": {"commission_per_trade": 1.0, "slippage_bps": 5},
             "enabled_instruments": ["AAPL"], "seed": 3, "warmup_days": 30}
    p = be.derive_export_payload(_bt(), "expert_settings", opt_backtest_block=block)
    assert p["settings"]["expert_params"]["sizing_mode"] == "notional"
    assert p["universe"] == {"mode": "static", "symbols": ["AAPL"]}
    assert p["execution"]["commission"] == 1.0 and p["execution"]["seed"] == 3


def test_bypass_overlay_only_when_bypass_check_true():
    block = {"experts": [{"class": "FactorRanker", "settings": {"universe_source": "static"}}],
             "account_settings": {}, "screener_opt": {
                 "store": "sp500", "base_settings": {"min_mcap": 1}, "cadence_days": 7,
                 "apply_to_expert_settings": True}}
    bt = _bt(expert_name="FactorRanker", strategy_params={"screener:min_mcap": 5})
    on = be.derive_export_payload(bt, "expert_settings", opt_backtest_block=block,
                                  bypass_check=lambda name: name == "FactorRanker")
    off = be.derive_export_payload(bt, "expert_settings", opt_backtest_block=block)
    assert on["settings"]["expert_params"]["universe_source"] == "screener"
    assert on["settings"]["expert_params"]["min_mcap"] == 5
    assert off["settings"]["expert_params"]["universe_source"] == "static"
    assert on["universe"]["mode"] == "screener"


def test_ruleset_unified_rules_pass_through_normalized():
    p = be.derive_export_payload(_bt(strategy_params={"entryRules": [], "exitRules": []}),
                                 "ruleset")
    assert p == {"backtest_id": 11, "name": "TOP1", "entry_rules": [], "exit_rules": [],
                 "optimized_genes": {}}


def test_legacy_reconstruction_callback_used_only_for_gene_only_rows():
    calls = []
    def recon():
        calls.append(1)
        return None, None, [], []
    gene_only = _bt(strategy_params={"cond:c1:threshold": 3})
    assert be.needs_legacy_reconstruction(gene_only.strategy_params) is True
    be.derive_export_payload(gene_only, "ruleset", reconstruct_legacy_ruleset=recon)
    assert calls == [1]
    # An EMPTY exit list counts as absent (the original `not exits`), so use a buy tree.
    with_trees = _bt(strategy_params={"cond:c1:threshold": 3, "buyEntryConditions": {}})
    assert be.needs_legacy_reconstruction(with_trees.strategy_params) is False
    empty_exits = _bt(strategy_params={"cond:c1:threshold": 3, "exitConditions": []})
    assert be.needs_legacy_reconstruction(empty_exits.strategy_params) is True


def test_unsupported_kind():
    with pytest.raises(be.UnsupportedExportKind):
        be.derive_export_payload(_bt(), "nope")


def test_refused_ruleset_raises_export_refused(monkeypatch):
    def boom(rules, where):
        raise ValueError("unresolved mode gene")
    monkeypatch.setattr(be, "assert_market_conditions_resolved", boom)
    with pytest.raises(be.ExportRefused, match="unresolved mode gene"):
        be.derive_export_payload(_bt(strategy_params={"entryRules": []}), "ruleset")


def test_build_deploy_entry_key_order():
    e = be.build_deploy_entry(backtest_id=1, target_instance_id=None, account_id=None,
                              virtual_equity_pct=10.0, expert_name="FMPRating", label="x",
                              ruleset={"r": 1}, settings={"s": 1})
    assert list(e) == ["backtest_id", "target_instance_id", "account_id", "virtual_equity_pct",
                       "expert_name", "label", "ruleset", "settings"]
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_backtest_export.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ba2_common.export'`.

- [ ] **Step 3: Create the module**

`packages/common/ba2_common/export/__init__.py`:

```python
"""Pure export builders shared by the BA2 apps and the public site (no DB, no filesystem)."""
```

`packages/common/ba2_common/export/backtest_export.py`: the body below is `testplatform/backend/app/api/backtests.py::_derive_export_payload` (lines 1173-1446) with **exactly** these substitutions and nothing else changed. Keep the original comments inside the body when you paste; they are elided here only to save space.

| Original | Replacement |
|---|---|
| `from app.services.strategy_param_space import schedule_override_from_genes` (inside the function) | module-level import from `ba2_common.core.schedule_genes` |
| `bt_block, _strat = _opt_backtest_block(backtest, db)` | `bt_block = opt_backtest_block if isinstance(opt_backtest_block, dict) else None` |
| `… and _is_bypass_expert_class(backtest.expert_name)` | `… and bypass_check is not None and bypass_check(backtest.expert_name)` |
| `r_buy, r_sell, r_exits, r_entries = _reconstruct_opt_ruleset(backtest, db)` | `r_buy, r_sell, r_exits, r_entries = (reconstruct_legacy_ruleset() if reconstruct_legacy_ruleset is not None else (None, None, None, None))` |
| `raise HTTPException(status_code=400, detail=str(e)) from e` (market-condition block) | `raise ExportRefused(str(e)) from e` |
| final `raise HTTPException(status_code=400, detail=f"Unsupported export kind: …")` | `raise UnsupportedExportKind(f"Unsupported export kind: {kind!r}. Use 'expert_settings' or 'ruleset'.")` |
| function-local imports of `normalize_trade_rules`, `trade_rules_from_legacy`, `assert_market_conditions_resolved`, `assert_market_rule_actions` | module-level imports (so tests can monkeypatch them on this module) |
| `logger` (test-app logger) | `from ba2_common.logger import logger` |

```python
"""Backtest export payloads (``expert_settings`` / ``ruleset``) and the deploy-payload entry.

Moved from testplatform/backend/app/api/backtests.py::_derive_export_payload (2026-09, site plan
P0a) so the public site produces byte-identical payloads without importing the test app. Pure:
the caller resolves the optimization's run-level ``backtest`` block, the bypass-expert check and
(optionally) the legacy ruleset reconstruction, and passes them in. Pinned by
testplatform/backend/tests/test_export_payload_golden.py.
"""
from typing import Any, Callable, Dict, Optional, Tuple

from ba2_common.core.deploy_parity import (
    BacktestRunFacts, backtest_only_settings, forced_expert_settings,
)
from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool
from ba2_common.core.market_condition_rules import (
    assert_market_conditions_resolved, assert_market_rule_actions,
)
from ba2_common.core.rule_models import normalize_trade_rules, trade_rules_from_legacy
from ba2_common.core.schedule_genes import schedule_override_from_genes
from ba2_common.logger import logger

EXPORT_KINDS = ("expert_settings", "ruleset")

LegacyReconstructor = Callable[[], Tuple[Any, Any, Any, Any]]

_LEGACY_TREE_KEYS = ("buyEntryConditions", "buy_entry_conditions", "sellEntryConditions",
                     "sell_entry_conditions", "exitConditions", "exit_conditions",
                     "entryActions", "entry_actions")


class ExportRefused(ValueError):
    """The stored run cannot be exported as a deployable payload (the reason is the message)."""


class UnsupportedExportKind(ValueError):
    """``kind`` is not one of EXPORT_KINDS."""


def _is_rule_gene(k: Any) -> bool:
    return isinstance(k, str) and (k.startswith("cond:") or k.startswith("exit:")
                                   or k.startswith("entry:"))


def needs_legacy_reconstruction(strategy_params: Any) -> bool:
    """True when the ``ruleset`` export of this row can only be rebuilt by decoding its flat
    genes against the optimization's base strategy (the test app's ``decode_params``): no
    unified rule lists, no legacy trees, but rule genes present. Mirrors the condition inside
    ``derive_export_payload``; callers without a reconstructor must treat such rows as not
    exportable."""
    sp = strategy_params if isinstance(strategy_params, dict) else {}
    for k in ("entryRules", "entry_rules", "exitRules", "exit_rules"):
        if sp.get(k) is not None:
            return False
    buy = sp.get("buyEntryConditions") if sp.get("buyEntryConditions") is not None else sp.get("buy_entry_conditions")
    sell = sp.get("sellEntryConditions") if sp.get("sellEntryConditions") is not None else sp.get("sell_entry_conditions")
    exits = sp.get("exitConditions") if sp.get("exitConditions") is not None else sp.get("exit_conditions")
    entries = sp.get("entryActions") if sp.get("entryActions") is not None else sp.get("entry_actions")
    return (buy is None and sell is None and not exits and not entries
            and any(_is_rule_gene(k) for k in sp))


def derive_export_payload(
    backtest: Any,
    kind: str,
    *,
    opt_backtest_block: Optional[Dict[str, Any]] = None,
    bypass_check: Optional[Callable[[Optional[str]], bool]] = None,
    reconstruct_legacy_ruleset: Optional[LegacyReconstructor] = None,
) -> Dict[str, Any]:
    """Build the chosen read-only export payload from a backtest's strategy_params.

    (Paste the original docstring's two-kinds explanation here, unchanged.)

    ``backtest`` needs attributes id, name, expert_name, engine_type, strategy_params,
    start_date, end_date, initial_capital. ``opt_backtest_block`` is the source optimization's
    ``optimization_config['backtest']`` dict, or None. ``bypass_check(expert_name)`` is only
    consulted on the screener + apply_to_expert_settings branch (lazy, as before).
    ``reconstruct_legacy_ruleset()`` is only consulted for gene-only legacy rows.

    Raises ExportRefused (undeployable ruleset) or UnsupportedExportKind.
    """
    sp = backtest.strategy_params or {}

    def _pick(*keys):
        for k in keys:
            if isinstance(sp, dict) and k in sp and sp[k] is not None:
                return sp[k]
        return None

    if kind == "expert_settings":
        model_overrides = (
            {k[len("model:"):]: v for k, v in sp.items()
             if isinstance(k, str) and k.startswith("model:")}
            if isinstance(sp, dict) else {}
        )
        persisted_fixed = (sp.get("expertFixedSettings") or {}) if isinstance(sp, dict) else {}
        bt_block = opt_backtest_block if isinstance(opt_backtest_block, dict) else None
        if bt_block is not None:
            base_specs = bt_block.get("experts") or []
            base_settings = {}
            for spec in base_specs:
                if isinstance(spec, dict) and spec.get("class") == backtest.expert_name:
                    base_settings = dict(spec.get("settings") or {})
                    break
            expert_params = {**persisted_fixed, **base_settings, **model_overrides}
            acct = bt_block.get("account_settings") or {}
            screener_opt = bt_block.get("screener_opt")
            if isinstance(screener_opt, dict) and screener_opt.get("store"):
                screener_overrides = {
                    k[len("screener:"):]: v for k, v in sp.items()
                    if isinstance(k, str) and k.startswith("screener:")
                } if isinstance(sp, dict) else {}
                eff_screener = {**(screener_opt.get("base_settings") or {}), **screener_overrides}
                universe = {
                    "mode": "screener",
                    "screener_store": screener_opt["store"],
                    "screener_settings": eff_screener,
                    "screener_cadence_days": int(screener_opt.get("cadence_days", 7)),
                }
                if (screener_opt.get("apply_to_expert_settings") and bypass_check is not None
                        and bypass_check(backtest.expert_name)):
                    expert_params = {
                        **persisted_fixed,
                        **base_settings,
                        "universe_source": "screener",
                        "screener_store": screener_opt["store"],
                        **eff_screener,
                        **model_overrides,
                    }
            else:
                universe = {"mode": "static", "symbols": list(bt_block.get("enabled_instruments") or [])}
            execution = {
                "seed": bt_block.get("seed"),
                "fill_model": acct.get("fill_model"),
                "warmup_days": bt_block.get("warmup_days"),
                "commission": acct.get("commission_per_trade"),
                "slippage": acct.get("slippage_bps"),
                "enable_short": bool(bt_block.get("enable_short")),
                "run_schedule_override": (
                    schedule_override_from_genes(sp, bt_block.get("run_schedule_override"),
                                                weekdays_only=True)
                    or bt_block.get("run_schedule_override")
                ),
            }
            interval = bt_block.get("execution_interval")
        else:
            expert_params = (
                {**persisted_fixed, **model_overrides} if (persisted_fixed or model_overrides)
                else _pick("expertSettings", "expert_settings") or {}
            )
            universe = _pick("universe")
            execution = {
                "seed": _pick("seed"),
                "fill_model": _pick("fillModel", "fill_model"),
                "warmup_days": _pick("warmupDays", "warmup_days"),
                "commission": _pick("commission"),
                "slippage": _pick("slippage"),
                "enable_short": _pick("enableShort", "enable_short"),
                "run_schedule_override": (
                    schedule_override_from_genes(
                        sp, _pick("runScheduleOverride", "run_schedule_override"),
                        weekdays_only=True)
                    or _pick("runScheduleOverride", "run_schedule_override")
                ),
            }
            interval = _pick("executionInterval", "execution_interval")

        def _executed_toggle(name: str) -> bool:
            raw = sp.get(f"model:{name}")
            if raw is None:
                return False
            try:
                return coerce_bool(raw)
            except ValueError:
                logger.warning(f"backtest {backtest.id}: model:{name}={raw!r} is not a boolean "
                               f"spelling; exporting it OFF, as every run on record was")
                return False

        facts = BacktestRunFacts(
            enable_short=bool(execution.get("enable_short")),
            hold_assigned_stock=bool((acct if bt_block is not None else {}).get(
                "hold_assigned_stock")),
            entry_action=(bt_block.get("entry_action") if bt_block is not None else None),
            use_atr_stop=_executed_toggle("use_atr_stop"),
            regime_overlay_enabled=_executed_toggle("regime_overlay_enabled"),
        )
        expert_params = {**expert_params, **forced_expert_settings(facts)}
        return {
            "backtest_id": backtest.id,
            "name": backtest.name,
            "expert": backtest.expert_name,
            "engine_type": backtest.engine_type or "ml",
            "settings": {
                "expert_params": expert_params,
            },
            "backtest_only": backtest_only_settings(facts),
            "execution": execution,
            "universe": universe,
            "execution_interval": interval,
            "start_date": backtest.start_date.isoformat() if backtest.start_date else None,
            "end_date": backtest.end_date.isoformat() if backtest.end_date else None,
            "initial_capital": backtest.initial_capital,
        }

    if kind == "ruleset":
        cond_genes = (
            {k: v for k, v in sp.items() if _is_rule_gene(k)}
            if isinstance(sp, dict) else {}
        )
        entry_rules = _pick("entryRules", "entry_rules")
        exit_rules = _pick("exitRules", "exit_rules")

        if entry_rules is None and exit_rules is None:
            buy = _pick("buyEntryConditions", "buy_entry_conditions")
            sell = _pick("sellEntryConditions", "sell_entry_conditions")
            exits = _pick("exitConditions", "exit_conditions")
            entries = _pick("entryActions", "entry_actions")
            if buy is None and sell is None and not exits and not entries and cond_genes:
                r_buy, r_sell, r_exits, r_entries = (
                    reconstruct_legacy_ruleset() if reconstruct_legacy_ruleset is not None
                    else (None, None, None, None))
                buy = buy if buy is not None else r_buy
                sell = sell if sell is not None else r_sell
                exits = exits if exits else r_exits
                entries = entries if entries else r_entries
            converted = trade_rules_from_legacy(
                buy_tree=buy, sell_tree=sell, entry_actions=entries, exit_conditions=exits,
            )
            entry_rules = converted["entry_rules"]
            exit_rules = converted["exit_rules"]

        raw_exit_rules = exit_rules if isinstance(exit_rules, list) else []
        entry_rules = normalize_trade_rules(entry_rules or [])
        exit_rules = normalize_trade_rules(exit_rules or [])
        try:
            assert_market_conditions_resolved(entry_rules, f"backtest {backtest.id} entry_rules")
            assert_market_conditions_resolved(exit_rules, f"backtest {backtest.id} exit_rules")
            assert_market_rule_actions(raw_exit_rules, f"backtest {backtest.id} exit_rules")
            assert_market_rule_actions(exit_rules, f"backtest {backtest.id} exit_rules")
        except ValueError as e:
            raise ExportRefused(str(e)) from e
        return {
            "backtest_id": backtest.id,
            "name": backtest.name,
            "entry_rules": entry_rules,
            "exit_rules": exit_rules,
            "optimized_genes": cond_genes,
        }

    raise UnsupportedExportKind(
        f"Unsupported export kind: {kind!r}. Use 'expert_settings' or 'ruleset'.")


def build_deploy_entry(*, backtest_id: Any, target_instance_id: Any, account_id: Any,
                       virtual_equity_pct: Any, expert_name: Any, label: Any,
                       ruleset: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    """One entry of the deploy-payload file that tools/import_deploy_payload.py consumes
    (a JSON list of these). ``target_instance_id=None`` makes the import create the instance."""
    return {
        "backtest_id": backtest_id,
        "target_instance_id": target_instance_id,
        "account_id": account_id,
        "virtual_equity_pct": virtual_equity_pct,
        "expert_name": expert_name,
        "label": label,
        "ruleset": ruleset,
        "settings": settings,
    }
```

- [ ] **Step 4: Run the pure tests**

Run: `~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_backtest_export.py -q`
Expected: all PASS.

- [ ] **Step 5: Replace the test-app function with a wrapper**

In `testplatform/backend/app/api/backtests.py`, replace the whole `_derive_export_payload` function (lines 1173-1446) with:

```python
def _derive_export_payload(backtest: Backtest, kind: str, db: Any = None) -> dict:
    """Build the chosen read-only export payload from a backtest's strategy_params.

    Thin wrapper (site plan P0a): the derivation lives in
    ``ba2_common.export.backtest_export.derive_export_payload`` so the public site produces
    byte-identical payloads. This wrapper resolves the DB-bound inputs (the optimization's
    run-level block, the bypass-expert check, the legacy ruleset reconstruction) and maps the
    shared errors to the same HTTP 400s this endpoint always raised.
    """
    from ba2_common.export.backtest_export import (
        ExportRefused, UnsupportedExportKind, derive_export_payload,
    )

    bt_block = None
    if kind == "expert_settings":
        bt_block, _strat = _opt_backtest_block(backtest, db)
    try:
        return derive_export_payload(
            backtest, kind,
            opt_backtest_block=bt_block,
            bypass_check=_is_bypass_expert_class,
            reconstruct_legacy_ruleset=lambda: _reconstruct_opt_ruleset(backtest, db),
        )
    except (ExportRefused, UnsupportedExportKind) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
```

Then delete from the top of `backtests.py` any import that is no longer used. Run `grep -n "coerce_bool\|BacktestRunFacts\|backtest_only_settings\|forced_expert_settings" testplatform/backend/app/api/backtests.py` and remove only the names with no remaining use.

- [ ] **Step 6: Point export_deploy_payload at the shared builder**

In `tools/export_deploy_payload.py`, add after the `_derive_export_payload` import (line 37):

```python
from ba2_common.export.backtest_export import build_deploy_entry  # noqa: E402
```
and replace the `payloads.append({...})` literal (lines 82-91) with:

```python
        payloads.append(build_deploy_entry(
            backtest_id=bt_id,
            target_instance_id=inst_id,          # None -> import creates the instance
            account_id=args.account,
            virtual_equity_pct=args.equity_pct,
            expert_name=bt.expert_name,
            label=label,
            ruleset=ruleset,
            settings=settings,
        ))
```

- [ ] **Step 7: Run the goldens, the export tests and the whole test-platform suite**

```bash
cd testplatform/backend
~/ba2-venvs/test/bin/python -m pytest tests/test_export_payload_golden.py tests/test_backtest_export_fixed_settings.py tests/backtest/test_deploy_round_trip_parity.py -q
~/ba2-venvs/test/bin/python -m pytest tests -q -x
cd -
```
Expected: all PASS. If a golden differs, the move changed behavior. Diff the golden against the new output and fix the new module; never re-baseline here.

- [ ] **Step 8: Commit**

```bash
git add packages/common/ba2_common/export packages/common/tests/test_backtest_export.py testplatform/backend/app/api/backtests.py tools/export_deploy_payload.py
git commit -m "refactor(export): backtest export derivation moves to ba2_common.export.backtest_export

The test-app endpoint keeps its wrapper and HTTP 400s; goldens unchanged.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Move the live performance metrics to ba2_common

**Files:**
- Create: `packages/common/ba2_common/analytics/__init__.py`, `packages/common/ba2_common/analytics/performance.py`
- Modify: `ba2_trade_platform/ui/components/performance_charts.py:437-563` (the five functions)
- Modify: `ba2_trade_platform/ui/pages/performance.py:12-18` (imports), `:122-196` (per-expert loop body); `_calculate_monthly_metrics` stays unchanged
- Test: `tests/test_performance_page_metrics.py` (characterization, written first), `packages/common/tests/test_performance_analytics.py`

**Interfaces:**
- Produces, in `ba2_common.analytics.performance`:
  - `calculate_sharpe_ratio(returns, risk_free_rate=0.02) -> float | None`
  - `calculate_win_loss_ratio(transactions) -> tuple[float, int, int]`
  - `calculate_max_drawdown(equity_curve) -> float`
  - `max_drawdown_from_pnl(pnls) -> tuple[float, float | None]`
  - `calculate_profit_factor(winning_trades, losing_trades) -> float | None`
  - `expert_performance(txns) -> dict` with keys `total_transactions, max_drawdown, max_drawdown_pct, avg_duration_days, total_pnl, avg_pnl, win_rate, wins, losses, profit_factor, largest_win, largest_loss, sharpe_ratio, transactions, returns`
  - `monthly_pnl(txns) -> dict[str, dict]` mapping `"YYYY-MM"` to `{"pnl": float, "count": int}`
- A `txn` is any object with `open_price, close_price, quantity, side` (`OrderDirection` or its string value), `multiplier`, `open_date`, `close_date`.

- [ ] **Step 1: Write the characterization test against the CURRENT page code**

`tests/test_performance_page_metrics.py`:

```python
"""Characterization pin for PerformanceTab's per-expert metrics, written BEFORE the metric
code moved to ba2_common.analytics.performance (site plan P0a). Must stay green after."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.ui.pages.performance import PerformanceTab

T0 = datetime(2026, 1, 5, 15, 0)


def _txn(i, side, open_p, close_p, qty=10, mult=None, days=2, expert_id=901):
    return SimpleNamespace(id=i, expert_id=expert_id, side=side, open_price=open_p,
                           close_price=close_p, quantity=qty, multiplier=mult,
                           open_date=T0 + timedelta(days=i),
                           close_date=T0 + timedelta(days=i + days))


TXNS = (
    [_txn(0, OrderDirection.BUY, 100.0, 110.0), _txn(1, OrderDirection.SELL, 50.0, 55.0),
     _txn(2, OrderDirection.BUY, 2.0, 0.0, qty=1, mult=100),     # expired worthless option
     _txn(3, OrderDirection.SELL, 1.5, 0.0, qty=2, mult=100)]    # short option kept premium
    + [_txn(10 + k, OrderDirection.BUY, 20.0, 20.0 + (k % 5) - 2) for k in range(30)]
)


def test_per_expert_metrics_are_pinned():
    m = PerformanceTab(None)._calculate_transaction_metrics(TXNS)["Expert-901"]
    assert m["total_transactions"] == 34
    assert round(m["total_pnl"], 6) == round(100 - 50 - 200 + 300 + sum(
        ((k % 5) - 2) * 10 for k in range(30)), 6)
    assert (m["wins"], m["losses"]) == (14, 14)
    assert m["sharpe_ratio"] is not None
    assert round(m["avg_duration_days"], 6) == 2.0
    assert m["max_drawdown"] >= 0


def test_monthly_metrics_are_pinned():
    monthly = PerformanceTab(None)._calculate_monthly_metrics(TXNS)
    total = sum(v["Expert-901"]["pnl"] for v in monthly.values())
    count = sum(v["Expert-901"]["count"] for v in monthly.values())
    assert count == 34
    assert round(total, 6) == round(100 - 50 - 200 + 300 + sum(
        ((k % 5) - 2) * 10 for k in range(30)), 6)
```

Before Step 2, compute the exact expected values once and **replace the `>= 0` / `is not None` assertions with exact rounded numbers**. Run:
`~/ba2-venvs/trade/bin/python -c "from tests.test_performance_page_metrics import *; m=PerformanceTab(None)._calculate_transaction_metrics(TXNS)['Expert-901']; print({k:v for k,v in m.items() if k not in ('transactions','returns')})"`.
Paste the printed `sharpe_ratio`, `max_drawdown`, `max_drawdown_pct`, `profit_factor`, `largest_win` and `largest_loss` into the test as `round(x, 9) == <value>` assertions. After this step, the test pins every key.

- [ ] **Step 2: Run the characterization test (it must PASS on current code)**

Run: `~/ba2-venvs/trade/bin/python -m pytest tests/test_performance_page_metrics.py -q`
Expected: PASS.

- [ ] **Step 3: Write the failing pure tests**

`packages/common/tests/test_performance_analytics.py`:

```python
from datetime import datetime, timedelta
from types import SimpleNamespace

from ba2_common.analytics.performance import (
    calculate_profit_factor, calculate_sharpe_ratio, expert_performance, max_drawdown_from_pnl,
    monthly_pnl,
)
from ba2_common.core.types import OrderDirection

T0 = datetime(2026, 1, 5)


def _t(i, side, o, c, qty=1, mult=None):
    return SimpleNamespace(side=side, open_price=o, close_price=c, quantity=qty, multiplier=mult,
                           open_date=T0 + timedelta(days=i), close_date=T0 + timedelta(days=i + 1))


def test_zero_close_is_a_measured_loss_and_option_multiplier_applies():
    p = expert_performance([_t(0, OrderDirection.BUY, 2.0, 0.0, mult=100)])
    assert p["total_pnl"] == -200.0 and p["losses"] == 1


def test_short_side_accepts_enum_or_string():
    a = expert_performance([_t(0, OrderDirection.SELL, 10.0, 8.0, qty=5)])
    b = expert_performance([_t(0, "SELL", 10.0, 8.0, qty=5)])
    assert a["total_pnl"] == b["total_pnl"] == 10.0


def test_sharpe_needs_thirty_returns():
    assert calculate_sharpe_ratio([0.01] * 29) is None
    assert expert_performance([_t(i, "BUY", 10, 11) for i in range(29)])["sharpe_ratio"] is None


def test_drawdown_walks_close_order():
    txns = [_t(1, "BUY", 10, 5, qty=10), _t(0, "BUY", 10, 20, qty=10)]  # +100 first by close
    p = expert_performance(txns)
    assert (p["max_drawdown"], p["max_drawdown_pct"]) == max_drawdown_from_pnl([100.0, -50.0])


def test_profit_factor_edge_cases():
    assert calculate_profit_factor([], []) is None
    assert calculate_profit_factor([5.0], []) == float("inf")


def test_monthly_pnl_buckets_by_close_month():
    txns = [_t(0, "BUY", 10, 11), _t(40, "BUY", 10, 9)]
    assert monthly_pnl(txns) == {"2026-01": {"pnl": 1.0, "count": 1},
                                 "2026-02": {"pnl": -1.0, "count": 1}}
```

- [ ] **Step 4: Run them and confirm they fail**

Run: `~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_performance_analytics.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'ba2_common.analytics'`.

- [ ] **Step 5: Create the module**

`packages/common/ba2_common/analytics/__init__.py`:

```python
"""Pure analytics shared by the live app and the public site."""
```

`packages/common/ba2_common/analytics/performance.py`: paste the five functions **verbatim** from `performance_charts.py` lines 437-563 (`calculate_sharpe_ratio` through `calculate_profit_factor`, docstrings included), under this header:

```python
"""Trade-performance metrics (moved from ba2_trade_platform/ui/components/performance_charts.py
and the per-expert aggregation in ui/pages/performance.py, 2026-09, site plan P0a).
Pure: operates on transaction-like objects, never touches a DB."""
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from ba2_common.core.utils import calculate_transaction_pnl
```

Then append:

```python
def expert_performance(txns: Iterable[Any]) -> Dict[str, Any]:
    """Per-expert metrics exactly as the live Performance page computes them (one expert's
    transactions in, one metrics dict out). Order-sensitive pieces (drawdown) sort by close date
    themselves; everything else follows the given order, as the page always did."""
    txns = list(txns)
    durations = []
    for txn in txns:
        if txn.open_date and txn.close_date:
            durations.append((txn.close_date - txn.open_date).total_seconds() / 86400)

    pnls = []
    for txn in txns:
        pnl = calculate_transaction_pnl(txn)
        if pnl is not None:
            pnls.append(pnl)

    winning_pnls = [p for p in pnls if p > 0]
    losing_pnls = [p for p in pnls if p < 0]
    win_rate, wins, losses = calculate_win_loss_ratio([{'pnl': pnl} for pnl in pnls])

    returns = []
    for txn in txns:
        pnl = calculate_transaction_pnl(txn)
        if pnl is not None:
            position_value = txn.open_price * txn.quantity * (getattr(txn, "multiplier", None) or 1)
            if position_value != 0:
                returns.append(pnl / position_value)

    ordered = sorted((t for t in txns if t.close_date), key=lambda t: t.close_date)
    ordered_pnls = [pnl for pnl in (calculate_transaction_pnl(t) for t in ordered)
                    if pnl is not None]
    max_dd, max_dd_pct = max_drawdown_from_pnl(ordered_pnls)

    return {
        'total_transactions': len(txns),
        'max_drawdown': max_dd,
        'max_drawdown_pct': max_dd_pct,
        'avg_duration_days': np.mean(durations) if durations else 0,
        'total_pnl': sum(pnls) if pnls else 0,
        'avg_pnl': np.mean(pnls) if pnls else 0,
        'win_rate': win_rate,
        'wins': wins,
        'losses': losses,
        'profit_factor': calculate_profit_factor(winning_pnls, losing_pnls),
        'largest_win': max(winning_pnls) if winning_pnls else None,
        'largest_loss': min(losing_pnls) if losing_pnls else None,
        'sharpe_ratio': calculate_sharpe_ratio(returns) if len(returns) >= 30 else None,
        'transactions': txns,
        'returns': returns,
    }


def monthly_pnl(txns: Iterable[Any]) -> Dict[str, Dict[str, float]]:
    """``{"YYYY-MM": {"pnl": sum, "count": n}}`` by CLOSE month, in the given order (the live
    page's monthly chart, for one expert). Open or unmeasurable transactions are skipped."""
    out: Dict[str, Dict[str, float]] = defaultdict(lambda: {'pnl': 0, 'count': 0})
    for txn in txns:
        pnl = calculate_transaction_pnl(txn)
        if txn.close_date and pnl is not None:
            bucket = out[txn.close_date.strftime('%Y-%m')]
            bucket['pnl'] += pnl
            bucket['count'] += 1
    return dict(out)
```

`calculate_transaction_pnl` compares `transaction.side == OrderDirection.BUY`. `OrderDirection` is a `str` Enum, so the string `"BUY"` also matches; the pure test pins that.

- [ ] **Step 6: Re-bind the names in performance_charts and use the helpers in the page**

In `performance_charts.py`, delete the five function definitions (437-563) and put this in their place:

```python
# Metric functions moved to ba2_common (site plan P0a); re-bound here for existing importers.
from ba2_common.analytics.performance import (  # noqa: E402,F401
    calculate_max_drawdown, calculate_profit_factor, calculate_sharpe_ratio,
    calculate_win_loss_ratio, max_drawdown_from_pnl,
)
```

In `performance.py`:
- Change the `performance_charts` import to keep only the UI classes (`MetricCard, PerformanceBarChart, TimeSeriesChart, PieChartComponent, PerformanceTable, MultiMetricDashboard`), and add `from ba2_common.analytics.performance import expert_performance, calculate_sharpe_ratio`. Before deleting any other name, grep the file for remaining uses of `calculate_win_loss_ratio`, `calculate_max_drawdown`, `calculate_profit_factor` and `max_drawdown_from_pnl`; if any remain, import those from `ba2_common.analytics.performance` too.
- In `_calculate_transaction_metrics`, replace everything from `# Calculate transaction duration` through the closing `}` of `expert_metrics[expert_name] = {...}` with:

```python
            expert_metrics[expert_name] = expert_performance(txns)
```
- Leave `_calculate_monthly_metrics` **unchanged** (controller ruling R9). Regrouping by expert would change the insertion order of the month and expert keys, which the order-sensitive live capture catches. `ba2_common.analytics.performance.monthly_pnl` is used by the public site and pinned by its own tests.

- [ ] **Step 7: Run everything touched**

```bash
~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_performance_analytics.py tests/test_performance_page_metrics.py tests/test_max_drawdown.py tests/test_no_zero_coercion.py -q
```
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add packages/common/ba2_common/analytics packages/common/tests/test_performance_analytics.py tests/test_performance_page_metrics.py ba2_trade_platform/ui/components/performance_charts.py ba2_trade_platform/ui/pages/performance.py
git commit -m "refactor(analytics): performance metrics move to ba2_common.analytics.performance

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Pure settings-row decoding and enabled-instruments parsing

**Files:**
- Modify: `packages/common/ba2_common/core/interfaces/ExtendableSettingsInterface.py:437-525` (the `settings` property)
- Modify: `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py:1151-1175`
- Test: `packages/common/tests/test_settings_row_decoding.py`

**Interfaces:**
- Produces:
  - `ba2_common.core.interfaces.ExtendableSettingsInterface.decode_setting_rows(rows: Iterable[Any], definitions: dict) -> dict`. Each row has attributes `key, value_str, value_json, value_float`.
  - `ba2_common.core.interfaces.MarketExpertInterface.enabled_instruments_config(settings: dict) -> dict`.

- [ ] **Step 1: Write the failing test**

`packages/common/tests/test_settings_row_decoding.py`:

```python
from types import SimpleNamespace as R

from ba2_common.core.interfaces.ExtendableSettingsInterface import decode_setting_rows
from ba2_common.core.interfaces.MarketExpertInterface import enabled_instruments_config

DEFS = {"flag": {"type": "bool"}, "n": {"type": "int"}, "x": {"type": "float"},
        "s": {"type": "str"}, "j": {"type": "json"}, "unset": {"type": "str"}}


def _r(key, value_str=None, value_json=None, value_float=None):
    return R(key=key, value_str=value_str, value_json=value_json, value_float=value_float)


def test_typed_decoding_and_legacy_spellings():
    out = decode_setting_rows([
        _r("flag", value_json="1"), _r("n", value_float=5.0), _r("x", value_float=0.5),
        _r("s", value_str="None"), _r("j", value_json={"a": 1}),
    ], DEFS)
    assert out == {"flag": True, "n": 5, "x": 0.5, "s": None, "j": {"a": 1}, "unset": None}


def test_legacy_int_in_value_str_and_unreadable_bool():
    out = decode_setting_rows([_r("n", value_str="7"), _r("flag", value_json="maybe")], DEFS)
    assert out["n"] == 7 and out["flag"] is False


def test_undefined_keys_infer_type_from_storage():
    out = decode_setting_rows([_r("extra_j", value_json=[1]), _r("extra_f", value_float=2.0),
                               _r("extra_s", value_str="hi")], {})
    assert out == {"extra_j": [1], "extra_f": 2.0, "extra_s": "hi"}


def test_enabled_instruments_config():
    assert enabled_instruments_config({"enabled_instruments": {"AAPL": {}}}) == {"AAPL": {}}
    assert enabled_instruments_config({"enabled_instruments": '{"MSFT": {"w": 1}}'}) == {"MSFT": {"w": 1}}
    assert enabled_instruments_config({"enabled_instruments": "not json"}) == {}
    assert enabled_instruments_config({}) == {}
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_settings_row_decoding.py -q`
Expected: FAIL with `ImportError: cannot import name 'decode_setting_rows'`.

- [ ] **Step 3: Extract the decoding loop**

In `ExtendableSettingsInterface.py`, add a module-level function above the class. Its body is the property's code from `settings = {k : None for k in definitions.keys()}` through the end of the `for setting in settings_value_from_db:` loop, **verbatim**, with `settings_value_from_db` renamed to `rows`:

```python
def decode_setting_rows(rows, definitions: Dict[str, Any]) -> Dict[str, Any]:
    """Typed settings dict from stored setting rows (``key``, ``value_str``, ``value_json``,
    ``value_float``) and the class's merged settings definitions. Every defined key is present
    (None when no row); undefined stored keys infer their type from which column holds data.
    Pure -- the ``settings`` property below and the public site's importer both use it."""
    settings = {k: None for k in definitions.keys()}
    for setting in rows:
        ...  # (the loop body, verbatim)
    return settings
```

In the property, replace the moved block with:

```python
            settings = decode_setting_rows(settings_value_from_db, definitions)
```
Keep the cache assignment, the return and the `except` exactly as they are.

- [ ] **Step 4: Extract the instruments parser**

In `MarketExpertInterface.py`, add a module-level function above the class:

```python
def enabled_instruments_config(settings: Dict[str, Any]) -> Dict[str, Dict]:
    """Instrument symbol -> config from an expert's decoded settings (``enabled_instruments``
    holds a dict or its JSON text). Pure; ``_get_enabled_instruments_config`` delegates here."""
    enabled_instruments_setting = (settings or {}).get('enabled_instruments')
    if enabled_instruments_setting:
        if isinstance(enabled_instruments_setting, dict):
            return enabled_instruments_setting
        elif isinstance(enabled_instruments_setting, str):
            try:
                import json
                return json.loads(enabled_instruments_setting)
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"Failed to parse enabled_instruments setting as JSON: {enabled_instruments_setting}")
                return {}
    return {}
```
and make the method's body `return enabled_instruments_config(self.settings)`, keeping its docstring.

- [ ] **Step 5: Run the new tests and the settings-related suites**

```bash
~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_settings_row_decoding.py packages/common/tests/test_bool_setting_round_trip.py -q
~/ba2-venvs/trade/bin/python -m pytest packages/common/tests -q -x
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/common/ba2_common/core/interfaces/ExtendableSettingsInterface.py packages/common/ba2_common/core/interfaces/MarketExpertInterface.py packages/common/tests/test_settings_row_decoding.py
git commit -m "refactor(common): pure decode_setting_rows + enabled_instruments_config

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Pure ruleset export body

**Files:**
- Modify: `packages/common/ba2_common/core/rules_export_import.py:173-282` (`export_ruleset`, `export_multiple_rulesets`)
- Test: `packages/common/tests/test_ruleset_export_body.py`

**Interfaces:**
- Produces, in `ba2_common.core.rules_export_import`:
  - `ruleset_export_body(ruleset, ordered_rules) -> dict`. `ruleset` has `name, description, type, subtype` (enums or None). `ordered_rules` is an iterable of `(order_index, rule)`, where `rule` has `name, type, subtype, triggers, actions, extra_parameters, continue_processing`.
  - `rulesets_export_envelope(bodies, exported_at=None) -> dict`

- [ ] **Step 1: Write the failing test**

`packages/common/tests/test_ruleset_export_body.py`:

```python
from types import SimpleNamespace as NS

from ba2_common.core.rules_export_import import ruleset_export_body, rulesets_export_envelope
from ba2_common.core.types import AnalysisUseCase, ExpertEventRuleType


def _rule(name):
    return NS(name=name, type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE, subtype=None,
              triggers={"t0": {"event_type": "bullish"}}, actions={"a0": {"action_type": "buy"}},
              extra_parameters={}, continue_processing=False)


def test_body_shape_order_and_generated_names():
    rs = NS(name="RS", description="d", type=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE,
            subtype=AnalysisUseCase.ENTER_MARKET)
    body = ruleset_export_body(rs, [(0, _rule("My Rule")), (1, _rule("rule 1"))])
    assert list(body) == ["name", "description", "type", "subtype", "rules"]
    assert body["type"] == "trading_recommendation_rule"
    assert body["subtype"] == "enter_market"
    assert body["rules"][0]["name"] == "My Rule"
    assert body["rules"][1]["name"] != "rule 1"          # generic -> generated
    assert body["rules"][1]["order_index"] == 1
    assert list(body["rules"][0]) == ["name", "type", "subtype", "triggers", "actions",
                                      "extra_parameters", "continue_processing", "order_index"]


def test_envelope():
    env = rulesets_export_envelope([{"name": "A"}], exported_at="2026-01-01T00:00:00")
    assert env == {"export_version": "1.0", "export_type": "rulesets",
                   "export_timestamp": "2026-01-01T00:00:00", "rulesets": [{"name": "A"}]}
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_ruleset_export_body.py -q`
Expected: FAIL with `ImportError: cannot import name 'ruleset_export_body'`.

- [ ] **Step 3: Add the pure builders and delegate**

In `rules_export_import.py`, add above `class RulesExporter`:

```python
def ruleset_export_body(ruleset: Any, ordered_rules: Iterable[Tuple[int, Any]]) -> Dict[str, Any]:
    """The ``ruleset`` object of a ruleset export ({name, description, type, subtype, rules}).
    Pure: ``ordered_rules`` is ``(order_index, rule)`` pairs already in order. Shared by
    RulesExporter and the public site's importer."""
    return {
        "name": ruleset.name,
        "description": ruleset.description,
        "type": ruleset.type.value if ruleset.type else None,
        "subtype": ruleset.subtype.value if ruleset.subtype else None,
        "rules": [
            {
                "name": _display_rule_name(rule),
                "type": rule.type.value if rule.type else None,
                "subtype": rule.subtype.value if rule.subtype else None,
                "triggers": rule.triggers,
                "actions": rule.actions,
                "extra_parameters": rule.extra_parameters,
                "continue_processing": rule.continue_processing,
                "order_index": order_index,
            }
            for order_index, rule in ordered_rules
        ],
    }


def rulesets_export_envelope(bodies: List[Dict[str, Any]],
                             exported_at: Optional[str] = None) -> Dict[str, Any]:
    """The multi-ruleset export envelope around ``ruleset_export_body`` results."""
    return {
        "export_version": "1.0",
        "export_type": "rulesets",
        "export_timestamp": exported_at or datetime.now().isoformat(),
        "rulesets": list(bodies),
    }
```
Add `Iterable` to the existing `from typing import Dict, List, Any, Optional, Tuple` line (line 9).

In `export_ruleset`, keep the two DB queries. Replace the manual assembly so that the returned dict is:

```python
                return {
                    "export_version": "1.0",
                    "export_type": "ruleset",
                    "export_timestamp": datetime.now().isoformat(),
                    "ruleset": ruleset_export_body(
                        ruleset, [(link.order_index, rule) for link, rule in results]),
                }
```
Move the `return` inside the inner `with get_db()` block, after `results = session.exec(statement).all()`. In `export_multiple_rulesets`, build `bodies = [RulesExporter.export_ruleset(rid)["ruleset"] for rid in ruleset_ids]` and `return rulesets_export_envelope(bodies)`, keeping the `try/except` logging.

- [ ] **Step 4: Run the new test and the existing rules export suites**

```bash
~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_ruleset_export_body.py packages/common/tests/test_cross_platform_ruleset_roundtrip.py tests/test_rules_export_import.py -q
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/common/ba2_common/core/rules_export_import.py packages/common/tests/test_ruleset_export_body.py
git commit -m "refactor(rules): pure ruleset_export_body / rulesets_export_envelope

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: expert_batch v1.0 builder in ba2_common

**Files:**
- Create: `packages/common/ba2_common/export/expert_batch.py`
- Modify: `ba2_trade_platform/core/expert_batch_export_import.py:44-121` (constants, `build_batch_export`, `_export_one`)
- Test: `packages/common/tests/test_expert_batch_builder.py`; the existing `tests/test_expert_batch_export_import.py` stays green.

**Interfaces:**
- Consumes: `rulesets_export_envelope` (Task 6), `decode_setting_rows`, `enabled_instruments_config` (Task 5).
- Produces, in `ba2_common.export.expert_batch`:
  - `EXPORT_TYPE = "expert_batch"`, `EXPORT_VERSION = "1.0"`
  - `RULESET_SLOTS = (("enter_market_ruleset_id", "enter_market_ruleset_name"), ("open_positions_ruleset_id", "open_positions_ruleset_name"))`
  - `build_expert_batch_entry(*, expert_type, alias, user_description, enabled, virtual_equity_pct, priority, account_id, ruleset_names, rulesets_export, expert_settings, symbol_settings) -> dict`
  - `build_batch_envelope(entries, exported_at=None) -> dict`

- [ ] **Step 1: Write the failing test**

`packages/common/tests/test_expert_batch_builder.py`:

```python
from ba2_common.export.expert_batch import (
    EXPORT_TYPE, EXPORT_VERSION, RULESET_SLOTS, build_batch_envelope, build_expert_batch_entry,
)


def test_entry_shape_and_key_order():
    e = build_expert_batch_entry(
        expert_type="FMPRating", alias=None, user_description="secret note", enabled=True,
        virtual_equity_pct=10.0, priority=1, account_id=3,
        ruleset_names={"enter_market_ruleset_name": "EM", "open_positions_ruleset_name": None},
        rulesets_export={"rulesets": []}, expert_settings={"a": 1}, symbol_settings={})
    assert list(e) == ["expert_type", "general", "enter_market_ruleset_name",
                       "open_positions_ruleset_name", "rulesets", "expert_settings",
                       "symbol_settings"]
    assert e["general"] == {"alias": "", "user_description": "secret note", "enabled": True,
                            "virtual_equity_pct": 10.0, "priority": 1, "account_id": 3}
    assert e["enter_market_ruleset_name"] == "EM" and e["open_positions_ruleset_name"] is None


def test_envelope_and_constants():
    env = build_batch_envelope([{"x": 1}], exported_at="2026-01-01T00:00:00")
    assert env == {"export_version": EXPORT_VERSION, "export_type": EXPORT_TYPE,
                   "export_timestamp": "2026-01-01T00:00:00", "experts": [{"x": 1}]}
    assert RULESET_SLOTS[0] == ("enter_market_ruleset_id", "enter_market_ruleset_name")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_expert_batch_builder.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create the builder**

`packages/common/ba2_common/export/expert_batch.py`:

```python
"""The ``expert_batch`` v1.0 export format (live expert settings + rules), as built by
ba2_trade_platform/core/expert_batch_export_import.py. Moved here (site plan P0a) so the public
site emits the same file from an imported live-DB copy. Pure."""
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

EXPORT_TYPE = "expert_batch"
EXPORT_VERSION = "1.0"

#: The two slots an expert can point a ruleset at, and the payload key naming each one.
RULESET_SLOTS = (
    ("enter_market_ruleset_id", "enter_market_ruleset_name"),
    ("open_positions_ruleset_id", "open_positions_ruleset_name"),
)


def build_expert_batch_entry(*, expert_type: str, alias: Optional[str],
                             user_description: Optional[str], enabled: Any,
                             virtual_equity_pct: Any, priority: Any, account_id: Any,
                             ruleset_names: Dict[str, Optional[str]],
                             rulesets_export: Optional[Dict[str, Any]],
                             expert_settings: Dict[str, Any],
                             symbol_settings: Dict[str, Any]) -> Dict[str, Any]:
    """One ``experts[]`` entry. ``ruleset_names`` maps each RULESET_SLOTS name key to the
    ruleset's name (or None); ``rulesets_export`` is a ``rulesets_export_envelope`` or None."""
    entry: Dict[str, Any] = {
        "expert_type": expert_type,
        "general": {
            "alias": alias or "",
            "user_description": user_description,
            "enabled": enabled,
            "virtual_equity_pct": virtual_equity_pct,
            "priority": priority,
            "account_id": account_id,
        },
    }
    for _id_attr, name_key in RULESET_SLOTS:
        entry[name_key] = ruleset_names.get(name_key)
    entry["rulesets"] = rulesets_export
    entry["expert_settings"] = dict(expert_settings)
    entry["symbol_settings"] = symbol_settings
    return entry


def build_batch_envelope(entries: Iterable[Dict[str, Any]],
                         exported_at: Optional[str] = None) -> Dict[str, Any]:
    return {
        "export_version": EXPORT_VERSION,
        "export_type": EXPORT_TYPE,
        "export_timestamp": exported_at or datetime.now().isoformat(),
        "experts": list(entries),
    }
```

- [ ] **Step 4: Delegate from the trade app**

In `ba2_trade_platform/core/expert_batch_export_import.py`:
- Replace the `EXPORT_TYPE`, `EXPORT_VERSION` and `_RULESET_SLOTS` definitions (lines 44-52) with:

```python
from ba2_common.export.expert_batch import (  # noqa: E402
    EXPORT_TYPE, EXPORT_VERSION, RULESET_SLOTS as _RULESET_SLOTS,
    build_batch_envelope, build_expert_batch_entry,
)
```
- In `build_batch_export`, replace the returned dict with `return build_batch_envelope(experts)`.
- Rewrite `_export_one` to gather its inputs as it does today, then build the entry:

```python
def _export_one(instance: ExpertInstance) -> Dict[str, Any]:
    ruleset_ids: List[int] = []
    ruleset_names: Dict[str, Optional[str]] = {}
    for id_attr, name_key in _RULESET_SLOTS:
        ruleset_id = getattr(instance, id_attr, None)
        name = None
        if ruleset_id:
            try:
                ruleset = get_instance(Ruleset, ruleset_id)
            except InstanceNotFound:
                # A dangling id (its ruleset was deleted) must not fail the whole export --
                # the table itself already tolerates one and shows '(Not found)'.
                logger.warning(f"Expert {instance.id} references missing ruleset {ruleset_id}; "
                               f"exporting the slot as empty")
            else:
                name = ruleset.name
                if ruleset_id not in ruleset_ids:
                    ruleset_ids.append(ruleset_id)
        ruleset_names[name_key] = name

    expert = get_expert_instance_from_id(instance.id)
    if expert is None:
        raise ValueError(f"Expert instance {instance.id} ({instance.alias}) could not be built; "
                         f"its settings cannot be exported")
    return build_expert_batch_entry(
        expert_type=instance.expert,
        alias=instance.alias,
        user_description=instance.user_description,
        enabled=instance.enabled,
        virtual_equity_pct=instance.virtual_equity_pct,
        priority=getattr(instance, "priority", 1),
        account_id=instance.account_id,
        ruleset_names=ruleset_names,
        rulesets_export=(RulesExporter.export_multiple_rulesets(ruleset_ids)
                         if ruleset_ids else None),
        expert_settings=dict(expert.settings),
        symbol_settings=(expert._get_enabled_instruments_config()
                         if hasattr(expert, "_get_enabled_instruments_config") else {}),
    )
```

Check the order of side effects: the original built the rulesets export **before** instantiating the expert, and raised `ValueError` after it. Here `get_expert_instance_from_id` runs first. Both are reads, so the only observable difference would be which error surfaces first when **both** the ruleset lookup and the expert build fail. To keep that exact order, compute `rulesets_export = RulesExporter.export_multiple_rulesets(ruleset_ids) if ruleset_ids else None` into a local variable **before** the `expert = …` line, and pass the local.

- [ ] **Step 5: Run the builder test and the batch suites**

```bash
~/ba2-venvs/trade/bin/python -m pytest packages/common/tests/test_expert_batch_builder.py tests/test_expert_batch_export_import.py tests/test_batch_import_upload.py -q
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/common/ba2_common/export/expert_batch.py packages/common/tests/test_expert_batch_builder.py ba2_trade_platform/core/expert_batch_export_import.py
git commit -m "refactor(export): expert_batch v1.0 entry/envelope builders move to ba2_common

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Whole-repo verification and real-DB identity check

**Files:** none (verification only)

- [ ] **Step 1: Real-DB after-capture and diff**

```bash
DATABASE_URL=sqlite:////tmp/ba2-test-copy.db ~/ba2-venvs/test/bin/python tools/export_golden_capture.py /tmp/ba2-export-after.json
diff /tmp/ba2-export-before.json /tmp/ba2-export-after.json && echo IDENTICAL
cp /tmp/ba2-backups/dev_2026-09-28.sqlite /tmp/ba2-live-copy.sqlite
BA2_HOME=/tmp/ba2-backups/home DB_FILE=/tmp/ba2-live-copy.sqlite ~/ba2-venvs/trade/bin/python tools/live_export_capture.py /tmp/ba2-live-after.json 2>&1 | grep -v "^20[0-9][0-9]-"
rm -f /tmp/ba2-live-copy.sqlite*
diff /tmp/ba2-live-before.json /tmp/ba2-live-after.json && echo LIVE_IDENTICAL
```
Expected: `IDENTICAL` and `LIVE_IDENTICAL`. Any difference is a regression; fix it in the task that introduced it.

- [ ] **Step 2: Import-linter**

```bash
~/ba2-venvs/trade/bin/python -m pip show import-linter >/dev/null 2>&1 || ~/ba2-venvs/trade/bin/python -m pip install "import-linter>=2.0"
cd packages/common && ~/ba2-venvs/trade/bin/lint-imports; cd -
```
Expected: `Contracts: 1 kept, 0 broken.`

- [ ] **Step 3: All four suites**

```bash
~/ba2-venvs/trade/bin/python -m pytest tests -q
~/ba2-venvs/trade/bin/python -m pytest packages/common/tests -q
~/ba2-venvs/trade/bin/python -m pytest packages/experts/tests -q
cd testplatform/backend && ~/ba2-venvs/test/bin/python -m pytest tests -q; cd -
```
Expected: no new failures compared with `dev`. If any suite fails, check out `dev` in a worktree, run the same suite there, and compare. A failure that also happens on `dev` is pre-existing; list it in the hand-off. Anything that is new must be fixed.

- [ ] **Step 4: Report**

Report to the user: the branch name, the commit list (`git log --oneline dev..HEAD`), the IDENTICAL result, the suite results, and any pre-existing failures. Do not merge or push; the finishing-a-development-branch skill decides that with the user.
