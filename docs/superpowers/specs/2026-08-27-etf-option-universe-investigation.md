# ETF Option Universe — Investigation Item

**Date raised:** 2026-08-27
**Status:** Candidate list expanded 2026-08-28 (see "Candidate list v2" below) — 100
FMP-screened, high-volume, sub-$150 candidates, TastyTrade-spot-checked where noted. Q1-Q4 (the
actual measurement/go-no-go pass) still not started.
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

---

## Candidate list v2 — FMP-screened, 2026-08-28

The hand-picked 13-name list above was chosen for regime diversity, not systematically screened.
This is a broader, mechanically-generated pool to draw from instead — 100 high-volume, sub-$150
ETFs, filtered for the same "leveraged/inverse is a trap" reasoning already established above.

**Method:**
1. FMP `/stock-screener` with `isEtf=true`, `priceLowerThan=150`, `volumeMoreThan=500000`,
   `isActivelyTrading=true`, exchanges NYSE/NASDAQ/AMEX → 486 raw results.
2. Excluded 148 leveraged/inverse/volatility products by name pattern (2x/3x/Ultra/Bull/Bear/
   Daily/Inverse/Short/Leveraged/Volatility/VIX) and by issuer (Direxion, GraniteShares,
   ProShares Ultra*) — the exact class the section above already documents as a GA trap (a
   leveraged product's structural decay looks like discovered edge and isn't one). 338 remain.
3. Sorted by volume, took the top 100.
4. Spot-checked TastyTrade option liquidity: constructed a near-the-money synthetic contract
   (`expiry_calendar`/`strike_ladder`, the same no-listing-endpoint approach
   `tools/warm_options_history.py --discovery synthetic` uses — see its own `--discovery` help
   text for why: a personal OAuth app's three scopes, read/trade/openid, cannot access
   `/instruments/equity-options` regardless of parameters) and fetched real bars for it.

**Caveat on the TastyTrade column below — read before trusting a "unchecked" row as a no.** The
spot-check used ONE contract per symbol with a short quiet-window, no retry, no batching. It threw
false negatives on names this doc already establishes as deeply liquid (TLT, XLF, HYG, SLV, GDX,
EEM, AGG, IEMG all came back `unresolved` — a stream timeout, not a confirmed-empty result — not
`empty`, which is what a genuinely nonexistent contract returns). So: **"confirmed" is a real
positive signal; "unchecked" means not-yet-verified, not "no options.·"** The real go/no-go per
symbol (Q2 above — final-day volume, not mere existence) still needs the batched/retried check
`fetch-options`/`warm_options_history.py` already does properly; that is the next step before
building a cache on any of these.

