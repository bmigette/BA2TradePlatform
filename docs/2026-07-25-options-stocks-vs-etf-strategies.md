# Option strategies: index/ETF vs single stocks — and why they need different experts

**Date:** 2026-07-25
**Short version:** the premium sold on an index is not the same premium as on a single stock.
It has a different economic source, different contract mechanics, and a different optimal
strategy shape. Our current options stack applies an *index-shaped* strategy to a
*single-name-only* universe, which is the wrong pairing on both halves.

---

## 1. It is not the same premium

The research report's entire case for premium selling rests on an **index** number: VIX 19.3%
vs realized 15.1% over 1990–2018, a **4.2-point** gross volatility risk premium. Every
empirical pillar in it — the CBOE PUT, BXM and WPUT indices, the Bondarenko study — is S&P 500
*index* options.

Measured on our own cache (25,466 ATM-IV vs subsequent-21-day-realized pairs, 91 single-name
underlyings, 2024-02 → 2026-07):

| Regime | n | ATM IV | realized vol that followed | VRP |
|---|---|---|---|---|
| risk_off + stressed | 2,615 | 39.7 | 41.9 | **−2.22** |
| risk_on + calm | 9,010 | 33.1 | 31.1 | +1.99 |
| risk_on + normal | 8,847 | 32.5 | 29.6 | +2.89 |
| risk_on + stressed | 4,910 | 33.1 | 31.1 | +1.94 |
| **ALL** | 25,466 | 33.6 | 31.7 | **+1.87** |

**+1.87 points, against the index's 4.2 — less than half.** And that +1.87 is fragile: it uses
close-to-close realized vol, and swapping to a Parkinson (high-low) estimator moves the overall
figure to +7.29 and *flips the sign* of the risk_off row from −2.22 to +4.70. A number whose
sign depends on the volatility estimator is not a number to build a strategy on.

The literature says the gap is structural, not incidental:

> "The difference between implied and realized volatility is greater between index options than
> between individual stock options. Realized variance is on average **higher** than implied
> variance for individual stocks, while the variance risk premium for the S&P500 itself is
> always positive and statistically significant."
> — Driessen, Maenhout & Vilkov, *Option-Implied Correlations and the Price of Correlation Risk*

The index premium is attributed *entirely to priced correlation risk* — implied correlation
~39.5% vs realized ~32.5%. **Selling index volatility is selling correlation. A single stock has
no correlation to sell.** That is why the index premium survives and the single-name one is
roughly zero or negative.

**Consequence:** a continuous premium-selling strategy on single names is harvesting the wrong
premium. It is not a weaker version of the index trade; it is a different trade with much less
evidence behind it.

---

## 2. The contracts themselves behave differently

| | SPX | SPY / QQQ / IWM | Single stocks |
|---|---|---|---|
| Exercise style | European | American | American |
| Settlement | **Cash** | Shares | Shares |
| Early assignment | **impossible** | yes | yes |
| Dividend assignment risk on short calls | none | yes | yes |
| Tax (US) | **Section 1256, 60/40** | standard short-term | standard short-term |
| Notional per contract | ~10× SPY | 1× | varies |
| Scheduled binary events | none | none | **earnings, every quarter** |
| Idiosyncratic jump risk | no | no | yes (M&A, guidance, FDA) |

Two of these change *engine* behaviour, not just returns:

- **Cash settlement removes an entire risk class.** SPX cannot be assigned early, so the
  assignment/dividend-risk paths our engine models simply do not apply. That is a real
  simplification, not a nuance.
- **Cash settlement also makes the Wheel impossible on SPX.** No shares are ever delivered, so
  there is nothing to write covered calls against. The Wheel structurally requires
  share-settled contracts — ETFs or single names only.

---

## 3. Which strategy belongs on which instrument

### Index / broad ETF (SPY, QQQ, IWM, SPX)
- **Systematic premium selling** — cash-secured puts / put-writing, iron condors, strangles.
  This is precisely what the CBOE PUT index *is*, and it is the only place our evidence base
  actually applies.
- **0DTE** (once intraday data exists — see the intraday roadmap). SPX/SPY dominate 0DTE volume.
- **Regime gating matters most here**, because the strategy is continuous and therefore always
  exposed. `risk_off` was the worst VRP environment under both estimators we tried.

