## 1. REAL OR FITTED?

**Verdict: there are credible strategy families here, but no demonstrated out-of-sample edge yet.** The strongest candidates are **small-cap Insider S1, mid-cap EarningsDrift S1, and selected DeterministicScorer S2/S6 jobs**. Large-cap FactorRanker has unusually broad P&L, although its return is less compelling. Several insider and rating variants look like searches that found a few exceptional winners.

Below, job IDs identify the exact jobs in your JSON; `R1` means `TOP1`.

### Strongest evidence worth taking to the holdout

| Jobs and strategy | What the data shows | Judgment |
|---|---|---|
| **414 / 447 — small Insider S1, ATR / notional** | Top-five CAR **16.79–27.41%**, Sharpe **1.83–2.17**, Calmar **1.65–4.12**, **206–299 trades**. Four of five genomes have top-five P&L concentration only **16.7–20.5%**. | **Strongest insider evidence.** Fitness falls from 13.16 to 5.95, but economics remain strong throughout. This is a counterexample to treating a fitness spike alone as proof of overfit. |
| **370 / 424 — mid EarningsDrift S1, ATR / notional** | Job 370 fitness **7.23–8.29**, CAR **16.38–28.38%**, **562–803 trades**. R1: Sharpe **2.10**, top-five concentration **14.5%**. Job 424 R1: CAR **24.87%**, Sharpe **1.93**, concentration **15.6%**. | **Strongest earnings evidence.** The winning genomes are materially better than the remaining plateau, but the plateau itself is profitable. |
| **375 — large DeterministicScorer S6 ATR** | Fitness **5.83–7.80**; all five CAR **14.21–31.23%**. R1/R2: CAR **24.49/20.51%**, DD **−15.59/−13.90%**, **451/514 trades**, top-five concentration **20.9/13.6%**. | **Credible family**, particularly R1/R2. R5’s 31.23% CAR is less attractive: DD −26.67%, concentration 40.2%. |
| **389 — mid DeterministicScorer S6 ATR** | Top three CAR **20.13–20.66%**, Sharpe **1.61–1.63**. All five Calmar **1.52–1.71**, CAR **13.95–20.66%**. | **Credible, moderately concentrated.** Top-five concentration **36.4–43.7%** warrants more caution than job 375. |
| **360 — small DeterministicScorer S2 notional** | All five CAR **10.21–12.87%**, **335–433 trades**, PF **1.74–2.17**, top-five concentration **21.8–28.6%**. | **Good economic plateau**, not merely one spectacular genome. |
| **362 — large FactorRanker ATR** | Fitness **4.47–4.89**, CAR **11.33–12.67%**, **1,109–1,293 trades**, top-five concentration **13.8–16.9%**. | **Broadest, cleanest plateau**, but not established alpha: benchmark exposure and transaction costs could explain or consume much of it. |
| **430 — mid Insider S1 notional** | CAR **13.14–15.79%**, **168–270 trades**, top-five concentration **21.7–28.0%**, PF **1.92–2.83**. | **Credible secondary insider candidate**, stronger breadth than mid Insider S1 ATR, job 377. |
| **390 / 358 — large Rating S1, notional / ATR** | Job 390 CAR **12.09–17.49%**, Calmar **1.37–1.77**; job 358 fitness tightly grouped at **5.26–5.55**. Mostly moderate concentration. | **Worth validating**, but only approximately four years of effective rating history, not six. |

Additional supporting—not decisive—evidence:

- **334/335/350:** large DeterministicScorer S1/S2 show profitable alternatives with hundreds of trades.
- **341/353:** mid DeterministicScorer S2 works under both sizing searches. Job 341’s first four CARs are **14.74–15.34%**, with top-five concentration **23.8–25.1%**.
- **441:** small EarningsDrift S2 notional: R1/R2 CAR **15.36/13.67%**, concentration **24.9/26.8%**.
- **386/437:** small EarningsDrift S1 has fairly stable CAR across five genomes, although concentration is usually **31–43%**.
- **359/396:** large Rating S2 has strong reported ratios, but only **108–195 trades**, few winning trades, and approximately **38–47%** top-five concentration.

### Results I would treat as fitted or economically unconvincing

- **379/431 — mid Insider S2; 380/432 — mid Insider S3:** top-five concentration generally **67–97%**. PF as high as **13.66** in 379 R2 does not rescue a **13.0% CAR / −19.73% DD** result dependent on five winners.
- **384/435 — small Rating S2; 385/436 — small Rating S3:** extreme concentration, unstable economics, and in job 436 profits that disappear when a single trade is removed.
- **382/422 — mid/small FactorRanker:** weak CAR plus heavy concentration. R1 CAR is **4.43%/2.12%**, with top-five shares **59.8%/61.0%**. **No persuasive edge here.**
- **348/363 — small DeterministicScorer S3:** the attractive CAR genomes, R2–R4, depend on the best five trades for **66.4–71.8%** of net P&L.
- **411/443 — small EarningsDrift S3:** R1 fitness **4.34**, versus R2 **1.54** and remaining scores mostly below **0.65**. Lower ranks generally have concentration above **53%**. Much weaker robustness than EarningsDrift S1/S2.
- **417 — small Insider S2:** no compelling economics even after optimization: Calmar **0.32–0.49** across the five.
- **433 — small Rating S1 notional:** potentially interesting signal, but **do not trust the headline 36.99% CAR**. R3 has **65%** of net P&L in five trades; fitness falls from **6.77** at R1 to **2.76** at R5.

Two important qualifications:

1. **Five selected survivors cannot establish a broad parameter plateau.** They may be near-clones or isolated profitable points. Genome distances, unselected results, and neighborhood perturbations are missing.
2. **Sizing repetitions are not independent replications.** Entire five-row economic result sets match for **342/354, 348/363, 361/398, 366/402, and 425/439**. Jobs **414/447** are almost identical. That is not evidence that two independent implementations confirmed the edge; the reason for the matches is not supplied.

Also, **347 R1** illustrates why fitness dispersion and CAR dispersion must be separated: its fitness is only **0.7% above R2**, but CAR is **22.31% versus 9.69%**, with DD **−23.28% versus −7.69%**. Similar fitness does not mean similar economic behavior.

---

## 2. FITNESS VALIDITY

### Ranking disagreement

I count a disagreement whenever another persisted genome has strictly higher CAR, less-negative DD, or higher CAR/|DD|. Ties count as rank-1 being joint-best.

| Metric | Jobs where R1 is not best | Percentage |
|---|---:|---:|
| CAR | **44 / 79** | **55.7%** |
| Drawdown | **61 / 79** | **77.2%** |
| Calmar | **42 / 79** | **53.2%** |

**Job 448 is missing TOP2**, explaining 394 rather than 395 rows. These are comparisons against available genomes. Excluding that incomplete job gives **44/78, 60/78, and 41/78**, respectively.

### Is that economically wrong?

**Not maximizing CAR or minimizing DD is entirely reasonable. Not maximizing Calmar is also permissible—but the score needs an explicit economic justification.**

Examples where the ranking is sensible