| # | symbol | price | volume | name | TastyTrade spot-check |
|---|---|---|---|---|---|
| 1 | BITO | $10.78 | 86,117,210 | ProShares Bitcoin ETF | confirmed |
| 2 | IBIT | $45.29 | 60,542,527 | iShares Bitcoin Trust ETF | unchecked |
| 3 | ETHA | $18.87 | 58,633,399 | iShares Ethereum Trust ETF | confirmed |
| 4 | HYG | $79.87 | 32,437,567 | iShares iBoxx $ High Yield Corporate Bond ETF | unchecked |
| 5 | XLE | $62.29 | 29,517,010 | State Street Energy Select Sector SPDR ETF | confirmed |
| 6 | XLF | $57.88 | 26,065,932 | State Street Financial Select Sector SPDR ETF | unchecked |
| 7 | SCHD | $34.83 | 25,676,777 | Schwab U.S. Dividend Equity ETF | confirmed |
| 8 | LQD | $106.73 | 21,807,117 | iShares iBoxx $ Investment Grade Corporate Bond ETF | unchecked |
| 9 | SGOV | $100.66 | 19,883,565 | iShares 0-3 Month Treasury Bond ETF | confirmed |
| 10 | TLT | $83.13 | 17,338,107 | iShares 20+ Year Treasury Bond ETF | unchecked |
| 11 | GDX | $103.69 | 16,790,483 | VanEck Gold Miners ETF | unchecked |
| 12 | EWZ | $35.76 | 16,589,895 | iShares MSCI Brazil ETF | unchecked |
| 13 | XLU | $43.18 | 16,005,821 | State Street Utilities Select Sector SPDR ETF | confirmed |
| 14 | SLV | $62.77 | 14,355,686 | iShares Silver Trust | unchecked |
| 15 | VGIT | $58.46 | 14,335,378 | Vanguard Intermediate-Term Treasury ETF | unchecked |
| 16 | FXI | $35.24 | 12,405,105 | iShares China Large-Cap ETF | unchecked |
| 17 | KRE | $74.35 | 10,751,179 | State Street SPDR S&P Regional Banking ETF | confirmed |
| 18 | EEM | $67.61 | 10,746,987 | iShares MSCI Emerging Markets ETF | unchecked |
| 19 | IEF | $93.23 | 10,552,537 | iShares 7-10 Year Treasury Bond ETF | confirmed |
| 20 | UNG | $10.43 | 10,360,838 | United States Natural Gas Fund LP | unchecked |
| 21 | KWEB | $26.10 | 10,068,778 | KraneShares CSI China Internet ETF | confirmed |
| 22 | VEA | $73.42 | 9,046,134 | Vanguard FTSE Developed Markets ETF | unchecked |
| 23 | EMB | $95.13 | 8,893,108 | iShares J.P. Morgan USD Emerging Markets Bond ETF | confirmed |
| 24 | BSOL | $15.03 | 8,605,732 | Bitwise Solana Staking ETF | unchecked |
| 25 | XLP | $85.08 | 8,487,958 | State Street Consumer Staples Select Sector SPDR ETF | confirmed |
| 26 | BIL | $91.63 | 7,714,377 | State Street SPDR Bloomberg 1-3 Month T-Bill ETF | unchecked |
| 27 | XLB | $53.23 | 7,591,921 | State Street Materials Select Sector SPDR ETF | confirmed |
| 28 | QYLD | $18.28 | 7,501,327 | Global X - Nasdaq 100 Covered Call ETF | unchecked |
| 29 | SCHG | $35.86 | 7,281,407 | Schwab U.S. Large-Cap Growth ETF | confirmed |
| 30 | JAAA | $50.70 | 7,036,866 | Janus Henderson AAA CLO ETF | unchecked |
| 31 | BND | $72.54 | 6,872,162 | Vanguard Total Bond Market ETF | unchecked |
| 32 | ICLN | $17.83 | 6,679,802 | iShares Global Clean Energy ETF | unchecked |
| 33 | AGG | $97.83 | 6,002,550 | iShares Core U.S. Aggregate Bond ETF | unchecked |
| 34 | SCHF | $28.42 | 6,000,138 | Schwab International Equity ETF | unchecked |
| 35 | SCHX | $30.41 | 5,962,902 | Schwab U.S. Large-Cap ETF | confirmed |
| 36 | BKLN | $20.53 | 5,832,717 | Invesco Senior Loan ETF | unchecked |
| 37 | IEMG | $82.42 | 5,733,207 | iShares Core MSCI Emerging Markets ETF | unchecked |
| 38 | SCHR | $24.45 | 5,646,792 | Schwab Intermediate-Term U.S. Treasury ETF | unchecked |
| 39 | SPYM | $90.75 | 5,438,302 | State Street SPDR Portfolio S&P 500 ETF | confirmed |
| 40 | EFA | $108.02 | 5,371,933 | iShares MSCI EAFE ETF | unchecked |
| 41 | GSOL | $8.31 | 5,312,989 | Grayscale Solana Staking ETF | unchecked |
| 42 | XLY | $115.88 | 5,220,236 | State Street Consumer Discretionary Select Sector SPDR ETF | unchecked |
| 43 | VCIT | $81.44 | 5,188,023 | Vanguard Intermediate-Term Corporate Bond ETF | unchecked |
| 44 | VTEB | $49.48 | 5,115,272 | Vanguard Tax-Exempt Bond ETF | unchecked |
| 45 | VGT | $121.91 | 5,047,615 | Vanguard Information Technology ETF | confirmed |
| 46 | SCHB | $29.81 | 5,018,689 | Schwab U.S. Broad Market ETF | unchecked |
| 47 | JEPI | $57.85 | 4,830,059 | JPMorgan Equity Premium Income ETF | unchecked |
| 48 | SPTL | $25.37 | 4,789,533 | State Street SPDR Portfolio Long Term Treasury ETF | unchecked |
| 49 | PYLD | $26.23 | 4,689,600 | PIMCO Multisector Bond Active Exchange-Traded Fund | unchecked |
| 50 | VUG | $88.90 | 4,682,892 | Vanguard Morningstar Growth ETF | unchecked |
| 51 | XLRE | $44.66 | 4,635,340 | State Street Real Estate Select Sector SPDR ETF | confirmed |
| 52 | VWO | $61.01 | 4,589,374 | Vanguard FTSE Emerging Markets ETF | unchecked |
| 53 | JEPQ | $60.31 | 4,562,136 | JPMorgan Nasdaq Equity Premium Income ETF | confirmed |
| 54 | VXUS | $87.93 | 4,516,345 | Vanguard Total International Stock ETF | unchecked |
| 55 | EWT | $108.63 | 4,419,938 | iShares MSCI Taiwan ETF | unchecked |
| 56 | XLC | $111.41 | 4,419,088 | State Street Communication Services Select Sector SPDR ETF | unchecked |
| 57 | SPHY | $23.36 | 4,377,105 | State Street SPDR Portfolio High Yield Bond ETF | unchecked |
| 58 | USFR | $50.34 | 4,374,174 | WisdomTree Floating Rate Treasury Fund | unchecked |
| 59 | ASHR | $34.56 | 4,323,142 | Xtrackers Harvest CSI 300 China A-Shares ETF | confirmed |
| 60 | IYR | $103.79 | 4,284,994 | iShares U.S. Real Estate ETF | unchecked |
| 61 | IAU | $86.62 | 4,235,603 | iShares Gold Trust | unchecked |
| 62 | IHI | $54.62 | 4,193,742 | iShares U.S. Medical Devices ETF | unchecked |
| 63 | GLDM | $91.17 | 4,002,972 | SPDR Gold MiniShares Trust | unchecked |
| 64 | SPIB | $33.19 | 3,936,126 | State Street SPDR Portfolio Intermediate Term Corporate Bond ETF | unchecked |
| 65 | BNDX | $47.70 | 3,867,068 | Vanguard Total International Bond ETF | unchecked |
| 66 | IJH | $76.65 | 3,851,149 | iShares Core S&P Mid-Cap ETF | unchecked |
| 67 | SCHH | $23.85 | 3,818,942 | Schwab U.S. REIT ETF | confirmed |
| 68 | IWF | $123.89 | 3,814,722 | iShares Russell 1000 Growth ETF | unchecked |
| 69 | SHY | $82.03 | 3,801,569 | iShares 1-3 Year Treasury Bond ETF | unchecked |
| 70 | EMLC | $25.77 | 3,788,472 | VanEck J.P. Morgan EM Local Currency Bond ETF | unchecked |
| 71 | MUB | $105.39 | 3,652,148 | iShares National Muni Bond ETF | confirmed |
| 72 | REET | $27.89 | 3,610,248 | iShares Global REIT ETF | unchecked |
| 73 | URA | $48.37 | 3,477,977 | Global X - Uranium ETF | confirmed |
| 74 | MSOS | $4.83 | 3,470,404 | AdvisorShares Pure US Cannabis ETF | unchecked |
| 75 | NVDY | $12.95 | 3,441,809 | YieldMax NVDA Option Income Strategy ETF | confirmed |
| 76 | GDXJ | $134.79 | 3,348,420 | VanEck Junior Gold Miners ETF | unchecked |
| 77 | VCLT | $72.62 | 3,328,327 | Vanguard Long-Term Corporate Bond ETF | unchecked |
| 78 | QQQI | $54.76 | 3,322,232 | NEOS Nasdaq-100 High Income ETF | unchecked |
| 79 | BINC | $52.07 | 3,211,819 | iShares Flexible Income Active ETF | unchecked |
| 80 | VEU | $85.81 | 3,171,750 | Vanguard FTSE All-World ex-US ETF | unchecked |
| 81 | PDBC | $18.42 | 3,133,989 | Invesco Optimum Yield Diversified Commodity Strategy No K-1 ETF | unchecked |
| 82 | ETH | $23.86 | 3,103,696 | Grayscale Ethereum Mini Trust ETF | unchecked |
| 83 | XRT | $86.59 | 3,088,539 | State Street SPDR S&P Retail ETF | confirmed |
| 84 | SCHE | $37.27 | 3,069,100 | Schwab Emerging Markets Equity ETF | unchecked |
| 85 | SPAB | $25.20 | 3,059,810 | State Street SPDR Portfolio Aggregate Bond ETF | unchecked |
| 86 | XRP | $16.31 | 3,051,599 | Bitwise XRP ETF | unchecked |
| 87 | SILJ | $33.16 | 3,040,220 | Amplify Junior Silver Miners ETF | confirmed |
| 88 | USO | $130.01 | 3,036,690 | United States Oil Fund, LP | unchecked |
| 89 | IBDV | $21.64 | 3,022,552 | iShares iBonds Dec 2030 Term Corporate ETF | unchecked |
| 90 | IUSB | $45.62 | 3,017,017 | iShares Core Universal USD Bond ETF | unchecked |
| 91 | DFAR | $26.29 | 2,885,885 | Dimensional US Real Estate ETF | unchecked |
| 92 | FNDX | $32.46 | 2,780,117 | Schwab Fundamental U.S. Large Company ETF | unchecked |
| 93 | BAI | $45.20 | 2,725,657 | iShares A.I. Innovation and Tech Active ETF | confirmed |
| 94 | SPTM | $93.54 | 2,721,798 | State Street SPDR Portfolio S&P 1500 Composite Stock Market ETF | unchecked |
| 95 | SCHP | $26.04 | 2,630,906 | Schwab US TIPS ETF | confirmed |
| 96 | CGDV | $50.45 | 2,606,873 | Capital Group Dividend Value ETF | unchecked |
| 97 | FNDF | $55.54 | 2,576,139 | Schwab Fundamental International Large Company Index ETF | unchecked |
| 98 | MSTY | $15.83 | 2,573,755 | YieldMax MSTR Option Income Strategy ETF | unchecked |
| 99 | ALLW | $30.38 | 2,547,483 | State Street Bridgewater All Weather ETF | unchecked |
| 100 | DBA | $28.82 | 2,498,720 | Invesco DB Agriculture Fund | unchecked |

87 of the 100 are under $100.

**Worth a second look before fetching, not excluded here:**
- **Single-crypto spot trusts** (BITO, IBIT, ETHA, BSOL, GSOL, ETH, XRP) — not leveraged (they
  track spot 1:1 or via futures), so they're left in as a genuine, different volatility driver
  per this doc's own "spanning different volatility drivers" goal — but they're a much newer,
  thinner options market than the equity ETFs above and deserve their own liquidity check.
- **Single-stock option-income funds** (NVDY, MSTY, QQQI, JEPI/JEPQ-style) — these are funds that
  themselves write options on one name or index; trading options ON the fund is a layered
  exposure, not a simple index play. Different animal from the rest of the list; flag for
  discussion before including.
- **Very low price** (MSOS $4.83, GSOL $8.31) — sub-$5-10 names often have coarse $0.50-$1
  strikes and thin books despite decent share volume; worth confirming before relying on them.

**Raw data:** the full 486-row FMP pull, the 338-row post-filter set, and the TastyTrade
spot-check output are not checked into the repo (scratch artifacts from this pass) — re-run the
FMP screen (`isEtf=true`, `priceLowerThan=150`, `volumeMoreThan=500000`) to regenerate if needed.
