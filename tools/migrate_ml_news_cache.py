"""One-off migration of the ML test platform's news cache into the shared news store.

WHAT IS BEING MIGRATED. The ML platform stored news as 132,237 content-addressed blobs
(``cache/news/<provider>/<2-char shard>/<sha256>.json``, each the base64 of
``{"content", "cached_at"}``) plus a separate SQLite index (``news_cache``, 131,608 rows)
carrying the ticker, publish date, headline and FinBERT scores. The blobs alone are
useless -- they have no ticker and no date -- so both halves are required.

WHY THE LAYOUT CHANGES. ``cache_sync.build_manifest`` recurses the whole cache tree, and
every job pre-flight stats every file. The live cache is already ~104k files; dropping
132k more tiny blobs in would roughly double pre-flight cost on every job of a running
grid, for ~123 MB of text. The store therefore uses one parquet per symbol (26 files),
split into a raw tier outside the cache root and a scored tier inside it. See
``ba2_providers/news/store.py`` and docs/plans/2026-08-11-shared-news-cache-design.md.

THE ALPHAVANTAGE ROWS ARE EXCLUDED FROM THE SCORED TIER, DELIBERATELY. 996 rows carry
AlphaVantage's own labels ("Somewhat-Bullish", "Bullish", ...) and were never scored by
FinBERT. Those labels live on a different scale from FinBERT's probabilities, so folding
them into the same ``score`` column would silently mix two units. They keep their raw text
(and so can be re-scored properly later); they simply do not get a legacy score.

Usage:
    python tools/migrate_ml_news_cache.py --check          # report, write nothing
    python tools/migrate_ml_news_cache.py                  # migrate everything
    python tools/migrate_ml_news_cache.py --symbols NVDA MSFT
"""
import argparse
import base64
import collections
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "common"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "providers"))

import pandas as pd  # noqa: E402

from ba2_providers.news import store  # noqa: E402

GDRIVE = r"G:\Mon Drive\Work\AiTrading\Test ML Cache"
DEFAULT_INDEX = os.path.join(GDRIVE, "datasets", "dl_forecasting.db")
DEFAULT_RAR = os.path.join(GDRIVE, "cache.rar")
DEFAULT_UNRAR = r"C:\Program Files\WinRAR\UnRAR.exe"

# FinBERT's three labels. Anything else in this column is a vendor label on a different
# scale -- see the module docstring. Matching on the VALUE rather than on
# provider='alphavantage' is the honest test: it is the label that is incompatible.
FINBERT_LABELS = {"positive", "neutral", "negative"}
LEGACY_MODEL = "finbert-legacy"


def _read_index(path, symbols=None):
    if not os.path.exists(path):
        raise SystemExit(
            f"News index not found at {path}.\n"
            f"This is the RECOVERED index from Google Drive (204 MB, 131,608 rows) -- the "
            f"local ~/Documents/ba2/test/dl_forecasting.db has the table but 0 rows and is "
            f"NOT a substitute.")
    con = sqlite3.connect(path)
    q = ("SELECT url_hash, ticker, provider, source, title, summary, published_at, "
         "content_file_path, sentiment_label, sentiment_score, "
         "positive_prob, neutral_prob, negative_prob FROM news_cache")
    if symbols:
        q += " WHERE ticker IN (%s)" % ",".join("?" * len(symbols))
        df = pd.read_sql_query(q, con, params=list(symbols))
    else:
        df = pd.read_sql_query(q, con)
    con.close()
    df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce")
    return df


