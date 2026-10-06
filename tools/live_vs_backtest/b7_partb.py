import sqlite3, json, collections
import pandas as pd, numpy as np
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
c=sqlite3.connect('file:C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite?mode=ro',uri=True)
ei=pd.read_sql("select id,expert,enter_market_ruleset_id as er from expertinstance where id in (7,8,9,10,11,12,13)",c)
print(ei.to_string())
rules={}
for _,r in ei.iterrows():
    rr=[]
    for (trig,) in c.execute('select ev.triggers from ruleset_eventaction_link e join eventaction ev on ev.id=e.eventaction_id where e.ruleset_id=? order by e.order_index',(int(r.er),)):
        rr.append(json.loads(trig))
    rules[int(r.id)]=rr
def passes(inst,conf,ep):
    # evaluate only the numeric conditions the rec itself carries; others assumed pass
    for trig in rules[inst]:
        ok=True
        for k,v in trig.items():
            et=v['event_type']
            if et=='confidence':
                op=v['operator']; t=v['value']
                ok&= (conf>t) if op=='>' else (conf>=t) if op=='>=' else True
            if et=='expected_profit_target_percent':
                ok&= ep>v['value']
        if ok: return True
    return False
b=pd.read_csv(L+'b4_buy_recs.csv')
b['rule_ok']=[passes(int(i),cf,ep) for i,cf,ep in zip(b.instance_id,b.confidence,b.expected_profit_percent)]
b['order']=b.n.notna()
print('BUY recs: rule_ok x order'); print(pd.crosstab(b.instance_id,[b.rule_ok,b.order]))
# no-order & rule_ok: price distribution
x=b[(~b.order)&(b.rule_ok)]
print(x.groupby('instance_id').price_at_date.describe())
f=b[b.order]; print('traded price dist'); print(f.groupby('instance_id').price_at_date.describe())
# skipped for balance + BT signal
sk=json.load(open(L+'b6_skipped_bt.json'))
rows=[]
for r in sk:
    if r.get('no_settings'): rows.append(dict(inst=r['inst'],data='no_settings')); continue
    for m in ('off','on'): pass
    o=r['off']; n=r['on']
    rows.append(dict(inst=r['inst'],sym=r['symbol'],D=r['as_of'][:10],skip_type=r['skip_type'],ok=o['ok'],price=o.get('price'),
        off=('SKIP' if o.get('skip') else o.get('sig')) if o['ok'] else 'ERR', on=('SKIP' if n.get('skip') else n.get('sig')) if n['ok'] else 'ERR',
        oconf=o.get('conf'),oep=o.get('exp_profit'),nconf=n.get('conf'),nep=n.get('exp_profit')))
sd=pd.DataFrame(rows); sd.to_csv(L+'b7_skipped_bt.csv',index=False)
sd['has_price']=sd.price.notna()
print('balance-skipped analyses: BT signal (off) by instance'); print(pd.crosstab(sd.inst,sd.off))
print('... (on)'); print(pd.crosstab(sd.inst,sd.on))
# BT BUY that would pass the live entry rule
sd['rule_ok_off']=[ (passes(int(i),cf,ep) if (s=='BUY' and cf is not None and ep is not None) else False) for i,s,cf,ep in zip(sd.inst,sd.off,sd.oconf,sd.oep)]
print('BT would-enter (BUY & rule) among balance-skipped, by inst:'); print(sd.groupby('inst').rule_ok_off.sum())
print('with-date breakdown'); print(sd.groupby(['inst','D']).size().unstack(0).fillna(0).astype(int).to_string())
