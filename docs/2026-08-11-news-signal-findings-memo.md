# News signal — findings memo (2026-08-11)

**Status:** work paused deliberately. Cache migrated and usable; expert integration NOT done
and not currently justified. Revisit after the goal2020 grid with bigger samples and more
models.

Full detail in `docs/plans/2026-08-11-shared-news-cache-design.md` §7b–7e. This memo is the
short version plus everything worth knowing before picking it up again.

---

## 1. What exists now (done, committed, safe to use)

| Artefact | Location | State |
|---|---|---|
| Two-tier news store | `ba2_providers/news/store.py` | 20 tests, coverage-vs-density semantics pinned |
| Migrated cache | `cache/news/<SYM>.parquet` (11.9 MB, syncs)<br>`news_raw/<SYM>.parquet` (85.6 MB, master-only) | 26 symbols, 127,134 scored, 131,608 raw |
| Sentiment scorer | `ba2_providers/news/sentiment.py` | 4 models, name-based label mapping |
| Migration | `tools/migrate_ml_news_cache.py` | idempotent per symbol |
| Bake-off | `tools/news_sentiment_bakeoff.py` | IC vs forward returns, samples, never writes |
| Event study | `tools/news_event_study.py` | 5-min bars, entry-lag aware |
| Persistence flags | `tools/news_persistence.py` | symbol-day level, 5 flags |

Coverage: 26 symbols, 2019–2026-03-02 usable (pre-2019 is <3 articles/symbol/month).
Densest: GOOG 19,576, NVDA 19,092. **MSFT is only 2024-11..2025-12** — not an acceptance
symbol despite what the design doc originally said.

---

## 2. The finding, in one line

**The news signal is real and strong (+6.9 bp at 30 min, t=6.3), decays over ~3 days, and is
dead at the 3–7 day lag implied by a once-or-twice-weekly analysis run.**

Spread (pos−neg, bp) at best horizon, by entry lag:

| lag | best spread | t | usable |
|---|---|---|---|
| 0 | +16.1 @3d | +4.0 | yes, unreachable |
| 1 day | +11.0 @3d | +2.8 | yes, negative-side only |
| 3 days | +3.7 @2d | +1.2 | **no** |
| 7 days | — | ICs negative | **no** |

**The blocker is cadence, not data.** The cache, the models and the aggregation were each
suspected and each cleared.

---

## 3. Things that would have been easy to get wrong

These cost real time to find. Do not re-derive them.

1. **Label order differs per model, silently.** Three distinct orderings across four
   checkpoints (`finbert` pos=0; `finbert-tone` neu=0,pos=1; `distilroberta`/`twitter`
   pos=2). All emit a 3-vector, so index-based reading yields a confidently inverted
   signal with nothing thrown. `sentiment.py` maps by NAME and runs a sign probe.
2. **Daily bars cannot measure news.** The first bake-off found nothing because a 09:00
   article was matched to `close[t]→close[t+1]`, discarding the same-session reaction.
   The signal appeared immediately at 5-minute resolution. **Never evaluate an event
   signal on daily bars.**
3. **A single sample draw is not a result.** The first bake-off draw gave t=2.49/3.23; a
   20% larger draw halved it to t=1.23/0.59. A real effect strengthens with more data.
4. **The effect is asymmetric.** At a 1-day lag positive news is indistinguishable from no
   news (+32.22 vs +32.27 bp at 3d). All of the spread is bad news falling. A symmetric
   `w_news` would spend half its GA range on nothing.
5. **The first 5–15 min shows nothing** (t≈0.2). That is the *reassuring* result — a large
   immediate edge would have implied a timestamp bug.
6. **Legacy scores are one-hot** (label + confidence), not distributions. `finbert-legacy`
   and any re-score are not comparable numbers.
7. **`finbert-tone` needs two loader overrides** — ships only `vocab.txt` (no
   `tokenizer.json`) and its `config.json` omits `model_type`. Declared per model in
   `MODELS`; do NOT convert to a blanket try/except fallback.
8. **`cache.rar` is 963 MB but the news tree is 123 MB** — the rest is job CSVs. It is a
   solid archive, so extracting only `cache/news/**` still decompresses through them.

---

## 4. What was tested and came back negative

- **Four models are statistically indistinguishable.** Per-symbol ICs correlate 0.55–0.88
  *across* models — they measure the same thing, so model choice barely matters.
  `distilroberta-news` is nominally best AND 2× faster AND half the memory, so it is the
  one to use if any is.
- **No flag identifies long-lived news.** Tested article burst size, wire-source
  materiality, cross-article agreement, news-day reaction size, and whether the reaction
  confirms the sentiment. None isolates persistence. The pattern that appears is
  *reversal*, not drift (confirmed reactions −31 bp at 5d) — opposite to PEAD. Treat as
  unproven: 15 buckets × 3 horizons will produce 2–3 |t|>2 cells by chance.

---

## 5. Open threads for the revisit

1. **Bigger samples.** Everything after the event study used ~6k-article draws. The event
   study itself used all 122k (it needs no inference) and is the more trustworthy result.
2. **Models that emit more than polarity** — probed, available, untested:
   - `yiyanghkust/finbert-fls` (439 MB) — **Not FLS / Non-specific FLS / Specific FLS**.
     A forward-looking-statement detector: the closest off-the-shelf horizon flag.
   - `MoritzLaurer/deberta-v3-base-zeroshot-v2.0` (369 MB) — arbitrary labels via NLI;
     tag earnings / M&A / guidance / litigation / analyst action with no fine-tuning.
   - `yiyanghkust/finbert-esg`, `finbert-esg-9-categories` (439 MB) — topic.
   Lower prior than it looks: the news-day price move is a stronger materiality proxy than
   any label, and it did not isolate persistence.
3. **The one viable form**: a **daily** negative-news veto, decoupled from the weekly
   analysis. ~11 bp of avoided drag over 3 days, and free to act on — a veto places no
   trade, so no spread and no commission. This is the only variant the data supports.
4. **Live forward-fetch** from 2026-03-02 (the cache freeze) is unbuilt.
5. **`FEATURES_SOURCE=ba2_providers` route for news** is unbuilt; default stays `legacy`.

---

## 6. Value retained regardless of the verdict

The migration restores the ML engine's `news_*` features, which were **dataless on this
machine** (`~/Documents/ba2/test/dl_forecasting.db` has the table with 0 rows). That alone
justified the work, independent of the expert integration.
