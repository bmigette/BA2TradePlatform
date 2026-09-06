#!/usr/bin/env python
"""Re-run selected backtests under a STATIC capital base and label them ``ok1000``.

    .venv/Scripts/python.exe tools/run_ok1000.py --list
    .venv/Scripts/python.exe tools/run_ok1000.py --only 1244        # smoke: one run
    .venv/Scripts/python.exe tools/run_ok1000.py --all              # the shortlist

WHY. A backtest compounds, so a strategy that did well early deploys larger positions ever after
and its later years are carried by its earlier luck. ``equity_cap`` holds the capital still, so
the result is about the strategy rather than about when it started. These runs answer: does the
edge survive on a fixed $1000, and how much of that $1000 does it actually use?

THE SOURCE ROWS ARE NEVER OVERWRITTEN. Each run creates a NEW ``Backtest`` row; the parent keeps
its metrics untouched, because the un-capped result is the baseline the capped one is judged
against. The parent is only ever LABELLED (``ok1000-source``), which is additive.

WHY NOT THE ``rerun_backtest`` QUEUE. That handler re-runs a row IN PLACE, overwriting its
results -- the one thing this must not do. It also routes standalone rows through
``_build_standalone_rerun_config``, which assembles its payload key by key and never sets
``equity_cap``, so a cap requested that way is silently dropped and the run comes back looking
identical to its baseline. This tool therefore drives ``run_daily_backtest`` directly and injects
the cap into ``account_settings``, which IS the path the optimize CLI's ``--equity-cap`` uses
(launcher -> account_settings -> _build_daily_trial_config forwards it wholesale ->
BacktestAccount reads ``settings["equity_cap"]``).

SMOKE FIRST. ``--only`` runs a single id and prints what the account actually saw, because this
feature has never run on real settings: 0 rows in the whole DB mention ``equity_cap``. Confirm
the sizer really is working against $1000 before spending the batch.
"""
import argparse
import copy
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "testplatform"))


def _bootstrap():
    """Reuse the launcher's own startup, don't re-implement it.

    A fresh CLI process points ba2_common at its NEUTRAL default DB (BA2_HOME/db.sqlite) rather
    than the test DB, so `get_app_setting` returns nothing and the first provider call dies with
    "FMP API key not configured" -- which is exactly what this script did on its first smoke run.
    `_enter_backend` is the one place that sorts it: sys.path + chdir, .env, `configure_db` onto
    the test DB, and mirroring FMP_API_KEY from app settings into the env.
    """
    from ba2test_launcher import _enter_backend
    return _enter_backend()


_bootstrap()

CAP = 1000.0
LABEL = "ok1000"
SOURCE_LABEL = "ok1000-source"
DOC = ROOT / "docs" / "plans" / "2026-09-06-ok1000-runs.md"

#: The shortlist: 2 per (expert, cap band) -- one profit-leaning [P], one robustness-leaning [R]
#: -- each clearing trades>=50, top-5 concentration<40%, avg capital use<50%, and deduped so a
#: risk_atr/notional twin never spends both slots of a cell.
#:
#: FMPRating carries THREE (1070 mid, 1178 + 1182 large), approved on the low-capital-usage
#: profile: 1070 is the lowest usage at 22.1% (CAR 16.7), 1182 the best profit at CAR 17.5 with
#: the lowest concentration (28.4%), 1178 the best risk-adjusted at calmar 1.77 / DD -9.0.
#: 1396 (Senate) is EXCLUDED: it predates the disclosure-lookahead fix, so its numbers are void.
SHORTLIST = [1001, 1064, 1065, 1070, 1088, 1107, 1114, 1173, 1175, 1178,
             1182, 1225, 1243, 1244, 1275, 1285, 1298, 1363, 1364]


def _labels(raw):
    """The labels column is a JSON LIST on most rows and a PLAIN STRING on ~65 of them."""
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return list(raw)
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    return list(v) if isinstance(v, list) else [str(v)]


