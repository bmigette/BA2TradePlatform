import sqlite3, json, collections
import pandas as pd
L='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/'
c=sqlite3.connect('file:C:/Users/basti/Documents/ba2_trade_platform-prod/db.sqlite?mode=ro',uri=True)
rec=pd.read_sql("select id,instance_id,market_analysis_id,symbol,recommended_action,expected_profit_percent,price_at_date,confidence,created_at,subtype from expertrecommendation where created_at>='2026-09-10'",c)
ma=pd.read_sql("select id,symbol,expert_instance_id,status,subtype,created_at from marketanalysis where created_at>='2026-09-10'",c)
tx=pd.read_csv(L+'b1_live_tx.csv'); o=pd.read_csv(L+'b1_live_orders.csv')
rec['D']=rec.created_at.str[:10]
ma=ma.set_index('id'); rec['uc']=rec.market_analysis_id.map(ma.subtype)
o['D']=o.created_at.str[:10]
# entry orders linked to rec
eo=o[o.expert_recommendation_id.notna()].copy(); eo['expert_recommendation_id']=eo.expert_recommendation_id.astype(int)
byrec=eo.groupby('expert_recommendation_id').agg(n=('id','size'),st=('status',lambda s: '/'.join(sorted(set(s)))),qty=('quantity','sum'),cm=('comment',lambda s:'|'.join(x[:60] for x in s)))
rec=rec.join(byrec,on='id')
tx['D']=tx.open_date.str[:10]
txk=tx.groupby(['expert_id','symbol','D']).id.first()
rec['tx']=[txk.get((i,s,d)) for i,s,d in zip(rec.instance_id,rec.symbol,rec.D)]
e=rec[(rec.uc=='ENTER_MARKET')&(rec.instance_id.isin([7,8,9,10,11,12,13]))]
print('ENTER_MARKET recs by instance/action'); print(pd.crosstab(e.instance_id,e.recommended_action))
b=e[e.recommended_action=='BUY']
print('BUY recs: has order / has tx'); print(pd.crosstab(b.instance_id,[b.n.notna(),b.tx.notna()]))
print('BUY recs with order status'); print(pd.crosstab(b.instance_id,b.st.fillna('no_order')))
print(b[b.n.isna()].groupby('instance_id').size())
b.to_csv(L+'b4_buy_recs.csv',index=False)
# open_positions BUY recs -> add-to-position orders
op=rec[(rec.uc=='OPEN_POSITIONS')&(rec.instance_id.isin([7,8,9,10,11,12,13]))]
print(pd.crosstab(op.instance_id,op.recommended_action))
# per instance trades
tx['pnl']=(tx.close_price-tx.open_price)*tx.quantity
tx['notional']=tx.open_price*tx.quantity
print(tx.groupby('expert_id').agg(n=('id','size'),notional=('notional','sum'),pnl=('pnl','sum'),closed=('status',lambda s:(s=='CLOSED').sum())))
print(pd.crosstab(tx.expert_id,tx.close_reason.fillna('open')))
print(tx[tx.expert_id.isin([7,8,9,10,11,12,13])][['id','expert_id','symbol','quantity','open_price','close_price','open_date','close_date','close_reason','notional']].to_string())
