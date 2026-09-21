"""Offline follow-up review: execute current source nodes with fixture dependencies.

No market data, backtests or application databases are used. The remote-export
case runs in a test-owned subprocess with mock HTTP failures and a sleeping
fallback; the parent kills only that subprocess after verifying the exit hang.
Assertions document observed defects, not desired regression behavior.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / 'testplatform/backend/app/services/strategy_optimization_handler.py'
LAUNCHER = ROOT / 'testplatform/ba2test_launcher.py'
QUEUE = ROOT / 'testplatform/backend/app/services/task_queue.py'
DRIVER = ROOT / 'tools/run_options_matrix.py'


def parse(path):
    return ast.parse(path.read_text(encoding='utf-8'))


def function(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def execute(nodes, namespace, filename):
    tree = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(tree, str(filename), 'exec'), namespace)


def synthetic_trial(config):
    if config.get('sleep'):
        time.sleep(60)
    return {'ok': True}


def failed_remote(*args, **kwargs):
    raise RuntimeError('fixture connection failure')


def stub_module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module


def export_child():
    """Execute the export dispatcher/teardown and the real remote fallback function."""
    source = parse(LAUNCHER)
    persist = function(source, '_persist_top_backtests')
    body = next(n for n in persist.body if isinstance(n, ast.Try)).body
    start = next(i for i, n in enumerate(body)
                 if isinstance(n, ast.If) and ast.unparse(n.test) == 'not specs')
    wrapper = ast.parse('def execute_export():\n    persisted = 0\n').body[0]
    wrapper.body += body[start:]
    stub_module('app.services.strategy_optimization_handler',
                _BACKEND_DIR='', _WORKER_ENV_KEYS=(), _worker_init=None,
                _persist_trial_worker=synthetic_trial)
    stub_module('app.services.worker_client', run_trial_full=failed_remote)
    namespace = dict(globals())
    namespace.update(
        specs=[(1, {'sleep': False}, {}), (2, {'sleep': True}, {})],
        ranked=[1, 2], n_local=1, remote_workers=[{'name': 'mock-remote'}],
        opt=NS(fitness_metric='fixture'), _persist_trial_worker=synthetic_trial,
        _persist_one=lambda *args: True, _REMOTE_RETRY_BACKOFF_S=0,
    )
    execute([function(source, '_kill_executor'), function(source, '_remote_then_local'), wrapper],
            namespace, LAUNCHER)
    os.environ['BT_LOCAL_STALL_TIMEOUT_S'] = '1'
    result = namespace['execute_export']()
    print(f'EXPORT_RETURNED persisted={result}', flush=True)
    # Natural interpreter exit now waits for the still-running ThreadPoolExecutor fallback.


def main():
    source = parse(HANDLER)
    launcher = parse(LAUNCHER)
    namespace = dict(globals())
    execute([function(source, '_count_measured'), function(source, '_final_status'),
             function(launcher, '_rank_measured_candidates')], namespace, HANDLER)
    stub_module('app.services.strategy_fitness', STALLED_SENTINEL=-3e9)
    evidence = {'commit': '441771e8', 'checks': []}

    assert namespace['_final_status']([{'fitness': -3e9, 'status': 'stalled'}]) == 'no_measurements'
    legacy = [{'fitness': -3e9, 'fitness_raw': -3e9, 'key': 'old-stall', 'params': {'a': 1}}]
    legacy_status = namespace['_final_status'](legacy)
    assert legacy_status == 'completed'
    ranked, _ = namespace['_rank_measured_candidates'](legacy, 5, {'a': 1}, -3e9)
    assert ranked == []
    evidence['checks'].append({'legacy_stall_final_status': legacy_status,
                               'legacy_stall_export_candidates': len(ranked),
                               'new_stall_final_status': 'no_measurements'})
    try:
        namespace['_rank_measured_candidates']([{'params': {'a': 1}, 'fitness': None}], 5, None, None)
    except NameError as error:
        assert '_json' in str(error)
        evidence['checks'].append({'missing_fitness_export': str(error)})
    else:
        raise AssertionError('Expected missing _json binding')

    row = NS(status='running', error_message=None)
    session = NS(query=lambda *_: NS(filter=lambda *_: NS(first=lambda: row)),
                 commit=lambda: None, close=lambda: None)
    logger = logging.getLogger('offline-stall-recheck')
    logger.disabled = True
    queue_env = dict(globals())
    queue_env.update(SessionLocal=lambda: session, TaskQueue=NS(task_id='fixture'), logger=logger,
                     TaskStatus=NS(FAILED=NS(value='failed'), COMPLETED=NS(value='completed')))
    execute([function(parse(QUEUE), '_process_task_inline')], queue_env, QUEUE)
    service = NS(_handlers={'strategy_optimization': lambda *args: {
        'status': 'no_measurements', 'error': 'no measured trials: fixture'}}, _active_tasks={'fixture': True})
    queue_env['_process_task_inline'](service, NS(task_id='fixture', task_type='strategy_optimization', payload={}), 'fixture')
    assert row.status == 'completed' and row.error_message is None
    evidence['checks'].append({'queued_no_measurements_status': row.status,
                               'queued_error_message': row.error_message})

    driver = parse(DRIVER)
    with tempfile.TemporaryDirectory(prefix='ba2-stale-grid-review-') as tmp:
        path = Path(tmp) / 'fixture.sqlite'
        con = sqlite3.connect(path)
        con.execute('CREATE TABLE strategy_optimizations (id INTEGER, name TEXT, error_message TEXT)')
        con.execute('INSERT INTO strategy_optimizations VALUES (1, ?, ?)',
                    ('job-A', 'no measured trials: previous launch'))
        con.commit()
        con.close()
        launches = []
        def launch(cmd, **kwargs):
            launches.append(cmd[0])
            return NS(returncode=1 if cmd[0] == 'job-A' else 0)
        env = dict(globals())
        env.update(
            NO_MEASUREMENT_MARKER='no measured trials', _db_path=lambda: str(path),
            build_parser=lambda: None,
            resolve_args=lambda *a: NS(experts='fixture', strategies='fixture', universe_file='',
                                      launcher='fixture', profile='legacy', fitness='fixture',
                                      robust_fitness=True, dry_run=False, screener_gate_store=None),
            _universe=lambda *a: 'XYZ', _completed_names=lambda: set(),
            planned_jobs=lambda *a: [('job-A', 'fixture', 'fixture', 'legacy'),
                                    ('job-B', 'fixture', 'fixture', 'legacy')],
            build_cmd=lambda args, launcher, name, *a: [name],
            subprocess=NS(run=launch),
        )
        execute([function(driver, '_failure_reason'), function(driver, 'main')], env, DRIVER)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = env['main']([])
        assert launches == ['job-A', 'job-B'] and rc == 0
        evidence['checks'].append({'stale_failure_marker_masks_new_launch_failure': True,
                                   'jobs_launched': launches, 'matrix_exit_code': rc,
                                   'note': 'New failed launch writes no optimization row; only an old marker exists.'})

    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--export-child'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    hung = False
    try:
        out, err = child.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        hung = True
        child.kill()  # Only this probe's own subprocess, never grid workers.
        out, err = child.communicate(timeout=10)
    assert 'EXPORT_RETURNED persisted=1' in out, (out, err)
    assert hung, 'Expected remaining remote fallback thread to block process exit'
    evidence['checks'].append({'export_bound_seconds': 1, 'export_returned': True,
                               'launcher_still_alive_after_seconds': 5,
                               'cause': 'remote fallback thread survives wait=False shutdown',
                               'transcript': out.strip()})
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    if '--export-child' in sys.argv:
        export_child()
    else:
        main()
