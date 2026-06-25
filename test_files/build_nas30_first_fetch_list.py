"""Reorder the missing-5min list so NAS30 (still uncached) comes first, then the rest."""
import os

NEW = r"C:\Users\basti\Documents\ba2\common\cache\ohlcv\fmp"
NAS30 = ("AAPL,MSFT,NVDA,AMZN,META,GOOGL,AVGO,TSLA,COST,NFLX,AMD,PEP,ADBE,CSCO,TMUS,"
         "INTC,QCOM,INTU,AMAT,TXN,AMGN,ISRG,BKNG,HON,VRTX,ADP,SBUX,GILD,MU,LRCX").split(",")

have = {f[:-13] for f in os.listdir(NEW) if f.endswith("_5min.parquet")}
missing_all = [s for s in open(r"C:\Users\basti\Documents\dev\missing_5min_newpath.txt").read().split(",") if s]

nas30_missing = [s for s in NAS30 if s not in have]
rest = [s for s in missing_all if s not in set(nas30_missing)]
ordered = nas30_missing + rest

open(r"C:\Users\basti\Documents\dev\fetch_nas30_first.txt", "w").write(",".join(ordered))
print(f"nas30 missing (front): {len(nas30_missing)} -> {nas30_missing}")
print(f"rest: {len(rest)} | total ordered: {len(ordered)}")
