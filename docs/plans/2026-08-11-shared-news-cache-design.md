# Shared News Cache + Sentiment + Optional DeterministicScorer News Section — Design

**Date:** 2026-08-11
**Status:** design agreed, not yet implemented

**Goal:** migrate the ML test platform's raw news cache into a shared on-disk store usable by
both the test platform and the live trade platform (so live never downloads or parses news
twice), rebuild sentiment from the raw text, and add news as an **optional** section to
DeterministicScorer.

---

## 1. What we recovered

The source archive is on **G:**, not H:

| Artefact | Location | Contents |
|---|---|---|
| Content blobs | `G:/Mon Drive/Work/AiTrading/Test ML Cache/datasets/cache.rar` (963 MB) | 132,238 files, `cache/news/<provider>/<2-char shard>/<sha256>.json`, each base64-encoded `{"content", "cached_at"}` |
| **Index** | `G:/…/Test ML Cache/datasets/dl_forecasting.db` (204 MB) | `news_cache` table, **131,608 rows** |

The blobs alone are useless — they carry no ticker, no publish date, no headline. The index is
what makes them addressable, and it lives in that separate 204 MB DB (**not** in the damaged
34.7 GB `ba2_ml_cache_export.zip`, whose `manifest.json` fails CRC-32).

> The **local** `~/Documents/ba2/test/dl_forecasting.db` has the `news_cache` table but **0 rows**.
> The ML engine's news features are therefore already dataless on this machine. This migration
> restores them; it does not put them at risk.

### Coverage

26 tickers (not 30 — no AMZN, TSLA, META, AMD):

```
AAPL ADBE ADP AMGN APP ASML AVGO CHTR COST CSCO GOOG GOOGL HON
INTC INTU LIN LRCX MSFT NFLX NVDA PEP QCOM SBUX TMUS TXN VRTX
```

| Years | Rows/yr | Verdict |
|---|---|---|
| 2011–2018 | 480–780 | too sparse, unusable |
| 2019–2021 | 7.6k–11.8k | usable |
| 2022–2025 | 13.4k–32.7k | dense (~2–3.5 articles/ticker/day) |
| 2026 (to 03-02) | 7.5k | dense, then frozen |

Providers: `fmp` 122,473, `finnhub` 8,145, `alphavantage` 990.

**Consequence:** a news-enabled grid can only ever run the **large** cap band. Mid and small
have zero coverage. The cache is frozen at 2026-03-02, so live must fetch forward from there.

#### Correction (measured 2026-08-11, at migration time)

Per-symbol coverage is far more uneven than "26 tickers, 2019+" suggests, and two of the
assumptions above were wrong:

| Symbol | Rows | Range | Note |
|---|---|---|---|
| GOOG | 19,576 | 2011-02 → 2026-03 | densest |
| NVDA | 19,092 | 2017-11 → 2026-03 | densest with a modern start |
| **MSFT** | 2,757 | **2024-11 → 2025-12** | **13 months only** |
| **AAPL** | 7,542 | 2021-04 → 2026-02 | only **3,573 scored** |
| LIN | 886 | 2013-07 → 2026-03 | thinnest |

§9 proposed "NVDA or MSFT, both 2022+" as the acceptance symbol. **MSFT does not qualify** —
its window starts 2024-11 and ends 2025-12. Use **NVDA** (or GOOG). AAPL additionally holds
most of the 3,478 unscored rows.

**The blobs are much smaller than the archive suggests.** `cache.rar` is 963 MB, but the news
tree is only **122.8 MB uncompressed across 132,237 files** — median **344 bytes**, p90 956 B,
and only 6,721 files over 4 KB. The remaining ~4 GB is `cache/jobs` dataset CSVs. Two
consequences:

- The archive is **solid**, so extracting only `cache/news/**` still requires decompressing
  through the job CSVs first. Budget minutes, not seconds.
- `content_fetched=1` for only **30,584 of 131,608 rows (23%)**. The other 77% never had a
  full-article scrape — but their blobs are *not* empty: they hold the vendor summary
  (mean 634 chars, vs 195 for the "fetched" ones, which are often truncated ledes).

So the text substrate is **headline + summary**, roughly 250–650 characters, not article
bodies. That is a legitimate sentiment substrate and it is what FinBERT already scored, but it
caps what any model can extract and it makes the §7 bake-off far cheaper than the estimated
30–60 min/model.

