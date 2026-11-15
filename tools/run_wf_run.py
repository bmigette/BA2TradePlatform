#!/usr/bin/env python
"""Driver of ONE stored walk-forward run (a ``walk_forward_runs`` row created in the UI).

Started by the test platform's walk-forward queue (app.services.wf_runs.handle_walk_forward), one
process per run, so the chain's train jobs, out-of-sample re-runs, launcher globals and logging state
never live inside the API server. It runs the SAME chain as the goal2027 grid: the stored
``wf_chain.CellSpec``, re-validated by ``wf_runs.preflight``, through ``wf_chain.run_cell`` with
tools/run_walk_forward.py's ``RealRunner``; the chain writes its report files under the run's report
directory and keeps the row current.

  python tools/run_wf_run.py --run-id 12 --token <the row's driver token> [--task-id T]

The token must match the row's ``driver_token`` (set by create / resume): a stale start never drives a
run a newer start owns. Exit codes: 0 the chain finished (any verdict), 2 refused, 1 a run failed.
"""
from __future__ import annotations

import argparse
import os
import sys
from types import SimpleNamespace

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKEND = os.path.join(_REPO, "testplatform", "backend")
for _p in (_BACKEND, os.path.join(_REPO, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--task-id", default=None)
    ap.add_argument("--report-dir", default=None, help="default: the row's report directory")
    ap.add_argument("--min-free-gb", type=float, default=20.0,
                    help="memory floor of the out-of-sample re-runs (as tools/run_walk_forward.py)")
    ap.add_argument("--force-memory", action="store_true")
    a = ap.parse_args(argv)

    import run_walk_forward as RWF
    from app.services import walk_forward as WF
    from app.services import wf_runs

    runner = RWF.RealRunner(SimpleNamespace(**RWF.selection_defaults(), min_free_gb=a.min_free_gb,
                                            force_memory=a.force_memory))
    runner.launcher_module()                   # bootstrap: backend path, DB wiring, init_db()
    try:
        res = wf_runs.drive(a.run_id, a.token, runner, task_id=a.task_id, report_dir=a.report_dir)
    except WF.WalkForwardError as e:
        print(f"run_wf_run: {e}", file=sys.stderr, flush=True)
        return 2
    print(f"run_wf_run: {res.name}: verdict {res.verdict}"
          + (f", refit {res.refit_job}" if res.refit_job else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
