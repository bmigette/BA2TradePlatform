# Option bar-sparsity probe (F19)

Measures the review's headline unknown: how often are daily bars missing or stale for a HELD
option contract, in the parquet store the backtest engine actually reads. Every miss flows
straight into marks, expiry settlement and liquidation through the paths F1/F2 describe.

Read-only against the main tree's cache: `C:\Users\basti\Documents\ba2\common\cache\
TastyTradeOptionsProvider` (TastyTrade daily option bars) and `...\FMPOHLCVProvider`
(underlying equity closes, for the moneyness split). Nothing in either cache, or in the main
tree, was written. Probe script and this report live only in this worktree
(`option-selection-modes`, on top of `36be07e0`).

## Method

**Sampling.** The store is one directory per underlying, one subdirectory per expiry
(`{SYMBOL}/exp={EXPIRY}/`), each carrying a `_manifest.json` (with a `status` field) and one
`{SYMBOL}_{EXPIRY}_1d.parquet` of daily OHLCV bars, one row per `(occ_symbol, bar_date)`.
Symbols were shuffled (seed `20260830`) and, per symbol, up to 24 of its expiries were sampled
at random and manifest-checked; a symbol's `status=="complete"` partitions were all kept. This
continued until at least 2,200 complete partitions were collected across at least 100 distinct
symbols, then stopped — so the sample is a random cross-section of symbols and expiries, not
the first N alphabetically or the most/least liquid names.

Result: **2,203 partitions** (`status=="complete"`) read across **184 distinct underlyings**,
all 2,203 read without error. That covers **76,203 individual contracts** (unique `occ_symbol`
values across the sampled partitions).

**Interior gaps.** For each contract, take its distinct printed `bar_date`s. For any contract
with 2 or more of them, `span = numpy.busday_count(first, last+1day)` — Mon-Fri business days
in `[first_bar, last_bar]` inclusive, no holiday calendar — and
`interior_gap_rate = 1 - (distinct_bar_dates / span)`, floored at 0. This asks: of the business
days the contract was plausibly open for trading between its own first and last print, what
fraction printed no bar at all. 67,510 of the 76,203 contracts had 2+ bars and are counted here;
the remaining 8,693 printed exactly one bar (or none — see manifest note below) and have no
interior span to measure.

**Tail gaps** (the held-to-expiry hazard). For every contract with at least one bar,
`tail_gap_days = numpy.busday_count(last_bar+1day, expiry+1day)` — business days strictly
between the last print and the contract's own expiry — flagged as a tail gap when `>= 5`. This
is independent of the interior measure and applies to all 76,203 contracts, including the
single-bar ones (a contract that printed once and never again is the worst case this asks
about, not an excluded one).

**Moneyness.** For each contract, the underlying's own close nearest-on-or-before the
contract's *last* printed bar date (looked up in `FMPOHLCVProvider/{SYMBOL}_1d.parquet`, which
covers 2011-06 through the store's own end date and resolved for every sampled underlying that
had a file) stands in for spot at the moment the contract's data went quiet. Bucketed by percent
in/out of the money at that spot: `ITM` at `>=5%` in the money, `OTM` at `>=5%` out, `ATM`
otherwise. 3,348 of 76,203 contracts (4.4%) had no resolvable underlying close and are excluded
from the moneyness split only; they are included in every overall number above.

**Caveats, stated once rather than in every number below.** (1) No holiday calendar: a handful
of NYSE closures per year count as "expected" business days in both the interior-span and
tail-gap denominators, so every gap-rate and tail-gap-days figure below is a small
*overstatement* of the true rate against actual trading days — on the order of a few percent
relative for a multi-month contract, more for a short-dated one that happens to span a holiday.
(2) The moneyness spot is a same-or-earlier close, not a same-bar snapshot; on a stale contract
this can be several days removed from the actual last-bar day, so the ITM/ATM/OTM split is a
useful signal of direction, not a precise cut. (3) This measures **printed vs. business days**,
not printed vs. "the exchange was actually open for this specific contract" — a contract that
was validly untradeable for a stretch (e.g. never listed until later, despite the manifest's
nominal `start`) would show as a gap here exactly as a genuinely-missed print would; the
resulting proxy is a defensible upper bound on "days the strategy would have had no fresh mark
if it were holding this contract," which is precisely the quantity F1/F2 care about.

## Numbers

```
Partitions sampled (status=complete): 2,203
Distinct underlyings: 184
Total contracts examined: 76,203
Contracts with >=2 bars (interior-gap eligible): 67,510

INTERIOR GAP RATE (overall, n=67,510):
  mean=46.18%  median=47.06%  p90=86.00%  p99=96.62%

TAIL GAP (last bar >=5 business days before expiry): 20,267 / 76,203 = 26.60%
Tail gap days: median=0  p90=19  p99=117  max=611

-- by moneyness (spot at contract's last printed bar) --
ITM: interior_gap n=23,003  mean=53.30%  median=58.82%  p90=90.00%  p99=97.50%
     tail_gap 7,701/27,316 = 28.19%
ATM: interior_gap n=11,082  mean=32.96%  median=29.03%  p90=70.00%  p99=89.52%
     tail_gap   604/11,348 =  5.32%
OTM: interior_gap n=30,431  mean=45.62%  median=46.15%  p90=84.88%  p99=96.00%
     tail_gap 11,456/34,191 = 33.51%

(contracts with no resolvable underlying spot: 3,348 of 76,203)
```

**Spot-checked for sanity**, not just trusted: `AAPL 2023-01-06P125` (a 4-business-day
contract) prints all 4 days, gap 0% — the algorithm does not manufacture gaps where none
exist. `AAPL 2024-12-06C175` (26-business-day span) prints 13 of them — bars on 2024-11-01,
11-04, 11-06, then silent for 9 business days to 11-15, silent again for 10 more to 11-25, then
active daily through expiry on 12-06 — a real, eyeballable 50% interior-gap contract, matching
the computed figure exactly.

## Read for F1/F2

The headline number is not the ~46% median interior-gap rate alone — it is that the ITM bucket
is the *worst* on both axes (59% median interior gap, 28% tail-gap rate) while carrying the
*largest* dollar exposure per contract. F1's unclamped expiry settlement and F2's
entry-premium-fallback both fire specifically on stale or absent bars, and this probe shows
those are not rare edge cases: a held ITM contract is missing a fresh print on a majority of the
business days it is open, and better than one in four ITM contracts go dark 5+ business days
before their own expiry — with the tail of that distribution (p99 = 117 business days, max =
611) showing contracts that effectively stopped printing months to years before expiring, so
whatever price F1 settles them at, or F2 marks them at, is not a stale-by-a-day rounding error
but frequently a snapshot from a wholly different regime of the underlying. F2's label in the
findings doc — "VERIFIED path / SUSPECT frequency" — should be read as VERIFIED on both counts:
the mechanism is real and the frequency is high, concentrated exactly where the dollar exposure
is largest.

## Reproduction

Probe script (read-only, not committed — regenerate on demand):
`f19_probe.py`, seed `20260830`, run against the paths above with the same
`.venv\Scripts\python.exe` used for the package test suite. No files in either cache were
modified; only local stdout was captured for the numbers in this report.
