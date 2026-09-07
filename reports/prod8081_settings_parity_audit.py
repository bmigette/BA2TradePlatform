"""Read-only deployment audit; no app startup, broker calls or DB mutations.

Run with Python 3.11+ and pydantic 2. Executes the repository's pure export/rule
converters and actual settings-reader property against read-only SQLite rows.
Application startup imports are deliberately excluded via AST extraction.
"""
from __future__ import annotations

import ast
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).parent
LIVE = Path('C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite')
TEST = Path('C:/Users/basti/Documents/ba2/test/dl_forecasting.db')
RUNS = Path('C:/Users/basti/AppData/Local/Temp/ba2_backtest_dbs')
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'packages/common'))
# Pure rule modules only need logging; avoid the application's file logger setup.
log_module = ModuleType('ba2_common.logger')
log_module.logger = logging.getLogger('readonly_audit')
sys.modules['ba2_common.logger'] = log_module
from ba2_common.core.deploy_parity import (
    BacktestRunFacts, forced_expert_settings, backtest_only_settings,
    live_settings_from_universe,
)
from ba2_common.core.rules_convert import trade_rules_to_live_export


def tree(relative):
    return ast.parse((ROOT / relative).read_text(encoding='utf-8-sig'))


def extract_function(relative, name, namespace):
    matches = [n for n in ast.walk(tree(relative))
               if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(matches) == 1, (relative, name)
    node = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), matches[0]], type_ignores=[])
    exec(compile(ast.fix_missing_locations(node), str(ROOT / relative), 'exec'), namespace)
    return namespace[name]


def literal_with_constants(node, constants):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        if isinstance(node, ast.Subscript):
            return literal_with_constants(node.value, constants)[ast.literal_eval(node.slice)]
        raise ValueError('nonliteral setting default')


def definitions(relative, constants=None):
    """Extract setting types without constructing experts or opening their databases."""
    result = {}
    for n in ast.walk(tree(relative)):
        if not isinstance(n, ast.Dict):
            continue
        for k, v in zip(n.keys, n.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and isinstance(v, ast.Dict)):
                continue
            fields = {}
            for vk, vv in zip(v.keys, v.values):
                if isinstance(vk, ast.Constant) and vk.value in ('type', 'default'):
                    try:
                        fields[vk.value] = literal_with_constants(vv, constants or {})
                    except (ValueError, TypeError):
                        pass  # Some declared defaults refer to imported constants.
            if 'type' in fields:
                result[k.value] = fields
    return result


