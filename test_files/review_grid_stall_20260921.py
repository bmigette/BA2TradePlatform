"""Isolated review of remote commit 96cb472f; no DB/provider/trading access.

Pass its handler and fitness source snapshots, plus the matching launcher source.
Executes unchanged AST nodes with small dependency fixtures. Real worker tests use
only this script's sleeping/returning tasks, never backtests or the running grid.
Assertions confirm observed behavior (including defects), not desired behavior.
"""
from __future__ import annotations

import ast
import json
import logging
import multiprocessing
import os
import sys
import time
import types
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional


def tiny_trial(config, metric):
    time.sleep(config['sleep'])
    return {'ok': True, 'fitness': 7.0}


def load_nodes(nodes, namespace, filename):
    tree = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(tree, filename, 'exec'), namespace)


def main():
    handler, fitness_path, launcher = map(Path, sys.argv[1:4])
    source = ast.parse(handler.read_text(encoding='utf-8'))
    launch_source = ast.parse(launcher.read_text(encoding='utf-8'))
    nodes = {n.name: n for n in ast.walk(source)
             if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    constant = next(n for n in ast.parse(fitness_path.read_text(encoding='utf-8')).body
                    if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'STALLED_SENTINEL' for t in n.targets))
    namespace = dict(globals())
    namespace.update(logger=logging.getLogger('offline-grid-review'), _os=os)
    namespace['logger'].disabled = True
    load_nodes([constant, nodes['_SlotPools'], nodes['_local_execute_jobs'],
                nodes['_elite_slice'], nodes['_seed_all_results_from_checkpoint']], namespace, str(handler))
    sentinel = namespace['STALLED_SENTINEL']
    fitness_module = types.ModuleType('app.services.strategy_fitness')
    fitness_module.STALLED_SENTINEL = sentinel
    fitness_module.ZERO_TRADE_SENTINEL = -1e9
    sys.modules[fitness_module.__name__] = fitness_module
    diagnostics = types.ModuleType('app.services.distributed_eval')
    diagnostics._log_memory_diagnostics = lambda *args: None
    sys.modules[diagnostics.__name__] = diagnostics
    namespace.update(opt=NS(fitness_metric='fixture'), _trial_worker=tiny_trial)
    os.environ['BT_LOCAL_STALL_TIMEOUT_S'] = '2'
    evidence = {'commit': '96cb472f', 'checks': []}

    # Actual recovery primitive + actual dispatcher, with real spawned processes.
    for slots in (1, 2):
        pools = namespace['_SlotPools'](
            lambda: ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn')),
            slots, 0)
        namespace['_pool'] = pools
        retired = []
        try:
            for i in range(slots):
                warm = pools.submit(i, tiny_trial, {'sleep': 0}, 'fixture')
                warm.result(timeout=15)
                pools.mark_done(warm)
                retired.extend(pools.pools[i]._processes.values())
            jobs = [(i, {'id': i}, f'k{i}', {'sleep': 60}) for i in range(slots)]
            jobs.append((slots, {'id': slots}, 'good', {'sleep': 0}))
            start = time.monotonic()
            results = list(namespace['_local_execute_jobs'](jobs))
            elapsed = time.monotonic() - start
            assert sum(r[3].get('stalled', False) for r in results) == slots
            assert results[-1][3]['ok'] and results[-1][3]['fitness'] == 7
            for child in retired:
                child.join(timeout=5)
                assert not child.is_alive()
            evidence['checks'].append({'recovery_slots': slots, 'seconds': round(elapsed, 3),
                                       'stalled_results': slots, 'subsequent_trial': 'completed',
                                       'retired_workers_alive': 0})
        finally:
            # Only processes explicitly created by this probe are ever terminated.
            for pool in pools.pools:
                for child in list((pool._processes or {}).values()):
                    if child.is_alive():
                        child.terminate()
            for child in retired:
                if child.is_alive():
                    child.kill()
            pools.shutdown(wait=True, cancel_futures=True)

    class Memo:
        def __init__(self):
            self.data = {}
            self.hits = self.misses = 0
        def get(self, key):
            return self.data.get(key)
        def put(self, key, value):
            self.data[key] = value

    callbacks, dispatched = [], []
    namespace.update(
        POOL=None, tq=NS(is_task_paused=lambda _: False, update_progress=lambda *a: None),
        task_id='fixture', ga={'generations': 3}, gen_state={'gen': 0}, memo=Memo(),
        _trial_key_for=lambda flat: str(flat['id']),
        _report_trial_result=lambda fn, i, score: fn(i, score) if fn else None,
        _build_daily_trial_config=lambda *a: {}, _maybe_mark_want_full=lambda cfg, last: cfg,
        backtest_cfg={}, strategy=None, hoisted=None, decode_params=lambda s, f: f,
        best={'fitness': None, 'params': None},
        _cost_model=NS(order=lambda jobs: jobs, report=lambda *a: None),
        _TrialCostModel=NS(width=lambda cfg: 0), all_results=[],
        _persist_live=lambda *a: None, _evaluator=None, _MAX_TASKS_PER_CHILD=0,
    )
    factory = ast.parse('def bind_batch():\n    _pool = POOL\n').body[0]
    factory.body += [nodes['make_batch_fitness'], ast.Return(value=ast.Name(id='make_batch_fitness', ctx=ast.Load()))]
    load_nodes([factory], namespace, str(handler))

    def stall_everything(jobs):
        dispatched.extend(jobs)
        for i, flat, key, _ in jobs:
            yield i, flat, key, {'ok': False, 'stalled': True, 'error': 'fixture timeout'}

    batch = namespace['bind_batch']()(stall_everything)
    assert batch([{'id': 1}], lambda i, f: callbacks.append((i, f))) == [sentinel]
    assert batch([{'id': 1}], lambda i, f: callbacks.append((i, f))) == [sentinel]
    assert len(dispatched) == 1 and len(namespace['all_results']) == 1
    evidence['checks'].append({'batch_memo_and_callback': 'pass', 'executions_for_two_identical_requests': 1})

    # Execute the real finalization tail with only stalled results.
    handler_node = nodes['handle_strategy_optimization']
    main_try = next(n for n in handler_node.body if isinstance(n, ast.Try))
    guard_idx = next(i for i, n in enumerate(main_try.body)
                     if isinstance(n, ast.If) and ast.unparse(n.test) == 'not all_results')
    finalizer = ast.parse('def finish_probe():\n    pass\n').body[0]
    finalizer.body = main_try.body[guard_idx:]
    cleared = []
    namespace.update(fatal={'msg': None}, _fail=lambda *a: {'status': 'failed'},
                     opt_id=99, db=NS(commit=lambda: None), ckpt_task_id='fixture',
                     _clear_checkpoint=cleared.append, result={'best_params': {'id': 1}, 'best_fitness': sentinel},
                     push_optimization=lambda *a: None, last_gen_full_results={}, _last_gen_full_results_by_opt={})
    load_nodes([finalizer], namespace, str(handler))
    finished = namespace['finish_probe']()
    assert finished['status'] == 'completed' and cleared == ['fixture']
    evidence['checks'].append({'all_stalled_final_status': finished['status'],
                               'best_fitness': finished['best_fitness'], 'checkpoint_cleared': True})

    # Execute the real export ranking block. No backtests or persistence are invoked.
    persist = next(n for n in launch_source.body if isinstance(n, ast.FunctionDef) and n.name == '_persist_top_backtests')
    body = next(n for n in persist.body if isinstance(n, ast.Try)).body
    start = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                 and ast.unparse(n.targets[0]) == '(seen, ranked)')
    ranking = body[start:start + 3]
    namespace.update(n=5, _json=json)
    namespace['opt'].all_results = [
        {'params': {'id': i}, 'fitness': fit, 'key': str(i)}
        for i, fit in enumerate([0.0, -1e8, -1e9, -2e9, sentinel])]
    load_nodes(ranking, namespace, str(launcher))
    assert namespace['ranked'][-1][2] == sentinel
    evidence['checks'].append({'export_top_5_scores': [r[2] for r in namespace['ranked']],
                               'stalled_candidate_selected_for_rerun': True})

    successful = [{'fitness': float(i), 'key': str(i)} for i in range(25)]
    stalled = {'fitness': sentinel, 'key': 'stalled'}
    restored = []
    namespace['_seed_all_results_from_checkpoint'](
        {'top_results': namespace['_elite_slice'](successful + [stalled], 20)}, restored)
    assert not any(r['key'] == 'stalled' for r in restored)
    evidence['checks'].append({'checkpoint_elites_preserve_stall_record': False,
                               'scope': 'resumed all_results (old optimization row may retain the original record)'})
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