**The legacy probabilities are one-hot.** Exactly zero rows have more than one non-zero class:
a `positive` row is `pos=0.92, neu=0, neg=0`. The stored numbers are *label + confidence*, not
a distribution. `pos − neg` still behaves correctly (neutral → 0), but any comparison between
`finbert-legacy` and a real re-score must know the two are not the same kind of number.

### Existing sentiment is not one model

128,130 rows carry labels in **two incompatible schemas**:

- FinBERT lowercase `positive/neutral/negative` — 127,134 rows
- AlphaVantage's own `Somewhat-Bullish/Bullish/Neutral/Bearish` — 996 rows, **never scored by
  FinBERT**, these carry the vendor's label
- 3,478 unscored

A rebuild is therefore not only an upgrade — it is what makes the column self-consistent.

---

## 2. Why the format must change

`cache_sync.build_manifest` recurses the **entire** cache tree ("no allowlist to drift"), so
anything on disk under `CACHE_FOLDER` syncs to remote workers for free. `.sqlite` files sync
too; only `-wal`/`-shm`/`.tmp`/`.part`/`.lock`/`.journal` are skipped.

The cache today is **104,284 files / 30.3 GB**. Dropping the rar in as-is would add 132,238 tiny
JSON blobs — **more than doubling the manifest file count** for ~1 GB of data. Every job
pre-flight `rglob`s and `stat`s every file, so that roughly doubles pre-flight cost on *every
job in the running grid*. Preserving the content-addressed layout is therefore not an option.

---

## 3. Storage architecture

**Home:** `ba2_providers/news/` (alongside the existing `FMPNewsProvider` / `FinnhubNewsProvider`).
New `store.py`; scorer in `sentiment.py` with `transformers`/`torch` behind an optional extra so
the live platform doesn't pull FinBERT unless it scores.

Two **physically separate** tiers, joined on `url_hash` (the sha256 already in the index, unique
per article, and already the blob filename):

```
RAW    ~/Documents/ba2/common/news_raw/<SYM>.parquet          master-only
       url_hash, published_at, provider, source, title, summary, text
       - OUTSIDE CACHE_FOLDER -> never enters build_manifest, never syncs
       - never opened by an expert or a backtest
       - exported to GDrive as a 3rd source in tools/ml_cache_archive.py

SCORED ~/Documents/ba2/common/cache/news/<SYM>.parquet        synced
       url_hash, published_at, provider, model, score, pos, neu, neg
       - under CACHE_FOLDER -> syncs, prunes, CRC-checks for free
       - ~26 manifest entries, a few MB total
```

Separating them by **file** makes "raw text is never loaded into memory on a worker" structural
rather than a discipline to remember at every read site.

The `model` column is what the split buys: a re-score writes new rows without touching raw, and
a stale mixed-model file is detectable rather than silent. It is also where the 996 AlphaVantage
vendor labels are handled honestly — they are excluded, not laundered into the same column.

**Read API:** `read_sentiment(symbol, as_of, window_days) -> DataFrame`, filtering
`published_at <= as_of` — the same no-lookahead contract the rest of the cache enforces.

### Why raw lives outside the cache root

Export and sync consume the cache tree and, for the first time, need to **disagree**: export
wants the raw text, sync must not have it. Putting raw outside `CACHE_FOLDER` preserves
`cache_sync`'s strongest invariant — *everything under the cache root syncs, no exceptions* —
which the code comment explicitly relies on to stop allowlists drifting.
`tools/ml_cache_archive.py` already has a named-source list (`cache`, `db`), so `news_raw`
joins it there and hangs off `--scope`.

### Not in scope: the ProviderCache DB event store

Investigated and **dropped**: `read_event_rows`/`upsert_event_rows` have **no production
callers** (only `native_cache.py` itself and three test files), and the `providercache` table
does not exist in the DB. Everything DeterministicScorer reads is already on disk and already
syncs — OHLCV via `FMPOHLCVProvider/*.parquet`, analyst grades and price targets via
`fmp_history/*.json`. There is nothing to migrate. It is dead substrate, not a live gap.

---

## 4. Defect found while checking the above: the macro section is inert

`DeterministicScorer/data.py:289`:

```python
macro = providers.macro() if hasattr(providers, "macro") else None
if macro is None:
    return out          # all five FRED inputs None
```

