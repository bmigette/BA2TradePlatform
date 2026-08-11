"""Event study: does news sentiment predict the return AFTER publication, intraday?

WHY THIS EXISTS. tools/news_sentiment_bakeoff.py aggregated sentiment to a daily mean and
regressed it on close-to-close returns. That found nothing, but it was a weak test by
construction: a 09:00 article was matched to close[t] -> close[t+1], so the entire
same-session reaction -- the window where news actually moves a stock -- landed BEFORE
the measured return and was discarded. Daily bars cannot see the event.

This measures the event directly. Each article is matched to the first 5-minute bar
STRICTLY AFTER its publication timestamp, and the return is measured forward from that
bar's open over several horizons. Articles published after the close or at the weekend
map to the next session's first bar, which is the correct treatment -- a lot of company
news lands outside market hours, and pretending it was tradable at the prior close would
be lookahead.

NO SCORING REQUIRED. It reads the `finbert-legacy` scores already in the store, so this
runs in minutes on all 26 symbols and 127k articles, with no model inference at all. That
ordering is deliberate: establish whether the SIGNAL exists at this resolution before
spending hours deciding which model measures it best. If nothing shows up here, no choice
of scorer rescues it.

READING THE OUTPUT. Two views of the same data:
  * IC  -- Spearman rank correlation of article sentiment vs forward return, per symbol
           then averaged, with a t-stat across symbols.
  * Buckets -- mean forward return in basis points for positive / neutral / negative
           articles, and the positive-minus-negative spread. This is the interpretable
           one: it says what a trade on the signal would actually have earned, before
           costs. A spread that does not exceed a few bp cannot survive the spread itself.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "common"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "providers"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ba2_common.config import CACHE_FOLDER  # noqa: E402
from ba2_providers.news import store  # noqa: E402

OHLCV_DIR = os.path.join(CACHE_FOLDER, "FMPOHLCVProvider")
# 5-minute bars. 78 bars ~= one 6.5h session.
# The multi-day horizons are the ones that decide whether this is usable AT ALL: we do not
# receive news in real time. A daily fetch means acting up to a day late, so an edge that
# has decayed by then is unreachable no matter how large it looks at 30 minutes.
HORIZONS = {"30m": 6, "1h": 12, "1d": 78, "2d": 156, "3d": 234, "5d": 390, "10d": 780}
POS_CUT, NEG_CUT = 0.15, -0.15


def load_bars(symbol: str) -> pd.DataFrame:
    path = os.path.join(OHLCV_DIR, f"{symbol}_5min.parquet")
    if not os.path.exists(path):
        return pd.DataFrame()
    d = pd.read_parquet(path)[["Date", "Open", "Close"]].dropna()
    d["Date"] = pd.to_datetime(d["Date"])
    return d.drop_duplicates("Date").sort_values("Date").reset_index(drop=True)


def study_symbol(symbol: str, model: str, entry_lag_min: float = 0.0) -> pd.DataFrame:
    """One row per article: its sentiment and forward returns from the next bar."""
    bars = load_bars(symbol)
    if bars.empty:
        return pd.DataFrame()
    news = store.read_sentiment(symbol, model=model)
    if news.empty:
        return pd.DataFrame()

    news = news.copy()
    news["sent"] = news["pos"].astype(float) - news["neg"].astype(float)
    news = news.sort_values("published_at")

    # searchsorted 'right' == the first bar STRICTLY after publication (plus any entry
    # lag). This single choice is what keeps the study honest: matching to the bar
    # containing the timestamp would let the article see part of its own reaction.
    #
    # entry_lag_min models the REAL constraint: news is not received in real time. A
    # platform that fetches once a day acts up to a day after publication, by which point
    # the fast part of the move is gone. Setting the lag is how we find out whether what
    # is left is still worth anything.
    ts = bars["Date"].values
    want = news["published_at"].values + np.timedelta64(int(entry_lag_min), "m")
    idx = np.searchsorted(ts, want, side="right")

    valid = idx < len(bars) - max(HORIZONS.values())
    news = news[valid].copy()
    idx = idx[valid]
    if news.empty:
        return pd.DataFrame()

    entry = bars["Open"].values[idx]
    out = pd.DataFrame({
        "symbol": symbol,
        "published_at": news["published_at"].values,
        "sent": news["sent"].values,
        "entry": entry,
        # Gap from publication to the tradable bar. Overnight news has a large gap and a
        # large part of its move happens in the opening auction, which is not capturable
        # at the recorded open -- reported so it can be filtered rather than assumed away.
        "gap_min": (bars["Date"].values[idx] - news["published_at"].values)
                   / np.timedelta64(1, "m"),
    })
    for name, h in HORIZONS.items():
        out[f"r_{name}"] = bars["Close"].values[idx + h] / entry - 1.0
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="finbert-legacy")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--since", default="2019-01-01",
                    help="Ignore articles before this (pre-2019 is <3/symbol/month)")
    ap.add_argument("--entry-lag-min", type=float, default=0.0,
                    help="Minutes after publication before we may enter. Model the real "
                         "fetch cadence: 1440 = a once-a-day news job.")
    ap.add_argument("--max-gap-min", type=float, default=None,
                    help="Drop articles whose next tradable bar is this far away "
                         "(e.g. 60 keeps only news that was tradable within the hour)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols] if args.symbols else store.covered_symbols()
    frames = []
    for s in symbols:
        try:
            df = study_symbol(s, args.model, entry_lag_min=args.entry_lag_min)
        except Exception as e:                       # noqa: BLE001 - reported per symbol
            print(f"  {s}: SKIP ({type(e).__name__}: {e})")
            continue
        if df.empty:
            print(f"  {s}: no overlap between news and 5min bars")
            continue
        frames.append(df)
    if not frames:
        raise SystemExit("No symbol produced any matched articles.")

    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df[all_df["published_at"] >= pd.Timestamp(args.since)]
    if args.max_gap_min is not None:
        before = len(all_df)
        all_df = all_df[all_df["gap_min"] <= args.max_gap_min]
        print(f"gap filter <= {args.max_gap_min} min: {before:,} -> {len(all_df):,} articles")

    print(f"\nmatched {len(all_df):,} articles across {all_df['symbol'].nunique()} symbols "
          f"({all_df['published_at'].min().date()} .. {all_df['published_at'].max().date()})")
    g = all_df["gap_min"]
    print(f"publication -> tradable bar gap: median {g.median():.0f} min, "
          f"p25 {g.quantile(.25):.0f}, p75 {g.quantile(.75):.0f}  "
          f"({100*(g <= 60).mean():.0f}% tradable within the hour)")

    # --- IC -----------------------------------------------------------------------
    rows = []
    for name in HORIZONS:
        per = []
        for sym, grp in all_df.groupby("symbol"):
            v = grp[["sent", f"r_{name}"]].dropna()
            if len(v) < 100 or v["sent"].nunique() < 5:
                continue
            per.append(v["sent"].corr(v[f"r_{name}"], method="spearman"))
        per = pd.Series(per).dropna()
        t = per.mean() / (per.std(ddof=1) / np.sqrt(len(per))) if len(per) > 1 else np.nan
        rows.append({"horizon": name, "symbols": len(per), "ic": per.mean(), "t": t})
    ic = pd.DataFrame(rows)

    # --- buckets ------------------------------------------------------------------
    b = all_df.assign(
        bucket=np.where(all_df["sent"] > POS_CUT, "pos",
                        np.where(all_df["sent"] < NEG_CUT, "neg", "neu")))
    brows = []
    for name in HORIZONS:
        col = f"r_{name}"
        m = b.groupby("bucket")[col].agg(["mean", "size"])
        pos = m.loc["pos", "mean"] * 1e4 if "pos" in m.index else np.nan
        neg = m.loc["neg", "mean"] * 1e4 if "neg" in m.index else np.nan
        neu = m.loc["neu", "mean"] * 1e4 if "neu" in m.index else np.nan
        # t on the pos-minus-neg difference of means (Welch), the quantity a trade earns.
        pv = b.loc[b.bucket == "pos", col].dropna()
        nv = b.loc[b.bucket == "neg", col].dropna()
        tt = ((pv.mean() - nv.mean()) /
              np.sqrt(pv.var(ddof=1) / len(pv) + nv.var(ddof=1) / len(nv))) if len(nv) > 1 else np.nan
        brows.append({"horizon": name, "pos_bp": pos, "neu_bp": neu, "neg_bp": neg,
                      "pos_minus_neg_bp": pos - neg, "t": tt})
    bk = pd.DataFrame(brows)

    print("\n" + "=" * 72)
    print("IC (Spearman, per-symbol mean; t across symbols)")
    print("=" * 72)
    print(ic.to_string(index=False, float_format=lambda x: f"{x:+.4f}"))
    print("\n" + "=" * 72)
    print("MEAN FORWARD RETURN BY SENTIMENT BUCKET (basis points)")
    print("=" * 72)
    print(bk.to_string(index=False, float_format=lambda x: f"{x:+.2f}"))
    n = b["bucket"].value_counts()
    print(f"\nbucket sizes: {dict(n)}")
    print("A pos-minus-neg spread below ~5 bp cannot survive transaction costs, however "
          "significant\nits t-statistic -- significance and tradability are different "
          "questions.")

    if args.out:
        all_df.to_parquet(args.out, index=False)
        print(f"\nper-article detail -> {args.out}")


if __name__ == "__main__":
    main()
