"""Can we FLAG which news has a long-lived effect, before acting on it?

THE PROBLEM THIS ATTACKS. tools/news_event_study.py established that the average news
effect decays over ~3 days and is dead by the 3-7 day lag implied by a once-or-twice-weekly
analysis run (see section 7e of the design doc). But "the average article" is the wrong
unit: a routine blog post and an earnings surprise are both one row. If the long-lived
subset can be IDENTIFIED IN ADVANCE, it is actionable at weekly cadence even though the
average is not.

The classic precedent is post-earnings-announcement drift, which runs for weeks and is
identified by the magnitude of the surprise -- something observable before you trade it.

CANDIDATE FLAGS, all computable at decision time:
  * n_articles     -- how many articles that symbol-day. A burst signals a real event
                      (earnings, guidance, M&A) rather than routine commentary.
  * agreement      -- fraction of the day's articles sharing the majority sign. Ten
                      articles that disagree carry less information than three that agree.
  * has_wire       -- a company/wire source (businesswire, prnewswire, reuters, ...) was
                      present. Distinguishes material announcements from aggregator
                      opinion (fool.com, youtube).
  * r0             -- the news-day price reaction itself. This is the key one and it is
                      NOT lookahead: we enter `lag` days later, so the initial move is
                      already history when we decide. Conditioning on it asks whether
                      confirmed reactions keep going -- i.e. whether there is drift.

The unit of analysis is the SYMBOL-DAY, not the article, because that is the unit a daily
or weekly expert actually decides on.

Entry is at the close `lag` trading days after the news day; returns are measured forward
from there. Daily bars are sufficient at these horizons and are far cheaper than 5-minute.
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
WIRE = ("businesswire", "prnewswire", "globenewswire", "reuters", "accesswire",
        "newsfile", "sec.gov")
HORIZONS = (5, 10, 20)
POS_CUT, NEG_CUT = 0.15, -0.15


def symbol_days(symbol: str, model: str, lag: int) -> pd.DataFrame:
    path = os.path.join(OHLCV_DIR, f"{symbol}_1d.parquet")
    if not os.path.exists(path):
        return pd.DataFrame()
    px = pd.read_parquet(path)[["Date", "Close"]].dropna()
    px["Date"] = pd.to_datetime(px["Date"]).dt.normalize()
    px = px.drop_duplicates("Date").sort_values("Date").reset_index(drop=True)

    sc = store.read_sentiment(symbol, model=model)
    raw = store.read_raw(symbol)[["url_hash", "source"]]
    if sc.empty:
        return pd.DataFrame()
    n = sc.merge(raw, on="url_hash", how="left")
    n["sent"] = n["pos"].astype(float) - n["neg"].astype(float)
    n["day"] = n["published_at"].dt.normalize()
    n["sign"] = np.sign(np.where(n["sent"].abs() < 0.15, 0, n["sent"]))
    n["wire"] = n["source"].fillna("").str.lower().str.contains("|".join(WIRE))

    agg = n.groupby("day").agg(
        n_articles=("sent", "size"),
        sent=("sent", "mean"),
        has_wire=("wire", "any"),
    ).reset_index()
    # Agreement among the day's DIRECTIONAL articles only -- a day of purely neutral
    # coverage should not count as unanimous.
    dirs = n[n["sign"] != 0].groupby("day")["sign"]
    agree = dirs.apply(lambda s: s.value_counts().iloc[0] / len(s) if len(s) else np.nan)
    agg = agg.merge(agree.rename("agreement"), on="day", how="left")

    # Map each news day to the trading day at or after it, then index arithmetic.
    ts = px["Date"].values
    d_idx = np.searchsorted(ts, agg["day"].values, side="left")
    ok = (d_idx >= 1) & (d_idx + lag + max(HORIZONS) < len(px))
    agg, d_idx = agg[ok].copy(), d_idx[ok]

    close = px["Close"].values
    # r0 = the news-day reaction. Already history by the time we enter at d_idx+lag.
    agg["r0"] = close[d_idx] / close[d_idx - 1] - 1.0
    entry = close[d_idx + lag]
    for h in HORIZONS:
        agg[f"fwd_{h}d"] = close[d_idx + lag + h] / entry - 1.0
    agg["symbol"] = symbol
    return agg


def spread(df: pd.DataFrame, h: int):
    """pos-minus-neg mean forward return in bp, with a Welch t."""
    p = df.loc[df["sent"] > POS_CUT, f"fwd_{h}d"].dropna()
    q = df.loc[df["sent"] < NEG_CUT, f"fwd_{h}d"].dropna()
    if len(p) < 30 or len(q) < 30:
        return np.nan, np.nan, len(p), len(q)
    t = (p.mean() - q.mean()) / np.sqrt(p.var(ddof=1) / len(p) + q.var(ddof=1) / len(q))
    return (p.mean() - q.mean()) * 1e4, t, len(p), len(q)


def report(df: pd.DataFrame, label: str, buckets):
    print(f"\n{'=' * 84}\n{label}\n{'=' * 84}")
    print(f"{'subset':<30}{'n':>8}" + "".join(f"{f'{h}d bp':>10}{'t':>7}" for h in HORIZONS))
    for name, mask in buckets:
        sub = df[mask]
        if len(sub) < 100:
            print(f"{name:<30}{len(sub):>8}   (too few)")
            continue
        line = f"{name:<30}{len(sub):>8}"
        for h in HORIZONS:
            bp, t, _, _ = spread(sub, h)
            line += f"{bp:>10.1f}{t:>7.2f}" if np.isfinite(bp) else f"{'-':>10}{'-':>7}"
        print(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="finbert-legacy")
    ap.add_argument("--lag", type=int, default=3,
                    help="Trading days between the news day and entry (weekly cadence ~3)")
    ap.add_argument("--since", default="2019-01-01")
    args = ap.parse_args()

    frames = []
    for s in store.covered_symbols():
        try:
            d = symbol_days(s, args.model, args.lag)
        except Exception as e:                       # noqa: BLE001
            print(f"  {s}: SKIP ({type(e).__name__}: {e})")
            continue
        if not d.empty:
            frames.append(d)
    all_df = pd.concat(frames, ignore_index=True)
    all_df = all_df[all_df["day"] >= pd.Timestamp(args.since)]
    print(f"\n{len(all_df):,} symbol-days across {all_df['symbol'].nunique()} symbols, "
          f"entry {args.lag} trading days after the news day")

    q_n = all_df["n_articles"].quantile([.5, .9]).values
    q_r = all_df["r0"].abs().quantile(.8)
    print(f"n_articles p50={q_n[0]:.0f} p90={q_n[1]:.0f} | |r0| p80={q_r*100:.2f}%")

    report(all_df, f"BASELINE and single flags (entry lag {args.lag}d)", [
        ("all", pd.Series(True, index=all_df.index)),
        (f"n_articles >= {q_n[1]:.0f} (top decile)", all_df["n_articles"] >= q_n[1]),
        (f"n_articles <= {q_n[0]:.0f}", all_df["n_articles"] <= q_n[0]),
        ("has_wire source", all_df["has_wire"]),
        ("no wire source", ~all_df["has_wire"]),
        ("agreement >= 0.8", all_df["agreement"] >= 0.8),
        ("agreement < 0.6", all_df["agreement"] < 0.6),
        (f"|r0| >= {q_r*100:.1f}% (big reaction)", all_df["r0"].abs() >= q_r),
        (f"|r0| < {q_r*100:.1f}% (quiet)", all_df["r0"].abs() < q_r),
    ])

    # The drift question: does the news-day move CONFIRM the sentiment, and if so does it
    # keep going? This is the shape a lagged, weekly decision could actually exploit.
    conf = np.sign(all_df["sent"]) == np.sign(all_df["r0"])
    big = all_df["r0"].abs() >= q_r
    report(all_df, f"CONFIRMED-REACTION DRIFT (entry lag {args.lag}d)", [
        ("r0 confirms sentiment", conf),
        ("r0 contradicts sentiment", ~conf),
        ("confirms AND big reaction", conf & big),
        ("confirms AND big AND wire", conf & big & all_df["has_wire"]),
        ("confirms AND big AND n>=p90", conf & big & (all_df["n_articles"] >= q_n[1])),
        ("contradicts AND big", ~conf & big),
    ])
    print("\nA subset is FLAGGABLE only if its spread is both large and stable across "
          "horizons.\nOne significant cell among many buckets is multiple testing, not a "
          "discovery.")


if __name__ == "__main__":
    main()