**No bundle in the codebase defines `macro()`** — `grep "def macro"` returns nothing outside an
unrelated TradingAgents node. Confirmed by running it: `hasattr(LiveProviderBundle, "macro")` is
`False`, and `fetch_macro_series` returns all-`None`, in **live and backtest alike**. Combined
with `breadth: None` (`__init__.py:609`), **six of the seven regime inputs are permanently
absent** and `regime_composite` renormalizes onto the one survivor.

The "macro regime" is currently exactly `+1 if SPY > SMA200 else -1`.

Consequences for the DeterministicScorer grid:

- `mw_breadth`, `mw_vix`, `mw_credit`, `mw_yield_curve`, `mw_pmi`, `mw_sahm`, `vix_calm`,
  `vix_stress`, `yc_scale` are **inert genes**, burning GA search width.
- `hard_riskoff` requires `MIN_INPUTS_FOR_RISKOFF = 2`; with one input it can **never fire**.

Same defect class as `use_atr_stop`, `DaysOpened`, `apply_stop_losses`,
`screener_dollar_volume_min`. **Fix before the grid, not after.**

**Sweep result — this one is a singleton, not a pattern.** Across 9 experts and ~400 declared
settings: exactly one `hasattr(providers, …)` in the codebase (this one); no other
declared-but-unread setting; `FactorRanker`'s `current_price: None` is legitimate and documented
(basket-level, cross-sectional).

### 4b. It cannot be fixed by wiring — the provider implements a different contract

`FREDMacroProvider` is a **markdown-report generator**, not a time-series source:

| What the expert needs | What the provider returns |
|---|---|
| `unrate_series`, `oas_series` as **series** | `latest = valid_obs[0]` — latest value only |
| keys by FRED series ID (`VIXCLS`, `UNRATE`) | keys by friendly name (`"VIX"`, `"Unemployment Rate"`) |
| `yc.get("10y")` → dict of series | a **list** of `{maturity: "10 Year", yield, date}`, latest only |
| `BAMLH0A0HYM2` (HY OAS) | **not in `INDICATOR_SERIES`** |
| `T10Y3M` | absent; only `DGS10`/`DGS3MO` latest values |
| `NAPM` for PMI | present in the list, but **the series does not exist on FRED** (verified) |

Even with `macro()` added to the bundle every input would still resolve to `None`. The
`hasattr` guard was not hiding a wiring gap — it was hiding an integration that was **never
built**, and nothing ever threw because the guard short-circuited before the mismatch surfaced.

Additional blockers in `_get_fred_data`: a bare `requests.get` with **no cache** (every bar ×
every worker would hit the FRED API, breaking the hermetic contract) and `limit: 100,
sort_order: desc` — only the last 100 observations, while the credit z-score wants ~756.

### 4c. Verified approach for the rebuild

**Point-in-time via FRED vintages, not lag heuristics.** `output_type=4` (initial release only)
with `realtime_start=1776-07-04&realtime_end=9999-12-31` returns each observation stamped with
its true first-publication date. Verified:

```
UNRATE        obs=2024-01-01 val=3.7   first_pub=2024-02-02   <- real 32-day lag, from FRED
BAMLH0A0HYM2  obs=2024-01-02 val=3.54  first_pub=2024-01-02   <- same-day (daily series)
```

Filtering on `realtime_start <= as_of` gives zero-lookahead point-in-time with no guessing.

Per-series mode (verified empirically):

| Series | Mode | Reason |
|---|---|---|
| `UNRATE`, `CPIAUCSL`, `PAYEMS`, `GDP` | `output_type=4` vintages | revised + published with a lag |
| `VIXCLS`, `T10Y3M`, `BAMLH0A0HYM2`, `DGS*` | default, filter on `date` | never revised, same-day; `output_type=4` is rejected anyway ("3,907 vintage dates") |
| PMI | **needs a replacement** — `NAPM` does not exist | ISM pulled FRED licensing ~2016 |

**Reuse, don't rebuild:** `testplatform/backend/app/services/macro.py` (`MacroService`, 372
lines) already returns **series** with forward-fill OHLC alignment — the shape the expert needs
— and its line 97 already notes it is "intended to be sourced through ba2_providers' shared
cache". It lacks a disk cache and vintage handling; those are the additions.

**Scope of the real fix:** point-in-time series method → per-series vintage mode → disk cache
under `CACHE_FOLDER` (syncs to workers, keeps backtests hermetic) → prewarm hook → remove the
`limit: 100` truncation → PMI replacement → bundle `macro()` wiring → rewrite the expert's
`fetch_macro_series` against the new contract → delete whichever genes remain unbacked.