def _extract_blobs(rar, unrar, dest):
    """Extract cache/news/** once. Skips if the tree is already there."""
    marker = os.path.join(dest, "cache", "news")
    if os.path.isdir(marker) and len(os.listdir(marker)) >= 3:
        print(f"blobs: reusing existing extraction at {marker}")
        return marker
    if not os.path.exists(rar):
        raise SystemExit(f"Archive not found at {rar}")
    if not os.path.exists(unrar):
        raise SystemExit(f"UnRAR not found at {unrar} (pass --unrar)")
    os.makedirs(dest, exist_ok=True)
    print(f"blobs: extracting cache/news/** from {rar} -> {dest} (~123 MB, a few minutes)")
    # -o+ overwrite, no -inul: a silent extraction failure here would look exactly like
    # "the archive has no news", which is the wrong diagnosis to hand someone.
    r = subprocess.run([unrar, "x", "-o+", rar, r"cache\news\*", dest + os.sep],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"UnRAR failed (rc={r.returncode}):\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    if not os.path.isdir(marker):
        raise SystemExit(f"UnRAR reported success but {marker} does not exist")
    return marker


def _load_text(blob_root, rel_path):
    """Decode one blob. Returns (text, reason_if_missing)."""
    if not rel_path or not isinstance(rel_path, str):
        return None, "no_path"
    path = os.path.join(blob_root, rel_path.replace("/", os.sep))
    if not os.path.exists(path):
        return None, "blob_missing"
    try:
        raw = open(path, "rb").read()
        if not raw:
            return None, "blob_empty"
        obj = json.loads(base64.b64decode(raw).decode("utf-8"))
        text = obj.get("content")
        if not text:
            return None, "content_empty"
        return text, None
    except Exception as e:                       # noqa: BLE001 - counted and reported
        return None, f"decode_error:{type(e).__name__}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--rar", default=DEFAULT_RAR)
    ap.add_argument("--unrar", default=DEFAULT_UNRAR)
    ap.add_argument("--extract-to", default=os.path.join(tempfile.gettempdir(), "ba2_news_blobs"))
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--check", action="store_true",
                    help="Report what would be migrated; write nothing, extract nothing")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols] if args.symbols else None
    idx = _read_index(args.index, symbols)
    print(f"index: {len(idx):,} rows, {idx['ticker'].nunique()} tickers, "
          f"{idx['published_at'].min()} -> {idx['published_at'].max()}")

    bad_dates = int(idx["published_at"].isna().sum())
    if bad_dates:
        # published_at is the no-lookahead cut. A row without one cannot be placed in
        # time, so it is dropped LOUDLY rather than defaulted to anything.
        print(f"index: DROPPING {bad_dates:,} rows with an unparseable published_at")
        idx = idx.dropna(subset=["published_at"])

    scoreable = idx["sentiment_label"].isin(FINBERT_LABELS)
    vendor = idx["sentiment_label"].notna() & ~scoreable
    print(f"index: {int(scoreable.sum()):,} FinBERT-scored, "
          f"{int(vendor.sum()):,} vendor-labelled (EXCLUDED from scores, raw text kept), "
          f"{int(idx['sentiment_label'].isna().sum()):,} unscored")

    # The legacy probability columns are one-hot -- only the winning class is non-zero.
    # That is fine for `pos - neg`, but it means the numbers are label+confidence, not a
    # distribution, and any comparison against a real re-score must know that.
    onehot = ((idx[["positive_prob", "neutral_prob", "negative_prob"]].fillna(0) > 0)
              .sum(axis=1))
    print(f"index: probability rows with >1 non-zero class: {int((onehot > 1).sum()):,} "
          f"(legacy scores are one-hot: label + confidence, not a distribution)")

    if args.check:
        by_sym = idx.groupby("ticker").agg(
            rows=("url_hash", "size"),
            scored=("sentiment_label", lambda s: int(s.isin(FINBERT_LABELS).sum())),
            first=("published_at", "min"), last=("published_at", "max"))
        print()
        print(by_sym.to_string())
        print("\n--check: nothing written")
        return

    blob_root = _extract_blobs(args.rar, args.unrar, args.extract_to)

    reasons = collections.Counter()
    totals = {"raw": 0, "scored": 0, "with_text": 0}
    per_symbol = []

    for sym, grp in idx.groupby("ticker"):
        grp = grp.copy()
        texts, why = [], []
        for rel in grp["content_file_path"]:
            t, r = _load_text(blob_root, rel)
            texts.append(t)
            if r:
                reasons[r] += 1
            why.append(r)
        grp["text"] = texts

        raw = pd.DataFrame({
            "url_hash": grp["url_hash"].astype(str),
            "published_at": grp["published_at"],
            "provider": grp["provider"].astype(str),
            "source": grp["source"].fillna("").astype(str),
            "title": grp["title"].fillna("").astype(str),
            "summary": grp["summary"].fillna("").astype(str),
            # Empty string, not None: the column is "what text we have", and a symbol
            # whose blobs failed to resolve must be visibly empty, not silently null.
            "text": grp["text"].fillna("").astype(str),
        })
        n_text = int((raw["text"].str.len() > 0).sum())
        store.write_raw(sym, raw)

        sc = grp[grp["sentiment_label"].isin(FINBERT_LABELS)]
        scored = pd.DataFrame({
            "url_hash": sc["url_hash"].astype(str),
            "published_at": sc["published_at"],
            "provider": sc["provider"].astype(str),
            "model": LEGACY_MODEL,
            "score": sc["sentiment_score"].astype(float),
            "pos": sc["positive_prob"].fillna(0.0).astype(float),
            "neu": sc["neutral_prob"].fillna(0.0).astype(float),
            "neg": sc["negative_prob"].fillna(0.0).astype(float),
        })
        store.write_scored(sym, scored)

        totals["raw"] += len(raw)
        totals["scored"] += len(scored)
        totals["with_text"] += n_text
        per_symbol.append({"symbol": sym, "raw": len(raw), "scored": len(scored),
                           "with_text": n_text,
                           "first": grp["published_at"].min().date(),
                           "last": grp["published_at"].max().date()})
        print(f"  {sym:<6} raw={len(raw):>6,}  scored={len(scored):>6,}  text={n_text:>6,}")

    print()
    print(pd.DataFrame(per_symbol).to_string(index=False))
    print(f"\nraw rows      : {totals['raw']:,}")
    print(f"scored rows   : {totals['scored']:,}")
    print(f"rows with text: {totals['with_text']:,} "
          f"({100.0 * totals['with_text'] / max(1, totals['raw']):.1f}%)")
    if reasons:
        # Reported, never swallowed: an unresolved blob is a real gap in the raw tier and
        # whoever re-scores later needs to know how big it is.
        print("\nunresolved text, by reason:")
        for k, v in reasons.most_common():
            print(f"  {k:<24} {v:>8,}")
    print(f"\nraw    -> {store.raw_folder()}   (master only, never synced)")
    print(f"scored -> {store.scored_folder()}   (syncs to workers with the cache)")


if __name__ == "__main__":
    main()
