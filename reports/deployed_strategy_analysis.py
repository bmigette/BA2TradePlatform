"""Read-only analysis of deployed strategy logic and saved capped backtests."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import re
import sqlite3

import pandas as pd

OUT = Path(__file__).parent


def database(path):
    c = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    c.execute('BEGIN')
    return c


live = database('C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite')
test = database('C:/Users/basti/Documents/ba2/test/dl_forecasting.db')
instances = [dict(r) for r in live.execute('select * from expertinstance where enabled=1 order by id')]
live.close()
stats, curves, ledgers = {}, {}, {}
for inst in instances:
    source = int(re.search(r'backtest (\d+)', inst['user_description'])[1])
    parent = test.execute('select name,strategy_params from backtests where id=?', (source,)).fetchone()
    raw = test.execute('select id,name,status,start_date,end_date,strategy_params,trades,results,'
                       'equity_curve,max_drawdown,win_rate,profit_factor from backtests where name=? '
                       'order by id desc limit 1', ('OK1000-' + parent['name'],)).fetchone()
    assert raw is not None and raw['status'] == 'completed', source
    p = json.loads(raw['strategy_params'])
    assert p['equityCap'] == 1000
    df = pd.DataFrame(json.loads(raw['equity_curve']))
    scoring = pd.Series(df.equity.to_numpy(float), index=pd.to_datetime(df.date, utc=True)).sort_index()
    assert scoring.index.is_unique and (scoring > 0).all()
    changes = scoring.div(scoring.shift()).sub(1).fillna(0) * 1000
    pnl_curve = changes.cumsum()
    dd = float((pnl_curve - pnl_curve.cummax()).min() / 10)
    assert abs(dd - raw['max_drawdown']) < .011
    t = pd.DataFrame(json.loads(raw['trades']))
    t['entry'] = pd.to_datetime(t.entry_time, utc=True)
    t['exit'] = pd.to_datetime(t.exit_time, utc=True)
    t['days'] = (t['exit'] - t.entry).dt.total_seconds() / 86400
    t['entry_cost'] = t.entry_price * t['size']
    closed = t[t.exit_reason != 'open_at_end']
    marked = t[t.exit_reason == 'open_at_end']
    assert abs(float(t.pnl.sum()) - float(pnl_curve.iloc[-1])) < .01
    wins, losses = closed[closed.pnl > 0], closed[closed.pnl < 0]
    result = json.loads(raw['results'])
    assert len(marked) == len(result['open_positions'])
    gp = float(wins.pnl.sum())
    net = float(closed.pnl.sum())
    capital_days = float((t.entry_cost * t.days).sum())
    window_days = (pnl_curve.index[-1] - pnl_curve.index[0]).total_seconds() / 86400
    case = {
        'instance': inst, 'parent_id': source, 'id': raw['id'], 'name': raw['name'],
        'start': raw['start_date'], 'end': raw['end_date'],
        'total_pnl_including_end_marks': float(t.pnl.sum()),
        'closed_pnl': net, 'open_at_end_marked_pnl': float(marked.pnl.sum()),
        'closed_count': len(closed), 'open_count': len(marked),
        'drawdown_pct_cap': dd,
        'reported_profit_factor': raw['profit_factor'],
        'closed_profit_factor': gp / abs(float(losses.pnl.sum())),
        'closed_win_rate': len(wins) / len(closed) * 100,
        'reported_win_rate': raw['win_rate'],
        'mean_winner': float(wins.pnl.mean()), 'median_winner': float(wins.pnl.median()),
        'mean_loser': float(losses.pnl.mean()),
        'mean_win_loss_ratio': float(wins.pnl.mean() / abs(losses.pnl.mean())),
        'top5_closed_winners_pct_gross': float(wins.pnl.nlargest(5).sum()) / gp * 100,
        'top5_closed_winners_pct_net': float(wins.pnl.nlargest(5).sum()) / net * 100,
        'closed_net_without_top5': net - float(wins.pnl.nlargest(5).sum()),
        'holding_mean_closed_days': float(closed.days.mean()),
        'holding_median_closed_days': float(closed.days.median()),
        'holding_p90_closed_days': float(closed.days.quantile(.9)),
        'holding_max_closed_days': float(closed.days.max()),
        'open_holding_days': marked[['symbol', 'days', 'pnl']].round(3).to_dict('records'),
        'entry_cost_exposure_proxy': capital_days / window_days,
        'exit_counts': dict(Counter(t.exit_reason)),
        'closed_yearly_pnl': closed.groupby(closed['exit'].dt.year).pnl.sum().round(2).to_dict(),
        'mark_to_market_yearly_pnl': changes.groupby(changes.index.year).sum().round(2).to_dict(),
        'largest_closed_winners': wins.nlargest(5, 'pnl')[['symbol', 'pnl', 'days']].round(3).to_dict('records'),
        'largest_issuer_closed_pnl': closed.groupby('symbol').pnl.sum().nlargest(5).round(2).to_dict(),
        'settings': {k: v for k, v in p.items() if k.startswith(('model:', 'screener:', 'schedule:'))
                     or k == 'expertFixedSettings'},
        'entry_rules': p['entryRules'], 'exit_rules': p['exitRules'],
    }
    retained = database(f'C:/Users/basti/AppData/Local/Temp/ba2_backtest_dbs/run_{raw["id"]}.sqlite')
    rec_rows = [dict(r) for r in retained.execute('''
        select o.id,o.transaction_id,r.confidence,r.risk_level,r.time_horizon
        from tradingorder o join expertrecommendation r on r.id=o.expert_recommendation_id
        where o.filled_qty>0 and o.side='BUY' order by o.id''')]
    retained.close()
    entry_recs = pd.DataFrame(rec_rows).drop_duplicates('transaction_id', keep='first')
    joined = t.merge(entry_recs, on='transaction_id', validate='one_to_one')
    assert len(joined) == len(t)
    case['entry_recommendation_profile'] = {
        'confidence_min': float(joined.confidence.min()),
        'confidence_max': float(joined.confidence.max()),
        'risk_counts': dict(Counter(joined.risk_level)),
        'horizon_counts': dict(Counter(joined.time_horizon)),
    }
    if inst['expert'] == 'FMPRating':
        joined['branch'] = joined.confidence.ge(80).map({True: 'confidence_ge_80', False: 'confidence_45_to_80'})
        case['rating_branch_closed_results'] = {}
        for branch, group in joined[joined.exit_reason != 'open_at_end'].groupby('branch'):
            case['rating_branch_closed_results'][branch] = {
                'closed_trades': len(group), 'pnl': float(group.pnl.sum()),
                'win_rate': float((group.pnl > 0).mean()*100),
            }
    stats[inst['id']] = case
    curves[inst['id']] = pnl_curve
    ledgers[inst['id']] = t
test.close()

# Common dates only. Aggregate separate runs with the same fixed denominator,
# including idle-cash controls; this is attribution, NOT a shared-account replay.
start = max(s.index[0] for s in curves.values())
end = min(s.index[-1] for s in curves.values())
aligned = pd.concat(curves, axis=1, sort=True).sort_index().ffill().loc[start:end]
assert aligned.notna().all().all()
aligned = aligned - aligned.iloc[0]
daily = aligned.groupby(aligned.index.normalize()).last().diff().fillna(0)
combos = {}
plans = {'original_four_plus_cash': [7, 8, 9, 10],
         'four_plus_mid_ed_plus_cash': [7, 8, 9, 10, 11],
         'four_plus_rating_plus_cash': [7, 8, 9, 10, 12],
         'all_six': [7, 8, 9, 10, 11, 12]}
for name, ids in plans.items():
    s = aligned[ids].sum(axis=1)
    d = s - s.cummax()
    combos[name] = {'ids': ids, 'total_pnl': float(s.iloc[-1]),
                    'dd_dollars': float(d.min()), 'dd_pct_fixed_6000': float(d.min() / 60),
                    'worst_day_pnl': float(daily[ids].sum(axis=1).min()),
                    'yearly_pnl': daily[ids].sum(axis=1).groupby(daily.index.year).sum().round(2).to_dict()}
overlap = {}
for a, b in itertools.combinations(ledgers, 2):
    left = ledgers[a]; right = ledgers[b]
    left = left[(left.entry <= end) & (left['exit'] >= start)]
    right = right[(right.entry <= end) & (right['exit'] >= start)]
    symbols, pair_days = set(), 0.0
    for sym in set(left.symbol) & set(right.symbol):
        for r in left[left.symbol == sym].itertuples():
            for q in right[right.symbol == sym].itertuples():
                lo = max(r.entry, q.entry, start); hi = min(r.exit, q.exit, end)
                if hi > lo:
                    symbols.add(sym)
                    pair_days += (hi-lo).total_seconds()/86400
    overlap[f'{a}-{b}'] = {'symbols_held_simultaneously': sorted(symbols),
                           'summed_pair_days': pair_days}

common_stats = {}
for iid in stats:
    s = aligned[iid]
    rest = daily.drop(columns=iid).sum(axis=1)
    common_stats[iid] = {'pnl': float(s.iloc[-1]),
                         'drawdown_pct_cap': float((s-s.cummax()).min()/10),
                         'correlation_other_five': float(daily[iid].corr(rest))}

result = {'created_utc': datetime.now(timezone.utc).isoformat(), 'standalone': stats,
          'common_start': str(start), 'common_end': str(end), 'common_stats': common_stats,
          'combinations_independent_6000_cap': combos,
          'daily_pnl_correlations': daily.corr().round(4).to_dict(), 'overlap': overlap}
(OUT / 'deployed_strategy_analysis_2026-09-07.json').write_text(
    json.dumps(result, indent=2, default=str), encoding='utf-8')
for iid, r in stats.items():
    print(json.dumps({k: v for k, v in r.items() if k not in
                      ('instance', 'settings', 'entry_rules', 'exit_rules', 'open_holding_days')}))
print('COMMON', json.dumps(common_stats))
print('COMBOS', json.dumps(combos))
print('CORRELATIONS', daily.corr().round(3).to_json())
print('OVERLAP', json.dumps({k: len(v['symbols_held_simultaneously']) for k, v in overlap.items()}))
