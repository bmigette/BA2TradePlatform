import sqlite3, json
import pandas as pd
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
c=sqlite3.connect('file:C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite?mode=ro',uri=True)
pd.set_option('display.max_colwidth',400); pd.set_option('display.width',300)
ei=pd.read_sql("select id,expert,enter_market_ruleset_id,open_positions_ruleset_id,virtual_equity_pct from expertinstance",c); print(ei)
for rid in sorted(set(ei.enter_market_ruleset_id.dropna().astype(int))):
    print('RULESET',rid, c.execute('select name,description from ruleset where id=?',(rid,)).fetchone())
    for r in c.execute('select e.order_index,e.ruleset_id,ev.id,ev.triggers,ev.actions from ruleset_eventaction_link e join eventaction ev on ev.id=e.eventaction_id where e.ruleset_id=?',(rid,)):
        print('   ',str(r)[:700])
b=pd.read_csv(L+'b4_buy_recs.csv')
x=b[(b.instance_id==7)&(b.D=='2026-09-14')].sort_values('confidence',ascending=False)
print(x[['symbol','confidence','expected_profit_percent','price_at_date','n','st','tx']].to_string())
x=b[(b.instance_id==10)&(b.D=='2026-09-15')].sort_values('confidence',ascending=False)
print(x[['symbol','confidence','expected_profit_percent','price_at_date','n','st','tx']].to_string())
