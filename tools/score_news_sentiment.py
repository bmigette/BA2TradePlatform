"""Score the raw news store with a sentiment model, writing rows under that model's name.

Reads the master-only raw tier, scores it on CPU, and upserts into the scored tier so the
new model sits ALONGSIDE ``finbert-legacy`` rather than replacing it -- that coexistence
is what makes the bake-off (tools/evaluate_news_sentiment.py) a like-for-like comparison
on identical articles.

MEMORY. One model resident at a time, one symbol streamed at a time. Peak is set by
batch size x sequence length, not by the weights, and both are capped low because this is
expected to run while a GA grid holds most of the machine's RAM. Defaults are deliberately
conservative; raise --batch-size only on an idle box.

Usage:
    python tools/score_news_sentiment.py --list
    python tools/score_news_sentiment.py --model distilroberta-news
    python tools/score_news_sentiment.py --model finbert --symbols NVDA GOOG
    python tools/score_news_sentiment.py --all-models --batch-size 16
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "common"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "packages", "providers"))

import pandas as pd  # noqa: E402

from ba2_providers.news import sentiment, store  # noqa: E402


def build_text(row) -> str:
    """Headline plus the best body we have.

    The blob ``text`` is the full content where the ML platform managed to scrape it and
    the vendor summary otherwise; ``summary`` is the index's own field. Both are short.
    The title is always prepended because it is the most reliably present -- and on this
    corpus, often the most informative -- piece of text.
    """
    title = (row.title or "").strip()
    body = (row.text or "").strip() or (row.summary or "").strip()
    if title and body:
        return f"{title}. {body}"
    return title or body


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=None, help="Model key (see --list)")
    ap.add_argument("--all-models", action="store_true", help="Score every registered model")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--max-length", type=int, default=256)
    ap.add_argument("--threads", type=int, default=None,
                    help="Cap torch threads (leave cores for a running grid)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Score only the N most recent articles per symbol (smoke tests)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        print(f"{'key':<20}{'MB':>6}  hugging-face id")
        for k, v in sentiment.MODELS.items():
            print(f"{k:<20}{v['mb']:>6}  {v['hf']}")
        return

    models = list(sentiment.MODELS) if args.all_models else [args.model]
    if not models or models == [None]:
        raise SystemExit("Pass --model <key>, --all-models, or --list")
    for m in models:
        if m not in sentiment.MODELS:
            raise SystemExit(f"Unknown model {m!r}. Known: {sentiment.available_models()}")

    symbols = [s.upper() for s in args.symbols] if args.symbols else store.covered_symbols()
    if not symbols:
        raise SystemExit(
            "The news store is empty. Run tools/migrate_ml_news_cache.py first.")

    for model in models:
        print(f"\n=== {model}  ({sentiment.MODELS[model]['hf']})")
        t0 = time.time()
        total = 0
        for sym in symbols:
            raw = store.read_raw(sym)
            if args.limit:
                raw = raw.sort_values("published_at").tail(args.limit)
            texts = [build_text(r) for r in raw.itertuples()]

            scores = sentiment.score_texts(
                texts, model=model, batch_size=args.batch_size,
                max_length=args.max_length, threads=args.threads)

            out = pd.DataFrame({
                "url_hash": raw["url_hash"].values,
                "published_at": raw["published_at"].values,
                "provider": raw["provider"].values,
                "model": model,
                "score": scores["score"].values,
                "pos": scores["pos"].values,
                "neu": scores["neu"].values,
                "neg": scores["neg"].values,
            })
            store.upsert_scored(sym, out)
            total += len(out)
            print(f"  {sym:<6} {len(out):>6,} rows  "
                  f"mean_score={out['score'].mean():+.3f}  "
                  f"pos%={100 * (out['score'] > 0.2).mean():4.1f}  "
                  f"neg%={100 * (out['score'] < -0.2).mean():4.1f}")
        dt = time.time() - t0
        print(f"  -> {total:,} rows in {dt / 60:.1f} min ({total / max(1, dt):.0f} rows/s)")

    store.reset_cache()
    print("\nModels now present per symbol:")
    print(store.coverage_report().to_string(index=False))


if __name__ == "__main__":
    main()
