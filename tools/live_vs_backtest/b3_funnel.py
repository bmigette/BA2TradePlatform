import sqlite3, json, collections
import pandas as pd
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
c=sqlite3.connect('file:C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite?mode=ro',uri=True)
print([r[1] for r in c.execute('pragma table_info(expertrecommendation)')])
rec=pd.read_sql("select * from expertrecommendation where created_at>='2026-09-10'",c)
print(len(rec)); print(rec.head(3).T)
o=pd.read_csv(L+'b1_live_orders.csv')
print(o[o.depends_on_order.isna()].groupby(['status','side']).size())
print(o[o.status=='REJECTED'].comment.str[:80].value_counts().head(8))
print(o[o.status=='CANCELED'].comment.str[:60].value_counts().head(6))
ma=pd.read_csv(L+'b1_live_ma.csv')
sk=ma[ma.status=='SKIPPED'].copy()
sk['skip_type']=sk.state.map(lambda s: json.loads(s).get('skip_reason') or json.loads(s).get('skip_type'))
print(sk.groupby(['expert_instance_id','skip_type']).size())
print(sk[sk.skip_type.isin(['insufficient_balance','symbol_price_balance_check'])].created_at.min())
print(sk.state[sk.state.str.contains('balance')].head(3).tolist())
