# ThetaData EOD options backfill — requirement, measurements, and the workable request shape

Measured 2026-09-02/03 against a live Professional account, Python library `thetadata`.

> **Read §4 first.** This document originally concluded the API path was unsuitable
> (~80–150 days). That was wrong — it measured a per-expiry request shape that is ~40–68x
> slower than what the API actually supports. The corrected projection is **~6 days**.
> §2 and §3 are kept as the record of the superseded shape; do not build against them.

## 1. What we need

| | |
|---|---|
| Universe | **857 US equity underlyings** |
| Window | **2020-01-01 → 2026-09-03** (~6.7 years) |
| Resolution | **EOD** — one row per contract per trading day (no intraday, no tick) |
| Coverage | full chain: every listed expiration, every strike, both rights |
| Fields | OHLC, volume, **bid/ask**, **open interest**, **implied volatility** |
| Scale | **294,599 (underlying, expiration) units** — ~344 expirations per symbol (AAPL alone: 409, F: 348), driven by weeklies |

Greeks are not required — we invert them ourselves via Black-Scholes. Vendor **IV is required**,
because inversion needs a rate + dividend assumption and cannot run where the close is missing.

## 2. Endpoints and parameters used

```python
# 1. bars + quotes + IV  (43 columns; the superset we actually depend on)
client.option_history_greeks_eod(
    symbol="AAPL", expiration=date(2024, 6, 21),
    strike="*", right="both",
    start_date=date(2024, 5, 1), end_date=date(2024, 5, 31))

# 2. open interest — NOT folded into the bars call, so a second request per window
client.option_history_open_interest(
    symbol="AAPL", expiration=date(2024, 6, 21),
    strike="*", right="both",
    start_date=date(2024, 5, 1), end_date=date(2024, 5, 31))

# 3. first traded date — used to clamp start_date off the run's start
client.option_list_dates(request_type="quote", symbol="AAPL",
                         expiration=date(2024, 6, 21))

# 4. expiration discovery
client.option_list_expirations("AAPL")
```

### The unit of work, and what it costs

The largest thing the API will bulk-serve is **one underlying + one expiration + all strikes +
both rights**. That is the *unit*. AAPL has ~409 expirations in our window; across 857 symbols
that is the **294,599 units** in §1.

A unit is still not one request. The server rejects spans >365 days, and 30-day windows measured
fastest (§3), so each unit is sliced into ~30-day windows covering the contract's life — first
listed date through expiry. Each window costs **two** round trips, because open interest is not
folded into the bars response.

```
cost(unit) = 1 × option_list_dates          (first-traded clamp, once per unit)
           + 2 × ceil(listed_days / 30)     (bars + open interest, per window)
plus 1 × option_list_expirations per underlying (857 total — negligible)
```

Worked example, matching the measurement in §3: **AAPL exp=2024-06-21** was listed ~2.2 years
ahead (third-Friday monthlies list years out) → 27 windows → **54 data requests**.

Weeklies dominate the count (~85% of units) but live only weeks, so most units cost ~4 requests;
the long-dated minority cost up to ~54. Whole plan ≈ **3.4 M requests**.

## 3. Measured response times

**Window size vs throughput** — one expiry, all strikes, both rights, identical data:

| span | wall | rows | rows/sec |
|---|---|---|---|
| 7 days | 4.1 s | 732 | 178 |
| **30 days** | **8.9 s** | **2,864** | **323** ← best |
| 90 days | 38.8 s | 7,988 | 206 |
| 365 days | 130.4 s | 28,224 | 216 |

**Endpoint comparison** — same window, `AAPL` 2024-06-21, May 2024:

| endpoint | cols | wall | rows/sec |
|---|---|---|---|
| `option_history_greeks_eod` | 43 | 11.9 s | 240 |
| `option_history_eod` | 20 | 10.6 s | 271 |

Dropping greeks/IV from the payload buys only ~13%, so cost is not payload-bound.

**One real unit, end to end** (30-day windows, start clamped to first traded date):

```
AAPL exp=2024-06-21   1410.8 s   54 requests   60,448 bars   →  43 rows/s effective
```

**Sustained backfill rate** (whole-universe run, our own worker):

| concurrency | units/hour |
|---|---|
| 4 (Standard limit) | ~43 |
| 6 | ~86 |
| 10 | ~150–197 |

After ~30 hours of continuous running: **2,902 partitions, 5.8 GB — roughly 1% of the plan.**

## 4. RETRACTED — the per-expiry shape was the wrong shape

> **This section previously concluded the API path was unsuitable at ~80–150 days. That
> conclusion was WRONG, and the sections above describe a request shape we should not use.**
> Re-measured 2026-09-03. The error was mine: the per-expiry unit of work in §2 is not the
> cheapest shape the API offers, and a `"day-at-a-time"` rejection seen on ONE endpoint was
> generalised to all of them.

`option_history_eod` accepts **`expiration="*"` over a full 365-day range**, returning every
expiration's chain in a single call:

