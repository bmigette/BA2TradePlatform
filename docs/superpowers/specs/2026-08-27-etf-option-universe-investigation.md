# ETF Option Universe — Investigation Item

**Date raised:** 2026-08-27
**Status:** Not started. Scoped question, not a design.
**Blocks:** the 1DTE grid variant (see below). Does NOT block the main option grid, which runs on
large-cap equities.

---

## Why this is an investigation and not a ticket

The 1DTE grid needs ETFs, and ETFs are not free to add. Three separate obstacles, each measured:

**1. ETFs are absent from the screener metric store, by construction.** `metric_store.py:255-261`
builds with `isEtf=false&isFund=false`; SPY, QQQ, IWM, XLF and TLT were all verified absent from
the local store. So no ETF can arrive through a screener cap band — it must come through an
explicit `--universe` list. That is workable (`optimize --universe` is a plain symbol list) but it
means ETFs can never participate in the screener-gated arms of any grid.

**2. Their option history is not in the cache, and an ETF is not a marginal add.** The repo's own
measurement, in `fetch_options.py:322-324`: *"a 2.4-year SPY window is on the order of 100k
contracts (~250 expiries x ~200 strikes x 2 rights)"*, against *"a couple of thousand"* for a
single name. **Roughly 50x a stock.** Adding five index ETFs is not adding five symbols; it is
plausibly doubling the cache. Fetch time, disk, and every per-bar chain scan pay for it.

**3. The thing that makes ETFs worth having may not be in the cache at all.** SPY, QQQ and IWM are
the only ETFs with genuine DAILY expiries, and daily expiries are the entire reason to want them
for a short-dated grid. Whether the cache actually holds those daily expiries — as opposed to
Fridays only — is unverified. If it holds Fridays only, the case for paying the 50x collapses to
"deeper Friday books", which is a much weaker argument.

---

## Questions to answer, in the order that can kill the work cheapest

**Q1. Does the cache hold daily expiries for SPY/QQQ/IWM, or only Fridays?**
Cheapest to answer and potentially decisive. Query the existing options cache for distinct
expiries per underlying and check the weekday distribution. If Fridays only, Q2-Q4 change
character entirely and the 1DTE grid becomes a Thursday-only exercise on equities as well.

**Q2. Which ETFs have real FINAL-DAY option volume?**
Not general option volume — final-session volume, which is a much smaller set. This is the binding
constraint: the fill engine caps an order at ~10% of a bar's volume, and
`option_selector.passes_liquidity` rejects a contract below `min_volume`. Measure the volume
distribution on expiry-day bars specifically. Expect the answer to be "SPY, QQQ, IWM and almost
nothing else", which is itself the finding.

**Q3. What does each ETF actually cost to cache?**
Contracts x days, disk, and fetch wall-clock, measured per symbol rather than extrapolated from
SPY. Sector SPDRs have far fewer strikes than SPY and may be cheap; the 50x figure is SPY's, not
an ETF constant.

**Q4. Does adding ETFs slow every OTHER run?**
The per-bar chain fetch scans the cache. A cache that has doubled may slow equity grids that never
touch an ETF. Measure before and after on a fixed equity backtest.

---

## Candidate list to evaluate

Chosen for option volume and for spanning different volatility drivers — the point is to let the
GA distinguish regimes, not to hand it fifteen correlated copies of the S&P.

| group | names | rationale |
|---|---|---|
| broad index | SPY, QQQ, IWM | deepest option books in existence; the only ETFs with daily expiries. IWM (~\$220) also stays usable if premium-selling structures are added later |
| sector | XLF, XLE, XLK, XLV | sector IV is not damped by the correlation that suppresses index IV, so these give real dispersion |
| rates / credit | TLT, HYG | a genuinely different driver; TLT's option book is deeper than expected and behaves nothing like equity |
| commodity | GLD (SLV secondary) | uncorrelated regime |
| high-IV thematic | SMH | debit structures need movement to pay for premium; low-IV names starve them |

### Exclude deliberately, and this matters more than the include list

**Leveraged and volatility ETFs — TQQQ, SQQQ, SOXL, UVXY, VXX, SVXY.** Their options are liquid,
which is precisely the trap. The underlyings carry structural decay and roll drag; UVXY loses on
the order of 90% a year by construction. A GA will discover "buy puts on UVXY", it will look
spectacular in sample, and it is not a strategy — it is a known structural fact that any options
grid will happily rediscover and present as edge. Left in the universe they would plausibly
dominate the results and make the grid worse than useless, because the winner would look
convincing.

**Thin sector books — XLB, XLRE, XLP.** They have options; they do not have final-day volume.
They would be rejected by the volume gate and spend trials doing it.

---

## Deliverable

1. A **measured** table: per candidate ETF, final-day option volume distribution, contract count
   for the intended window, cache cost in MB, and fetch wall-clock.
2. A go/no-go per symbol against a stated volume floor.
3. A recorded decision on the daily-expiry question (Q1), because it determines whether the 1DTE
   grid is "any weekday on three index ETFs" or "Thursdays only, everywhere".
4. If go: the `fetch-options` invocations to build the cache, and the explicit `--universe` string
   for the grid.

## Why this is not urgent

The main option grid (18 structures, large-cap equities) needs none of this — it runs on names
already in the screener store and the equity cache. This investigation gates only the **1DTE
variant**, which is a follow-on grid. Do it when that variant is next in line, not before, and do
Q1 first because it is the cheapest question that can change the answer to all the others.