def _add_label(row, label):
    ls = _labels(row.labels)
    if label not in ls:
        ls.append(label)
        row.labels = ls
    return ls


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, help="run ONE parent backtest id (smoke test)")
    ap.add_argument("--all", action="store_true", help="run the whole shortlist")
    ap.add_argument("--list", action="store_true", help="show the shortlist and exit")
    ns = ap.parse_args()

    if ns.list or not (ns.only or ns.all):
        print(f"shortlist ({len(SHORTLIST)}): {SHORTLIST}")
        print(f"cap = ${CAP:,.0f}   label = {LABEL}   doc = {DOC}")
        return 0

    from app.models.backtest import Backtest
    from app.models.database import SessionLocal
    from app.services.backtest.daily_backtest_handler import _persist_results, run_daily_backtest
    from app.services.backtest.rerun_handler import build_rerun_config

    targets = [ns.only] if ns.only else list(SHORTLIST)
    db = SessionLocal()
    done = []
    try:
        for pid in targets:
            parent = db.query(Backtest).filter(Backtest.id == pid).first()
            if parent is None:
                print(f"  !! parent {pid} not found, skipping")
                continue
            if SOURCE_LABEL in _labels(parent.labels) and not ns.only:
                # RESUMABLE. The batch is ~18 full backtests; a re-invocation after an
                # interruption must not spend an hour re-running what already finished, nor
                # leave a second OK1000 row for the same parent.
                print(f"  -- parent {pid} already has an ok1000 run, skipping")
                continue
            try:
                config = build_rerun_config(db, parent)
            except (KeyError, ValueError) as e:
                print(f"  !! {pid}: cannot rebuild config: {e}")
                continue

            config = copy.deepcopy(config)
            acct = dict(config.get("account_settings") or {})
            acct["equity_cap"] = CAP
            config["account_settings"] = acct

            child = Backtest(
                name=f"OK1000-{parent.name}"[:255],
                engine_type="daily_expert",
                expert_name=parent.expert_name,
                optimization_id=None,          # standalone, like the robustness variants
                strategy_id=parent.strategy_id,
                # RECORD THE CAP ON THE ROW. Without it the row is not self-describing: its
                # `initial_capital` column still reads the parent's $10,000 while the run
                # actually deployed against $1,000, so anyone computing a return from the
                # column alone gets it wrong by 10x. (Note the standalone re-run path does NOT
                # read this back -- `_build_standalone_rerun_config` assembles its payload key
                # by key and never sets equity_cap -- so a plain re-run of THIS row would come
                # back uncapped. Recorded for provenance, not for round-tripping.)
                strategy_params={**(parent.strategy_params or {}), "equityCap": CAP},
                start_date=parent.start_date,
                end_date=parent.end_date,
                initial_capital=parent.initial_capital,
                position_sizing_type=parent.position_sizing_type,
                position_sizing_value=parent.position_sizing_value,
                commission=parent.commission,
                slippage=parent.slippage,
                fitness_metric=parent.fitness_metric,
                status="running",
                is_saved=False,
            )
            _add_label(child, LABEL)
            db.add(child)
            db.commit()
            db.refresh(child)
            config["backtest_id"] = child.id
            config["name"] = child.name

            print(f"  -> parent {pid} => new row {child.id}  (cap ${CAP:,.0f}) ...", flush=True)
            try:
                results = run_daily_backtest(config)
            except Exception as e:                      # noqa: BLE001 - report and continue
                child.status = "failed"
                child.error_message = str(e)[:900]
                db.commit()
                print(f"     FAILED: {type(e).__name__}: {e}")
                continue

            _persist_results(db, child, results)
            child.status = "completed"
            child.completed_at = datetime.now()
            _add_label(parent, SOURCE_LABEL)           # additive; parent RESULTS untouched
            db.commit()
            done.append((pid, child.id, results.get("total_return"),
                         results.get("max_drawdown"), results.get("total_trades")))
            print(f"     done: return={results.get('total_return')}% "
                  f"dd={results.get('max_drawdown')}% trades={results.get('total_trades')}")

        _write_doc(done)
    finally:
        db.close()
    print(f"\n{len(done)} run(s) completed. Tracked in {DOC}")
    return 0


def _write_doc(done):
    if not done:
        return
    head = (f"# ok1000 static-balance runs\n\n"
            f"Each row re-runs a saved backtest's settings against a FIXED ${CAP:,.0f} capital "
            f"base (`equity_cap`), so the result is about the strategy rather than about how "
            f"much the account had grown by. Source rows are NEVER overwritten: each run is a "
            f"NEW row labelled `{LABEL}`, and the parent is only labelled `{SOURCE_LABEL}`.\n\n"
            f"| parent | ok1000 row | return % | max DD % | trades |\n"
            f"|---|---|---|---|---|\n")
    body = "".join(f"| {p} | {c} | {r} | {d} | {n} |\n" for p, c, r, d, n in done)
    prev = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
    if prev.startswith("# ok1000"):
        DOC.write_text(prev.rstrip() + "\n" + body, encoding="utf-8")
    else:
        DOC.write_text(head + body, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