| call | wall | rows | rows/s |
|---|---|---|---|
| `exp=*` 1-day | 1.5 s | 2,178 | 1,462 |
| `exp=*` 30-day | 14.8 s | 44,248 | 2,981 |
| `exp=*` 365-day | 193–337 s | ~565,000 | ~2,300–2,950 |
| per-expiry, whole unit (§3) | 1,410.8 s | 60,448 | **43 effective** |

That is **~40–68x** the per-expiry path. The gap was never a throughput ceiling — it was
per-request overhead across tens of thousands of small windows.

**Verified complete, not truncated** (this matters more than the speed):

```
wide exp='*' vs per-expiry, same expiry + same window:
   rows lost by wide : 0      rows extra : 0      differing closes : 0   => IDENTICAL
365-day call: asked 2024-01-01..2024-12-31, got 2024-01-02..2024-12-31, 252 days => FULL SPAN
```

`option_history_open_interest` also takes `exp="*"` over 365 days (135.6 s, 4,164 rows/s).

**The one real limit.** `implied_vol` exists only on `option_history_greeks_eod`, and *that*
endpoint genuinely enforces `"When expiration=*, you must request data a day-at-a-time"`.
Measured: neither `max_dte` (30/60/200) nor `strike_range` (5/20) nor both together lifts it at
any range — so it is a hard rule, not a cardinality cap. `max_dte` still cuts cost *within* a
day (2,178 → 1,594 rows; 1.49 s → 1.17 s).

### Revised projection

Per symbol-year, measured on AAPL — one of the heaviest chains listed, so a worst case:

| phase | endpoint | shape | cost |
|---|---|---|---|
| bars + bid/ask | `option_history_eod` | `exp=*`, 365-day | ~265 s |
| open interest | `option_history_open_interest` | `exp=*`, 365-day | ~136 s |
| implied volatility | `option_history_greeks_eod` | `exp=*`, **1 day × 252** | ~295 s |

≈ 78 min per symbol × 857 symbols ÷ 8 concurrent ≈ **~6 days** — and **~3.3 days** if vendor IV
is dropped in favour of inverting it ourselves (which needs a rate + dividend assumption we do
not currently make; see §1).

### What was actually wrong

1. **Wrong unit of work.** §2's "one (underlying, expiration) partition" is the shape the
   *library examples* suggest, not the cheapest shape the API serves. `exp="*"` collapses ~16
   live expirations per day into one call.
2. **Over-generalised one error.** `"day-at-a-time"` was observed on `greeks_eod` and assumed
   to hold for `option_history_eod`. It does not.
3. **Missed two server-side filters.** `max_dte` and `strike_range` are in the endpoint
   signature and were never tried.
4. **Measured the wrong ceiling.** "240–320 rows/sec per connection" was measured only on the
   per-expiry shape; the wide shape sustains ~2,950 rows/s on the same connection.

**Consequence:** do not send the drafted refund/quote request to ThetaData — its central claim
(a 150-day backfill) is not true of the product, only of our former request pattern.

### Server constraints hit along the way

| response | meaning |
|---|---|
| `INVALID_ARGUMENT: Too many days between start and end date; max 365 days allowed` | hard per-request span cap |
| `INVALID_ARGUMENT: When expiration=*, you must request data a day-at-a-time` | **`option_history_greeks_eod` ONLY.** `option_history_eod` and `option_history_open_interest` both accept `expiration="*"` over a 365-day range — see §4 |
| `PERMISSION_DENIED: Invalid permissions for date. Your first access date is: 20260827 for flat files` | flat files are a 7-day rolling window |
| `RESOURCE_EXHAUSTED: Too many concurrent requests` | above the tier's concurrency limit |
| `UNAUTHENTICATED: Invalid session ID… more than one terminal is running` | one authenticated session per API key |

### Next steps

1. **Rewrite `fetch_eod_bars` around the wide shape.** Today it groups by
   `(underlying, expiry)` and chunks to 30 days (`_REQUEST_WINDOW_DAYS`). It should issue one
   `exp="*"` call per underlying-year against `option_history_eod`, plus one for open interest,
   and fan the result out into the existing per-expiry parquet partitions — the on-disk layout
   does not need to change, only the fetch shape.
2. **Decide the IV question** (the only remaining day-at-a-time phase, ~40% of the cost):
   vendor `implied_vol` at ~6 days total, or self-inverted IV at ~3.3 days plus a rate and
   dividend assumption.
3. **The first-traded clamp becomes dead weight** under the wide shape — `option_list_dates`
   is one probe per (underlying, expiry) and the wide call has no per-expiry loop to clamp.
4. **Re-check the existing 5.8 GB.** It was written by the per-expiry path and predates the
   bid/ask schema fix, so it is both slow-won and missing quotes; refetching it under the wide
   shape costs hours, not weeks.
5. **Do not send the drafted ThetaData refund/quote request** — see §4.