The FRED API key lives in `AppSetting.fred_api_key` (present in the trade DB), **not** in `.env`.

---

## 5. News integration into DeterministicScorer

```python
"use_news":            {"default": False},   # explicit opt-in, NEVER hasattr-inferred
"news_lookback_days":  {"default": 7},
"news_min_articles":   {"default": 3},
"w_news":              {"default": 0.15},
"news_half_life_days": {"default": 3.0},
```

Aggregation over articles with `published_at` in `(as_of − lookback, as_of]`:

```
s_i   = pos_i − neg_i                    # signed; neutral mass ignored
w_i   = 0.5 ** (age_days_i / half_life)  # recent news dominates
score = Σ(w_i·s_i) / Σ(w_i)              # in [-1, +1], same range as other sections
```

`news_min_articles` is the guard that `min_price_targets_per_quarter` already earned: one
article is noise wearing a signal's clothes.

### Absence semantics — deliberately not collapsed

| Case | Backtest | Live |
|---|---|---|
| `use_news=False` | section absent, no store read | same |
| enabled, store dir missing entirely | **raise** — misconfiguration | **raise** — misconfiguration |
| enabled, symbol not in store | **raise, fail the run** | **skip symbol, no recommendation** |
| enabled, symbol present but `< news_min_articles` | drop section, renormalize | drop section, renormalize |

The distinction: **coverage** (does this symbol exist in the store?) is structural and fails
loudly per the hermetic contract; **density** (enough articles this window?) is normal variation
— a quiet week for a covered symbol must not fail a backtest or mute the symbol.

This is the lesson of §4 encoded: "feature on, data structurally unreachable" must be loud. A
silently renormalized news section would let the GA tune `w_news` on a universe where the
section mostly never fires — a weight that looks harmless and does not transfer.

**Consequence, stated up front:** a news-enabled grid is hard-restricted to the 26 covered
symbols. That is the honest constraint rather than one hidden in a renormalization.

---

## 6. Migration

`tools/migrate_ml_news_cache.py` — one-off, idempotent:

1. Read 131,608 index rows from the recovered `datasets/dl_forecasting.db`.
2. Extract `cache.rar` to temp; resolve each `content_file_path` → blob → base64-decode →
   `content`.
3. Write RAW: 26 × `news_raw/<SYM>.parquet`.
4. Write SCORED from existing columns as `model="finbert-legacy"`, **excluding** the 996
   AlphaVantage vendor-label rows.
5. Validate **loudly**: per-symbol counts, unresolved blobs reported not swallowed, no null
   `published_at`. The 194 rows without `content_file_path` and the ~630 index/blob discrepancy
   are reported, never silently dropped.

---

## 7. Sentiment rebuild

Score the raw text with FinBERT (incumbent baseline) plus two modern finance models, each
written under its own `model` value.

**Selection criterion: information coefficient of the daily aggregate against 1d/5d forward
returns**, per symbol and pooled, using OHLCV already in cache — *not* a labelled benchmark like
FiQA. We do not need the model that best labels a sentence "positive"; we need the one whose
aggregate predicts forward return on **this** universe.

This doubles as the go/no-go for §5: **if no scorer shows a usable edge on these 26 names, the
honest outcome is to migrate the cache and not wire it into the expert**, saving a whole grid.

**Scheduling constraint:** ~30–60 min of saturated CPU per model, torch pinned CPU-only
(`2.6.0+cpu`, Windows). Local and remote are both near their memory ceiling with the grid
running. The scoring pass must be scheduled **around** the grid, not alongside it.

### 7b. Bake-off result (2026-08-11): no model shows a usable edge

Ran on a seeded stratified sample of **5,794 articles** (≤30 per symbol-year, 2019–2026),
24 symbols with price data, 4,378 symbol-days. Scored in memory; the store was not touched.

| model | IC 1d | t | IC 5d | t | rows/s |
|---|---|---|---|---|---|
| distilroberta-news | +0.0200 | +1.23 | +0.0143 | +0.75 | **9.0** |
| twitter-roberta | +0.0130 | +0.75 | +0.0090 | +0.54 | 4.5 |
| finbert | +0.0123 | +0.81 | +0.0203 | +1.07 | 4.1 |
| finbert-tone | +0.0106 | +0.54 | +0.0025 | +0.12 | 4.7 |
| finbert-legacy | +0.0097 | +0.59 | +0.0124 | +0.64 | — |

