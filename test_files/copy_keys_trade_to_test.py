"""Copy the appsetting (API keys) table from the trade DB into the test platform's
single DB (test/dl_forecasting.db), per the 'single DB' refactor. Pure sqlite — no
heavy app import. Idempotent: replaces rows by key."""
import sqlite3

TRADE = r"C:\Users\basti\Documents\ba2\trade\db.sqlite"
DL = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"

src = sqlite3.connect(TRADE)
create_sqls = [
    r[0]
    for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE name='appsetting' AND sql IS NOT NULL"
    )
]
src.close()
print("DDL statements for appsetting:", len(create_sqls))

dl = sqlite3.connect(DL)
cur = dl.cursor()
have = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if "appsetting" not in have:
    for sql in create_sqls:
        cur.execute(sql)
    print("created appsetting (+indexes) in dl_forecasting.db")
else:
    print("appsetting already present in dl_forecasting.db")

cur.execute(f"ATTACH '{TRADE}' AS src")
before = cur.execute("SELECT count(*) FROM appsetting").fetchone()[0]
cur.execute("INSERT OR REPLACE INTO appsetting SELECT * FROM src.appsetting")
dl.commit()
after = cur.execute("SELECT count(*) FROM appsetting").fetchone()[0]
print(f"appsetting rows: before={before} after={after}")
# sanity: show the FMP key is present (value masked)
for k in ("FMP_API_KEY", "fmp_api_key", "openai_api_key"):
    row = cur.execute("SELECT value_str FROM appsetting WHERE key=?", (k,)).fetchone()
    print(f"  {k}: {'present' if row and row[0] else 'MISSING'}")
dl.close()
