"""Rank sentiment models by information coefficient against forward returns.

THE QUESTION THIS ANSWERS is not "which model labels a sentence best" -- we have no
labelled benchmark for this corpus and would not trust one if we did. It is "whose daily
aggregate best predicts the next 1 and 5 days of return on THESE 26 names". That is the
only property the DeterministicScorer news section would actually use.

IT ALSO DOUBLES AS THE GO/NO-GO. If no model shows a usable edge, the honest outcome is
to keep the migrated cache and NOT wire news into the expert -- saving a whole grid. A
near-zero IC here is a result, not a failure to find one.

WHY A SAMPLE. Scoring measured ~4 rows/s on this machine while a GA grid holds ~34 GB
and most of 20 cores, so a full 127k-row pass per model is ~9 hours -- 35 for four
models. But ranking models needs only enough rows to separate their ICs, not every row.
This scores a stratified sample (by symbol and year, so no single dense name or year
dominates) entirely IN MEMORY and never writes to the store: a bake-off is an experiment,
and it should not mutate the data it is ranking on. Only the winner gets a full-corpus
pass, via tools/score_news_sentiment.py.

READING THE OUTPUT. IC is a Spearman rank correlation, computed per symbol and then
averaged, with a t-statistic over the per-symbol ICs. Daily equity-news ICs are small by
nature: 0.02-0.05 is a real signal, above 0.10 on this sample size is more likely a bug
or lookahead than a discovery. The `legacy` row is the migrated FinBERT column and is the
bar to beat; note its scores are one-hot (label + confidence), so a re-scored `finbert`
beating it is an artefact of better probabilities, not a different model.

Usage:
    python tools/news_sentiment_bakeoff.py --sample 400 --models finbert distilroberta-news
    python tools/news_sentiment_bakeoff.py --sample 800 --all-models --threads 4
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "common"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "providers"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ba2_common.config import CACHE_FOLDER  # noqa: E402
from ba2_providers.news import sentiment, store  # noqa: E402

OHLCV_DIR = os.path.join(CACHE_FOLDER, "FMPOHLCVProvider")
MIN_YEAR = 2019          # 2011-2018 average <3 articles/symbol/month -- unusable
LEGACY_MODEL = "finbert-legacy"


def load_forward_returns(symbol: str, horizons=(1, 5)) -> pd.DataFrame:
    """Close-to-close forward returns, indexed by date."""
    path = os.path.join(OHLCV_DIR, f"{symbol}_1d.parquet")
    if not os.path.exists(path):
        return pd.DataFrame()
    px = pd.read_parquet(path)[["Date", "Close"]].dropna()
    px["Date"] = pd.to_datetime(px["Date"]).dt.normalize()
    px = px.drop_duplicates("Date").sort_values("Date").set_index("Date")
    out = pd.DataFrame(index=px.index)
    for h in horizons:
        out[f"fwd_{h}d"] = px["Close"].shift(-h) / px["Close"] - 1.0
    return out


def sample_articles(symbols, per_cell: int, seed: int = 7) -> pd.DataFrame:
    """Stratified sample: up to `per_cell` articles per (symbol, year).

    Stratifying matters here. GOOG and NVDA hold 30% of the corpus between them and 2025
    holds 23%, so a uniform random draw would rank the models on a handful of names in a
    single regime.
    """
    rng = np.random.default_rng(seed)
    parts = []
    for sym in symbols:
        raw = store.read_raw(sym)
        raw = raw[raw["published_at"].dt.year >= MIN_YEAR]
        if raw.empty:
            continue
        raw = raw.assign(symbol=sym, year=raw["published_at"].dt.year)
        for _, grp in raw.groupby("year"):
            take = min(per_cell, len(grp))
            parts.append(grp.iloc[rng.choice(len(grp), take, replace=False)])
    if not parts:
        raise SystemExit("No articles sampled -- is the news store populated?")
    return pd.concat(parts, ignore_index=True)


def build_text(row) -> str:
    title = (row.title or "").strip()
    body = (row.text or "").strip() or (row.summary or "").strip()
    return f"{title}. {body}" if (title and body) else (title or body)


def daily_aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """Mean signed sentiment per (symbol, date).

    Articles are stamped to the date they were PUBLISHED and matched to the return
    starting from that session's close. Anything published after the close would properly
    belong to the next session; this is a known approximation of the bake-off, and it is
    conservative in the sense that it can only blur the signal, not manufacture one.
    """
    d = df.copy()
    d["date"] = d["published_at"].dt.normalize()
    return d.groupby(["symbol", "date"], as_index=False).agg(
        sent=("sent", "mean"), n=("sent", "size"))


def evaluate(agg: pd.DataFrame, returns: dict, horizons=(1, 5), min_days: int = 60):
    """Per-symbol Spearman IC, then mean and t-stat across symbols."""
    rows = []
    for sym, grp in agg.groupby("symbol"):
        r = returns.get(sym)
        if r is None or r.empty:
            continue
        m = grp.set_index("date").join(r, how="inner").dropna()
        if len(m) < min_days or m["sent"].nunique() < 5:
            continue
        rec = {"symbol": sym, "days": len(m)}
        for h in horizons:
            rec[f"ic_{h}d"] = m["sent"].corr(m[f"fwd_{h}d"], method="spearman")
        rows.append(rec)
    per_sym = pd.DataFrame(rows)
    summary = {"symbols": len(per_sym), "days": int(per_sym["days"].sum()) if len(per_sym) else 0}
    for h in horizons:
        col = per_sym.get(f"ic_{h}d")
        if col is None or col.dropna().empty:
            summary[f"ic_{h}d"] = np.nan
            summary[f"t_{h}d"] = np.nan
            continue
        v = col.dropna()
        summary[f"ic_{h}d"] = float(v.mean())
        # t over the per-symbol ICs: does the edge hold ACROSS names, or is it one name?
        summary[f"t_{h}d"] = float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))) if len(v) > 1 else np.nan
    return per_sym, summary


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=400,
                    help="Max articles per (symbol, year) cell")
    ap.add_argument("--models", nargs="*", default=None)
    ap.add_argument("--all-models", action="store_true")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--max-length", type=int, default=192)
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--out", default=None, help="Write the per-symbol detail to this CSV")
    args = ap.parse_args()

    models = (list(sentiment.MODELS) if args.all_models
              else (args.models or ["finbert", "distilroberta-news"]))
    symbols = [s.upper() for s in args.symbols] if args.symbols else store.covered_symbols()

    print(f"sampling <= {args.sample} articles per (symbol, year) from {len(symbols)} symbols")
    sample = sample_articles(symbols, args.sample)
    print(f"sample: {len(sample):,} articles, {sample['year'].min()}-{sample['year'].max()}")

    returns = {s: load_forward_returns(s) for s in symbols}
    missing = [s for s, r in returns.items() if r.empty]
    if missing:
        # Loud, not silent: a symbol without price data drops out of the ranking, and a
        # ranking computed on 12 of 26 names is a different claim from one on all 26.
        print(f"WARNING: no OHLCV for {missing} -- excluded from IC")

    texts = [build_text(r) for r in sample.itertuples()]
    results = []
    detail = []

    # The migrated legacy column, evaluated on the SAME sampled articles. Joined on
    # url_hash so the comparison is article-for-article, not window-for-window.
    legacy = []
    for sym in symbols:
        if not store.has_coverage(sym):
            continue
        s = store.read_sentiment(sym, model=LEGACY_MODEL)
        if len(s):
            legacy.append(s.assign(symbol=sym))
    if legacy:
        lg = pd.concat(legacy, ignore_index=True)
        lg["sent"] = lg["pos"].astype(float) - lg["neg"].astype(float)
        lg = lg.merge(sample[["url_hash", "symbol"]], on=["url_hash", "symbol"], how="inner")
        per, summ = evaluate(daily_aggregate(lg), returns)
        summ["model"] = "finbert-legacy"
        summ["rows"] = len(lg)
        results.append(summ)
        detail.append(per.assign(model="finbert-legacy"))

    for model in models:
        print(f"\nscoring {len(texts):,} sampled articles with {model} ...")
        t0 = time.time()
        sc = sentiment.score_texts(texts, model=model, batch_size=args.batch_size,
                                   max_length=args.max_length, threads=args.threads)
        dt = time.time() - t0
        scored = sample.assign(sent=sc["score"].values)
        per, summ = evaluate(daily_aggregate(scored), returns)
        summ["model"] = model
        summ["rows"] = len(scored)
        results.append(summ)
        detail.append(per.assign(model=model))
        print(f"  {len(texts)/max(1e-9, dt):.1f} rows/s "
              f"-> full 127k corpus would take {127134/max(1e-9, len(texts)/dt)/3600:.1f} h")

    res = pd.DataFrame(results)[
        ["model", "rows", "symbols", "days", "ic_1d", "t_1d", "ic_5d", "t_5d"]]
    res = res.sort_values("ic_5d", ascending=False)
    print("\n" + "=" * 78)
    print("INFORMATION COEFFICIENT (Spearman, per-symbol mean; t over symbols)")
    print("=" * 78)
    print(res.to_string(index=False, float_format=lambda x: f"{x:+.4f}"))
    print("\nA daily equity-news IC of 0.02-0.05 is a real edge. Above ~0.10 on this "
          "sample\nis more likely a bug or lookahead than a discovery -- check before "
          "believing it.")

    if args.out:
        pd.concat(detail, ignore_index=True).to_csv(args.out, index=False)
        print(f"\nper-symbol detail -> {args.out}")


if __name__ == "__main__":
    main()