**Nothing reaches significance.** Every |t| < 1.3. Supporting evidence that this is noise
rather than a weak-but-real signal:

- Only **11–14 of 24 symbols** are positive for any model — a coin flip.
- Per-symbol IC scatter is σ ≈ 0.08 against a mean of ≈ 0.015, so SE ≈ 0.016 and every
  interval comfortably contains zero.
- **The result did not replicate.** An earlier draw (≤25/symbol-year, 4,835 articles)
  gave distilroberta 1d IC **+0.041, t=+2.49** and legacy **+0.053, t=+3.23**. Growing the
  sample by 20% roughly halved both and killed the significance. A real effect gets *more*
  significant with more data. The first draw was noise, and any conclusion drawn from it
  should be discarded.
- Per-symbol ICs correlate **0.55–0.88 across models**. The models largely agree with each
  other, so the between-model differences in the table are noise riding on one shared,
  weak signal — which means model choice barely matters here.

**Answer to "is there better than FinBERT": not demonstrably.** `distilroberta-news` is
nominally top at 1d *and* 2× faster *and* half the memory, so it is the one to use if any
is used — but it is not statistically distinguishable from the incumbent.

**Caveats — this is "not demonstrated", not "proven absent":**

1. **Daily bars blur the main event window.** News published at 09:00 is matched to
   `close[t] → close[t+1]`, which misses the intraday reaction that same session — likely
   where most of the effect lives. Resolving this needs intraday bars.
2. **The aggregation is a crude proxy**, not the proposed feature: a plain daily mean, with
   no half-life weighting and no `news_min_articles` floor. §5's actual formulation was
   not what got tested.
3. **24 symbols is a small cross-section** for a t-test over per-symbol ICs.

**Consequence for §5:** on this evidence, wiring the news section into DeterministicScorer
is **not justified**. The `use_news=False` default already means nothing regresses, but
adding a GA-tunable `w_news` on an undemonstrated signal spends search width on noise —
the same mistake as the inert macro genes, arrived at from the opposite direction.

The cheap way to settle it is to test the **actual** §5 aggregation (half-life + article
floor) on a larger sample with `distilroberta-news` alone — ~1 h at 9 rows/s — rather than
to conclude from a proxy.

### 7c. SUPERSEDES 7b — at intraday resolution the signal is real

§7b's negative result was **an artefact of daily bars**, exactly as its caveat 1 warned.
Matching a 09:00 article to `close[t] → close[t+1]` discards the entire same-session
reaction. 5-minute bars exist in cache for every covered symbol (2011→2026-06), so the
event can be measured directly instead of inferred from a daily average.

`tools/news_event_study.py` matches each article to the first 5-min bar **strictly after**
its publication timestamp and measures forward returns from that bar's open. It needs no
scoring — it reads the `finbert-legacy` scores already migrated — so it runs in minutes
over **122,084 articles across all 26 symbols**, 2019–2026.

**All news (122,084 articles):**

| horizon | IC | t | pos_bp | neu_bp | neg_bp | pos−neg | t |
|---|---|---|---|---|---|---|---|
| 30m | +0.0133 | +2.59 | −1.53 | −1.13 | −8.45 | +6.92 | +6.26 |
| 1h | +0.0119 | +2.33 | −1.08 | −0.50 | −6.98 | +5.90 | +4.58 |
| 1d | +0.0146 | +2.27 | +5.40 | +7.17 | −2.96 | +8.36 | +3.40 |

**Tradable within the hour (56,849 articles, median gap 4 min):**

| horizon | IC | t | pos_bp | neu_bp | neg_bp | pos−neg | t |
|---|---|---|---|---|---|---|---|
| 5m | +0.0015 | +0.21 | +0.09 | −0.13 | −1.09 | +1.18 | +1.53 |
| 30m | +0.0069 | +1.28 | +1.24 | +0.39 | −3.41 | +4.65 | +3.56 |
| 1h | +0.0088 | +1.50 | +2.88 | +1.04 | −2.24 | +5.12 | +3.14 |
| 1d | +0.0142 | +2.08 | +12.07 | +6.86 | −5.73 | **+17.79** | **+4.89** |

Three findings:

