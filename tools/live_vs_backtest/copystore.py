import sqlite3, shutil, os
SRC='C:/Users/basti/Documents/ba2_trade_platform-prod/cache/replay/v1'
DST='C:/Users/basti/AppData/Local/Temp/claude/C--Users-basti-Documents-dev-BA2TradePlatform/820f80e0-b6ea-41a0-8f76-d0176d9f7156/scratchpad/livecmp/store'
os.makedirs(DST, exist_ok=True)
s=sqlite3.connect(f'file:{SRC}/index.sqlite?mode=ro',uri=True)
d=sqlite3.connect(f'{DST}/index.sqlite')
s.backup(d); d.close(); s.close()
if not os.path.exists(DST+'/objects'):
    shutil.copytree(SRC+'/objects', DST+'/objects')
print('ok')