def read_db(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    return connection


def decoded_row(row):
    value = dict(row)
    if value['value_json'] is not None:
        value['value_json'] = json.loads(value['value_json'])
    return SimpleNamespace(**value)


def runtime_settings(rows, defs):
    """Run the actual ORM-facing settings property against an in-memory row adapter."""
    class Statement:
        def filter_by(self, **kwargs):
            return self

    @contextmanager
    def get_db():
        yield SimpleNamespace(exec=lambda statement: SimpleNamespace(
            all=lambda: [decoded_row(r) for r in rows]))

    ns = {'json': json, 'logger': log_module.logger,
          'get_db': get_db, 'select': lambda model: Statement()}
    prop = extract_function('packages/common/ba2_common/core/interfaces/ExtendableSettingsInterface.py',
                            'settings', ns)
    cls = type('AuditExpert', (), {
        'SETTING_MODEL': object, 'SETTING_LOOKUP_FIELD': 'instance_id',
        'get_merged_settings_definitions': classmethod(lambda cls: defs),
        'settings': prop,
    })
    instance = cls()
    instance.id = rows[0]['instance_id']
    instance._settings_cache = None
    all_values = instance.settings
    return {r['key']: all_values[r['key']] for r in rows}


def live_rules(db, rid):
    if rid is None:
        return []
    rows = db.execute('''select ea.*, l.order_index from eventaction ea
        join ruleset_eventaction_link l on l.eventaction_id=ea.id
        where l.ruleset_id=? order by l.order_index,ea.id''', (rid,)).fetchall()
    return [{'name': r['name'], 'triggers': json.loads(r['triggers']),
             'actions': json.loads(r['actions']),
             'continue_processing': bool(r['continue_processing'])} for r in rows]


def expected_value(value, definition):
    kind = definition['type'] if definition else None
    if kind == 'bool':
        if value in (True, False, 0, 1):
            return bool(value)
        raise ValueError(('unexpected boolean contract', value))
    return value


def trade_days(trades):
    return dict(Counter(datetime.fromisoformat(t['entry_time']).strftime('%A')
                        for t in trades))


def rule_semantics(rules):
    # RulesImporter suffixes duplicate display names; evaluation uses these fields.
    return [{'triggers': r['triggers'], 'actions_in_order': list(r['actions'].items()),
             'continue_processing': r['continue_processing']} for r in rules]


EXPERT_FILES = {
    'DeterministicScorer': 'packages/experts/ba2_experts/DeterministicScorer/__init__.py',
    'FMPEarningsDrift': 'packages/experts/ba2_experts/FMPEarningsDrift.py',
    'FMPInsiderClusterBuy': 'packages/experts/ba2_experts/FMPInsiderClusterBuy.py',
    'FMPRating': 'packages/experts/ba2_experts/FMPRating.py',
}
base_defs = definitions('packages/common/ba2_common/core/interfaces/MarketExpertInterface.py')
ds_constants = {}
for path in (ROOT / 'packages/experts/ba2_experts/DeterministicScorer').glob('*.py'):
    for node in ast.parse(path.read_text(encoding='utf-8-sig')).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                ds_constants[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
export_ns = dict(BacktestRunFacts=BacktestRunFacts,
                 forced_expert_settings=forced_expert_settings,
                 backtest_only_settings=backtest_only_settings,
                 _is_bypass_expert_class=lambda name: False)
derive_export = extract_function('testplatform/backend/app/api/backtests.py',
                                '_derive_export_payload', export_ns)
sc_tree = tree('packages/providers/ba2_providers/StockScreener.py')
sc_defaults_node = next(n for n in ast.walk(sc_tree) if isinstance(n, ast.AnnAssign)
                        and isinstance(n.target, ast.Name) and n.target.id == '_DEFAULTS')
sc_defaults = ast.literal_eval(sc_defaults_node.value)
results = {'timestamp_utc': datetime.now(timezone.utc).isoformat(),
           'live_db': str(LIVE), 'test_db': str(TEST), 'instances': []}
live = read_db(LIVE)
test = read_db(TEST)
instances = [dict(r) for r in live.execute('select * from expertinstance where enabled=1 order by id')]
for inst in instances:
    source_id = int(re.search(r'backtest (\d+)', inst['user_description'])[1])
    bt = dict(test.execute('select id,name,expert_name,optimization_id,strategy_params,engine_type,'
                          'initial_capital,start_date,end_date,results,trades from backtests where id=?',
                          (source_id,)).fetchone())
    sp = json.loads(bt['strategy_params'])
    bt['strategy_params'] = sp
    bt['start_date'] = datetime.fromisoformat(bt['start_date'])
    bt['end_date'] = datetime.fromisoformat(bt['end_date'])
    opt = test.execute('select optimization_config from strategy_optimizations where id=?',
                      (bt['optimization_id'],)).fetchone()
    block = json.loads(opt[0])['backtest']
    export_ns['_opt_backtest_block'] = lambda backtest, db, b=block: (b, None)
    settings_export = derive_export(SimpleNamespace(**bt), 'expert_settings', object())
    rules_export = derive_export(SimpleNamespace(**bt), 'ruleset', object())
    expected = {**settings_export['settings']['expert_params'],
                **live_settings_from_universe(settings_export['universe'])}
    defs = {**base_defs, **definitions(EXPERT_FILES[inst['expert']], ds_constants)}
    rows = [dict(r) for r in live.execute('select * from expertsetting where instance_id=?',
                                         (inst['id'],))]
    counts = Counter(r['key'] for r in rows)
    actual = runtime_settings(rows, defs)
    differences = []
    for key, value in expected.items():
        exp = expected_value(value, defs[key] if key in defs else None)
        if key not in actual or actual[key] != exp:
            differences.append({'key': key, 'payload': value, 'expected_typed': exp,
                                'live_runtime': actual[key] if key in actual else 'MISSING',
                                'defined': key in defs})
    rule_comparison = []
    converted = trade_rules_to_live_export(rules_export['entry_rules'], rules_export['exit_rules'],
                                           name=inst['alias'])
    for rset in converted['rulesets']:
        rid = inst[rset['subtype'] + '_ruleset_id'] if rset['subtype'] == 'enter_market' else inst['open_positions_ruleset_id']
        predicted = [{k: r[k] for k in ('name', 'triggers', 'actions', 'continue_processing')}
                     for r in rset['rules']]
        deployed = live_rules(live, rid)
        rule_comparison.append({'subtype': rset['subtype'], 'ruleset_id': rid,
                                'count': len(deployed),
                                'equal': rule_semantics(predicted) == rule_semantics(deployed),
                                'display_names_equal': predicted == deployed,
                                'expected': predicted, 'live': deployed})
    sc_live = {k: type(v)(actual[k]) if k in actual and actual[k] is not None else v
               for k, v in sc_defaults.items()}
    sc_bt = {k.removeprefix('screener_'): v for k, v in
             settings_export['universe']['screener_settings'].items()}
    sc_differences = []
    for k, val in sc_live.items():
        mk = k.removeprefix('screener_')
        if mk in sc_bt:
            if val != sc_bt[mk]:
                sc_differences.append({'key': k, 'backtest': sc_bt[mk], 'live': val})
        elif mk in ('price_min', 'volume_min', 'float_min') and val:
            sc_differences.append({'key': k, 'backtest': 'no explicit runtime filter', 'live': val})
    capped = test.execute('select id,name,status,results,trades,strategy_params from backtests '
                          'where name=? order by id desc limit 1', ('OK1000-' + bt['name'],)).fetchone()
    capped_info = None
    if capped:
        cp = json.loads(capped['strategy_params'])
        capped_info = {'id': capped['id'], 'status': capped['status'],
                       'cap': cp['equityCap'],
                       'same_parent_genes': all(cp[k] == v for k, v in sp.items()),
                       'entry_days': trade_days(json.loads(capped['trades'])),
                       'analysis_cadence': json.loads(capped['results'])['analysis_cadence']}
        ct = json.loads(capped['trades'])
        capped_info['entries_below_live_20_dollar_floor'] = sum(t['entry_price'] < 20 for t in ct)
        capped_info['total_closed_trades'] = len(ct)
        run_path = RUNS / f"run_{capped['id']}.sqlite"
        if run_path.exists():
            run = read_db(run_path)
            run_rows = [dict(r) for r in run.execute('select * from expertsetting')]
            historical = runtime_settings(run_rows, defs)
            keys = [k for k in expected if k in historical]
            hist_diff = {k: {'backtest_db': historical[k], 'live': actual[k] if k in actual else 'MISSING'}
                         for k in keys if k not in actual or historical[k] != actual[k]}
            capped_info['persisted_run_db'] = str(run_path)
            historical_instance = dict(run.execute('select * from expertinstance limit 1').fetchone())
            capped_info['rules_match_live'] = {
                subtype: rule_semantics(live_rules(run, historical_instance[key])) ==
                rule_semantics(live_rules(live, inst[key]))
                for subtype, key in [('entry', 'enter_market_ruleset_id'),
                                     ('exit', 'open_positions_ruleset_id')]
            }
            capped_info['historical_expert_setting_differences'] = hist_diff
            capped_info['historical_boolean_runtime'] = {k: historical[k] for k in historical
                                                        if k in defs and defs[k]['type'] == 'bool'}
            capped_info['unpersisted_live_defaults'] = {
                k: {'backtest': value, 'current_literal_default': defs[k].get('default', 'NON_LITERAL')}
                for k, value in historical.items() if k not in actual and k in defs}
            run.close()
    results['instances'].append({
        'instance': inst, 'source_backtest': source_id, 'source_name': bt['name'],
        'source_entry_days': trade_days(json.loads(bt['trades'])),
        'source_schedule_genes': {k.removeprefix('schedule:'): bool(v) for k, v in sp.items()
                                  if k.startswith('schedule:')},
        'payload_entry_schedule': settings_export['execution']['run_schedule_override'],
        'source_management_schedule': block['manage_schedule_override'],
        'live_entry_schedule': actual.get('execution_schedule_enter_market'),
        'live_management_schedule': actual.get('execution_schedule_open_positions'),
        'expected_settings': expected, 'live_runtime_settings': actual,
        'unknown_payload_settings': [k for k in expected if k not in defs],
        'duplicate_setting_keys': {k: n for k, n in counts.items() if n > 1},
        'settings_differences': differences,
        'rules': rule_comparison,
        'backtest_screener': sc_bt, 'live_screener': sc_live,
        'screener_differences': sc_differences,
        'capped': capped_info,
    })
live.close()
test.close()
destination = OUT / 'prod8081_settings_parity_evidence_2026-09-07.json'
destination.write_text(json.dumps(results, indent=2, default=str), encoding='utf-8')
for item in results['instances']:
    print(json.dumps({
        'instance': item['instance']['id'], 'source': item['source_backtest'],
        'rules': [(r['subtype'], r['count'], r['equal']) for r in item['rules']],
        'settings_compared': len(item['expected_settings']),
        'differences': item['settings_differences'],
        'screen_diff': item['screener_differences'],
        'source_days': item['source_entry_days'],
        'capped': item['capped'],
    }))
print(destination)