1. **The first 5–15 minutes are already priced.** t ≈ 0.2, spread ≈ 1.2 bp. There is no
   immediate-reaction edge to capture, which is reassuring — a large one would have
   implied a timestamp bug.
2. **The edge accrues over hours, not seconds**, building 4.65 → 17.79 bp from 30 min to
   one day on the tradable subset.
3. **Timing changes the SHAPE of the signal, not just its size.** On the tradable subset
   both sides work (pos +12.07 vs neu +6.86 at 1d). On the full sample positive news earns
   nothing — at 1d it *underperforms neutral*, +5.40 vs +7.17 — while negative news still
   falls. Only **47%** of articles are tradable within an hour; the rest publish outside
   market hours, and by the next open the good news is fully priced while the bad news
   keeps drifting.

**Implication for §5 — the section must not be symmetric.** DeterministicScorer decides on
daily bars: it cannot act four minutes after a headline, so it lives in the *full-sample*
regime, where the entire edge is on the negative side. A symmetric `w_news` would spend
half its GA range on a half of the signal that does not exist for this expert.

What the data supports is a **negative filter**: penalise or veto names carrying fresh bad
news, and do not reward good news at all. That is a different setting shape from §5's
`w_news` — closer to `news_veto_threshold` + `news_lookback_days` — and it is cheaper to
search.

**On model choice:** §7b's ranking was measured through the same broken daily aggregation
and should not be used to pick a model. The event study runs on `finbert-legacy`, so the
re-score question is now open again and should be re-tested at intraday resolution with
`distilroberta-news` (nominally best, 2× faster, half the memory) before any full pass.

### 7d. Persistence under a realistic fetch lag — the decisive test

**We do not receive news in real time.** A daily news job acts up to a day after
publication, so the only thing that matters is what survives that delay. Re-ran the event
study with `--entry-lag-min 1440` (enter one day after publication) against the zero-lag
upper bound. Same 122,084 articles, 26 symbols.

**pos − neg spread (bp), and the IC t-stat:**

| horizon | lag 0 spread | t | **lag 1d spread** | **t** |
|---|---|---|---|---|
| 30m | +6.92 | +6.26 | +2.83 | +2.80 |
| 1h | +5.90 | +4.58 | +1.62 | +1.37 |
| 1d | +8.36 | +3.40 | +3.72 | +1.61 |
| 2d | +14.22 | +4.27 | **+6.85** | **+2.13** |
| 3d | +16.14 | +3.97 | **+10.96** | **+2.83** |
| 5d | +15.92 | +3.17 | +3.81 | +0.78 |
| 10d | +6.68 | +1.01 | −0.91 | −0.14 |

**The effect does persist a few days, and only a few.** With a one-day lag the spread is
still significant at 2–3 days (+10.96 bp, t=2.83), then collapses by day 5 (t=0.78) and is
gone at 10. Rank IC, by contrast, is dead at every horizon once lagged (all |t| < 1.8) —
the surviving signal lives in the *tails*, not in the ranking of the middle.

**It is entirely a negative-side effect.** At 3 days with the lag applied:

```
pos +32.22 bp    neu +32.27 bp    neg +21.26 bp
```

Positive news is indistinguishable from no news — 32.22 against 32.27. The whole spread is
negative news underperforming by ~11 bp. This holds across every lagged horizon.

**Why this makes the feature worth having anyway.** ~11 bp over three days would be
marginal as a tradable alpha after costs, but as a **veto it costs nothing to act on** —
declining to open a position pays no spread and no commission. The asymmetry and the
transaction-cost problem cancel: the only usable form of the signal is also the only form
that is free to use.

**Final shape for §5, replacing the original settings:**

| §5 as designed | What the data supports |
|---|---|
| symmetric `w_news` contribution | **negative-only veto/penalty**; no positive-side weight |
| `news_lookback_days: 7` | **2–3 days** — the edge is gone by day 5 |
| `news_half_life_days: 3.0` | decay is faster; half-life ≈ 1 day, or a hard window |
| `news_min_articles: 3` | keep — density still matters |

A symmetric `w_news` with a 7-day lookback and 3-day half-life would have averaged mostly
dead information and spent half its GA range on a positive side worth exactly zero.

### 7e. VERDICT — the signal does not survive a weekly analysis cadence

DeterministicScorer analysis runs **once or twice a week**, so the realistic entry lag is
3–7 days, not one. Re-ran at those lags. Same 122,084 articles.