### Single stocks
- **NOT continuous premium selling.** Per §1, the premium is not reliably there.
- **Event-driven vol selling around earnings.** This is where single-name premium concentrates:
  IV inflates ahead of a known binary event and collapses after it, and stocks move less than
  the implied move roughly 60–70% of the time. The strategy shape is completely different —
  *episodic*, calendar-triggered, 1–3 days of exposure rather than 30–45 DTE continuous.
- **The Wheel / covered calls**, where assignment is a *feature*: you want to own the stock and
  the premium lowers your basis. The edge is the ownership decision, not the vol premium.
- **Directional expression** (long calls/puts on a signal) — what OS1's price-target gates
  already do.

### A pointed irony in our current stack

`PremiumSeller` has `earnings_filter_enabled`, which **blocks** entries near earnings. For a
30–45 DTE index-style seller that is correct — earnings is uncompensated idiosyncratic risk.
But applied to a *single-name* universe it excludes the one window where single-name premium is
actually rich. **Same earnings-calendar data, opposite sign.** An `EarningsVolSeller` would
invert exactly that filter.

---

## 4. Recommendation: three experts, not one

Our current `PremiumSeller` is continuous, IV-rank-gated selling over a static universe of 98
single names — an index-shaped strategy on single-name instruments. Split it:

| Expert | Universe | Shape | Why |
|---|---|---|---|
| **IndexPremiumSeller** | SPY, QQQ, IWM (later SPX/XSP) | continuous, 30–45 DTE, defined risk, IV-rank + regime gated | the VRP evidence lives here |
| **EarningsVolSeller** | single names | episodic: enter 1–3 days pre-print, exit after | where single-name premium actually is |
| **WheelSeller** | single names / ETFs you want to own | CSP → assignment → covered call → called away | assignment is the point; needs share settlement |

`PremiumSeller` becomes `IndexPremiumSeller` with a changed universe rather than a rewrite — the
IV-rank gate, delta targeting, credit-multiple stops and roll-DTE logic all carry over. The
substantive changes are the universe and the regime gate.

---

## 5. What blocks this today

1. **We have no index or ETF option data.** All 98 cached underlyings are single names —
   `SPY`, `QQQ`, `IWM`, `SPX`, `XSP` are all absent. So `IndexPremiumSeller` cannot be
   backtested at all until the cache is extended.
2. **The Alpaca options-data credentials no longer authorize.** Both stored key pairs (test/trade
   `PK…` len-26, prod `PK…` len-20) return **HTTP 401** on the historical option-bars endpoint —
   including for `AAPL240719C00200000`, a contract we already hold 342k bars around. So this is
   not an ETF-entitlement issue; options-data access has lapsed or the keys were rotated.
   **User action required** before any options fetch can run.
3. The spread cost model shipped 2026-07-25 is still uncalibrated (see the intraday roadmap,
   Phase 1).

**Order of operations:** fix (2) → fetch SPY/QQQ/IWM → build `IndexPremiumSeller` → only then
compare structures. Choosing between iron condor / strangle / CSP is second-order while the
underlying instrument class lacks the premium.

---

## Sources

- Driessen, Maenhout & Vilkov, [*Option-Implied Correlations and the Price of Correlation Risk*](https://pages.stern.nyu.edu/~rengle/Maenhout-S2005.pdf)
- [The correlation risk premium — Macrosynergy](https://macrosynergy.com/research/the-correlation-risk-premium/)
- [Volatility Risk Premium Effect — Quantpedia](https://quantpedia.com/strategies/volatility-risk-premium-effect)
- [Dispersion Trading — Quantpedia](https://quantpedia.com/strategies/dispersion-trading) (the trade that *monetises* the index-vs-single-name gap directly)
- [Why Trade XSP vs. SPY? — Cboe](https://www.cboe.com/insights/posts/why-trade-xsp-vs-spy-a-breakdown-of-the-benefits)
- [SPX vs SPY Options: settlement, assignment, Section 1256 — SteadyOptions](https://steadyoptions.com/articles/spx-vs-spy-options-which-one-should-you-trade-2026-guide-r831/)
- [IV Crush Explained — SpotGamma](https://support.spotgamma.com/hc/en-us/articles/15249330755859-IV-Crush-Explained-What-It-Is-When-It-Happens-and-How-to-Trade-It)
- Internal: `docs/2026-07-25-options-strategies-automated-trading-research.md` (the source report),
  `docs/plans/2026-07-25-options-data-and-intraday-roadmap.md`
