"""Offline review probes for 7f2f58a5; assertions describe observed behavior, not fixes.

Only an in-memory database and mock quote sources are used. No production state is touched.
Run with the trade venv. Evidence is written under reports/review_evidence/.
"""
from __future__ import annotations

import ast
import json
import runpy
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'testplatform/backend'))


def ranking_probe():
    source = ast.parse((ROOT / 'testplatform/ba2test_launcher.py').read_text(encoding='utf-8'))
    function = next(n for n in source.body if isinstance(n, ast.FunctionDef)
                    and n.name == '_rank_measured_candidates')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'actual_launcher', 'exec'), namespace)
    rank = namespace[function.name]
    from app.services.strategy_fitness import is_measured_result

    results = {}
    for label, records in {
        'mixed_numeric_and_string': [{'params': {'x': 1}, 'fitness': 4.0},
                                     {'params': {'x': 2}, 'fitness': 'high'}],
        'non_dict_record': [None, {'params': {'x': 1}, 'fitness': 4.0}],
    }.items():
        try:
            results[label] = {'result': rank(records, 5, None, None)}
        except Exception as exc:
            results[label] = {'error': type(exc).__name__, 'message': str(exc)}
    assert results['mixed_numeric_and_string']['error'] == 'TypeError'
    assert results['non_dict_record']['error'] == 'AttributeError'
    results['missing_best_fitness_fallback'] = rank([], 5, {'x': 1}, None)[0]
    results['nonfinite_considered_measured'] = {
        'nan': is_measured_result({'fitness': float('nan')}),
        'infinity': is_measured_result({'fitness': float('inf')}),
    }
    assert results['missing_best_fitness_fallback'] == [({'x': 1}, None, None)]
    return results


def ui_probe():
    fixture = runpy.run_path(str(ROOT / 'tests/test_option_tab_loader.py'))
    from ba2_trade_platform.core.models import Transaction
    from ba2_trade_platform.ui.components.LiveTradesTable import LiveTradesTable
    from ba2_trade_platform.ui.pages import option_trades
    from sqlmodel import select

    results = {}
    for sort_by in ('current_pnl', 'current_pnl_numeric', 'strategy'):
        engine, session = fixture['_seed_database'](3)
        transactions = session.exec(select(Transaction).order_by(Transaction.id)).all()
        for i, txn in enumerate(transactions):
            txn.created_at = datetime(2026, 9, 1) + timedelta(days=i)
            txn.option_strategy = ('long_call', 'long_put', 'bull_call_spread')[i]
            session.add(txn)
        session.commit()
        pnl = {transactions[0].id: 900.0, transactions[1].id: 200.0,
               transactions[2].id: 100.0}
        instance = fixture['tab']()
        account = NS(get_option_quote=lambda _: NS(bid=13.3, ask=13.4, last=13.35))
        with patch.object(option_trades, 'get_db', lambda: session), \
             patch.object(option_trades, 'scope_transactions_to_account', lambda q, *a: q), \
             patch.object(option_trades, 'get_selected_account_id', lambda: None), \
             patch.object(option_trades, 'get_account_instance_from_id', lambda *a, **k: account), \
             patch.object(option_trades, 'option_transaction_pnl',
                          lambda a, o, **k: NS(available=True, amount=pnl[o.transaction_id],
                                               percent=pnl[o.transaction_id] / 10, reason=None)):
            rows, count = instance._collect_rows(1, 2, {}, sort_by, True)
        results[sort_by] = {'ids': [r['id'] for r in rows], 'total': count,
                            'pnl_amounts': [pnl[r['id']] for r in rows]}
        engine.dispose()

    # Quasar sends the column NAME (current_pnl), not its numeric FIELD.
    assert results['current_pnl']['ids'] == [2, 3]
    assert results['current_pnl_numeric']['ids'] == [1, 2]
    assert results['strategy']['ids'] == [3, 2]  # creation order, not strategy order
    results['sortable_name_field_pairs'] = [
        [c.name, c.field] for c in LiveTradesTable.OPTION_TRANSACTION_COLUMNS if c.sortable
    ]
    return results


if __name__ == '__main__':
    evidence = {'reviewed_head': '7f2f58a5', 'ranking': ranking_probe(), 'option_sort': ui_probe()}
    destination = ROOT / 'reports/review_evidence/synced-7f2f58a5-2026-09-21'
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'offline-probes.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps(evidence, indent=2))
