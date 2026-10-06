import sqlite3, json, collections
import pandas as pd
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
c=sqlite3.connect('file:C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite?mode=ro',uri=True)
tx=pd.read_sql('select * from "transaction" where created_at>=\'2026-09-10\' and expert_id in (7,8,9,10,11,12,13)',c)
print(len(tx)); print(tx.groupby(['expert_id','status']).size())
print(tx[['id','expert_id','symbol','quantity','open_price','close_price','stop_loss','take_profit','open_date','close_date','status','close_reason','side']].head(8).to_string())
print(tx.close_reason.value_counts())
ma=pd.read_sql("select id,symbol,expert_instance_id,status,subtype,state,created_at from marketanalysis where created_at>='2026-09-10'",c)
print(ma.groupby(['expert_instance_id','status','subtype']).size())
sk=ma[ma.status=='SKIPPED']
print(len(sk)); print(sk.state.head(3).tolist())
tx.to_csv(L+'b1_live_tx.csv',index=False); ma.to_csv(L+'b1_live_ma.csv',index=False)
o=pd.read_sql("select * from tradingorder where created_at>='2026-09-10'",c)
o.to_csv(L+'b1_live_orders.csv',index=False)
print(len(o)); print(o.groupby(['status']).size())
print(c.execute('select id,virtual_equity_pct,account_id from expertinstance').fetchall())