**Best pos−neg spread at any horizon, by entry lag:**

| entry lag | best spread | t | usable? |
|---|---|---|---|
| 0 (real time) | +16.14 bp @3d | +3.97 | yes — unreachable |
| 1 day | +10.96 bp @3d | +2.83 | yes, negative-side only |
| **3 days** | +3.71 bp @2d | +1.23 | **no** |
| **7 days** | — | all \|t\| < 1.8, ICs negative | **no** |

At a 3-day lag every horizon is inside noise, and by 5–10 days the sign flips
(−3.62 bp, −7.86 bp). At 7 days every IC is negative. There is nothing left to capture.

**Conclusion: do not wire the news section.** Not because the cache is bad, the model is
wrong, or the aggregation was mis-specified — those were all fixed along the way. The
binding constraint is **cadence**. News decays over roughly 3 days; an expert that looks
at the world twice a week arrives after it is over.

**What would change the answer**, in order of cost:

1. **Run the news check daily**, decoupled from the weekly full analysis — a cheap
   negative-news veto pass over open and candidate positions. This is the only variant the
   data supports (~11 bp of avoided drag over 3 days, at zero transaction cost since a veto
   places no trade).
2. Intraday cadence would recover the full +16 bp, but that is a different platform.

**What was NOT the problem, for the record:** the migrated cache is sound (99.9% text
resolution), the four models are statistically indistinguishable from one another, and
§7b's null result was a measurement artefact, not the truth. The signal is real at
+6.9 bp/30 min. It is simply not reachable on this schedule.

**Value retained regardless:** the migration restores the ML engine's `news_*` features,
which were dataless on this machine, and the store is in place if the cadence ever changes.

---

## 8. Not breaking the ML engine

The ML side reads the `news_cache` table + `datasets/cache/news` blobs via `NewsCacheService`,
feeding `SentimentService.fetch_news_for_ticker(...)` → `news_*` columns
(`news_count`, `news_{period}_count`, `news_{period}_{sentiment}`) in
`app/services/features_source.py`.

That shape — *articles in a window, aggregated by sentiment* — is the **same** aggregate §5
needs. One store genuinely serves both; this is convergent, not forced.

A seam for exactly this already exists: `FEATURES_SOURCE` routes feature fetches through
`ba2_providers` "so experts (point-in-time slices) and ML training (a materialized feature/target
matrix) share one cache", with `news -> get_provider("news", name)` already wired.

Plan: complete that route for news so `NewsCacheService` reads the shared store, keeping the
`news_*` column semantics identical. **Default stays `legacy`**, honouring the existing "do NOT
flip the default until per-block equivalence is documented" note. Gate the flip on a test that
rebuilds one dataset both ways and asserts the `news_*` columns match.

---

## 9. Live fetch, prewarm and acceptance test

- **Live fetches from every provider with a configured key** — not FMP alone. `AppSetting`
  currently holds keys for `fmp_api_key`, `finnhub_api_key` and `alpha_vantage_api_key`, matching
  the three providers already present in the archive. Providers without a key are skipped
  silently; a provider with a key that fails is logged, not swallowed. Results are merged and
  deduped on `url_hash` so the same article from two providers is stored once.
- **Prewarm support** is required, on the same footing as the other buckets: the backtest
  prewarm hook must populate `cache/news/<SYM>.parquet` for the run's universe so GA workers
  read from disk and never fetch. Without this, an enabled news section either fetches per bar
  or fails the coverage check.
- **Reuse the existing test-platform news fetch script** rather than writing a new fetcher —
  `app/services/news_batch_handler.py` already does batched multi-provider fetch + scoring.
- **Acceptance test on a covered symbol.** After migration, run a backtest on a symbol with
  known dense coverage (e.g. NVDA or MSFT, both 2022+) with `use_news=True` and assert: the
  section produces non-null scores, the article counts match the store, no network call occurs,
  and an uncovered symbol fails the run per §5.

## 10. Open items

- Fold the §4 macro fix into this plan, or track separately? (Recommend: fix first — it
  invalidates DeterministicScorer GA runs that tune the nine inert genes.)
- DeterministicScorer is still excluded from the running grid
  (`--skip-experts DeterministicScorer`); re-inclusion not yet green-lit.
- Live forward-fetch from 2026-03-02 onward is required for the store to stay current; the
  fetchers exist (`FMPNewsProvider`, `FinnhubNewsProvider`) but the append path is new work.
