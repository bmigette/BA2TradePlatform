"""Process-level review: a daemon remote fallback owns a wedged child at exit.

Runs actual launcher teardown helpers with a spawn process pool and a sleeping task.
The task has no broker, database, market-data or application side effects.
"""
from __future__ import annotations

import ast
import json
import multiprocessing
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def child():
    tree = ast.parse((ROOT / 'testplatform/ba2test_launcher.py').read_text(encoding='utf-8'))
    names = {'_submit_daemon', '_run_local_fallback_bounded', '_kill_executor'}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {'Dict': dict, 'Any': object, 'Optional': __import__('typing').Optional}
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'actual_launcher_helpers', 'exec'), namespace)
    # Replace only worker payload/initialization: preserve real process ownership and teardown.
    handler = ModuleType('app.services.strategy_optimization_handler')
    handler._persist_trial_worker = time.sleep
    sys.modules[handler.__name__] = handler
    namespace['_new_local_pool'] = lambda n: ProcessPoolExecutor(
        max_workers=n, mp_context=multiprocessing.get_context('spawn'))
    deadline = time.monotonic() + 0.4
    future = namespace['_submit_daemon'](namespace['_run_local_fallback_bounded'], 8, deadline)
    time.sleep(0.35)
    assert not future.done(), 'fallback must still be running when main exits'
    assert multiprocessing.active_children(), 'probe must have an actual child process'
    print('main exiting just before export deadline with a live fallback child', flush=True)


if __name__ == '__main__':
    if '--child' in sys.argv:
        child()
    else:
        started = time.monotonic()
        result = subprocess.run([sys.executable, __file__, '--child'], capture_output=True,
                                text=True, timeout=15)
        evidence = {'returncode': result.returncode, 'elapsed_seconds': time.monotonic() - started,
                    'stdout': result.stdout, 'stderr': result.stderr}
        assert result.returncode == 0, evidence
        assert evidence['elapsed_seconds'] < 6, evidence
        destination = ROOT / 'reports/review_evidence/synced-7f2f58a5-2026-09-21'
        destination.mkdir(parents=True, exist_ok=True)
        (destination / 'bounded-fallback.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence, indent=2))
