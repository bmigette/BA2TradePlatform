# Option stage 1: HOLD versus low-confidence entries

Decision: September 20, 2026. Explore both separately for DeterministicScorer.

DeterministicScorer emits HOLD between its buy/sell score thresholds. With the
defaults, +0.10 means HOLD, +0.35 means BUY at 35% confidence and -0.25 means SELL
at 25%. Low-confidence directional signals and HOLD therefore select different
observations. The optimized score thresholds affect how many signals each arm gets.

## Two explicit arms

| Arm | Eligible signal | Mandatory entry gate | Searched parameters |
|---|---|---|---|
| `hold` | HOLD only | `current_rating_neutral` | Expert, structure, other entry/exit gates and market conditions |
| `low_confidence` | BUY/SELL, including OVERWEIGHT/UNDERWEIGHT | confidence <= optimized threshold | Confidence threshold plus the same other parameter families |

Each arm runs independently for O_STRD (long straddle), O_STRG (long strangle) and
O_IC (iron condor). A long volatility structure wants movement, while an iron
condor benefits from a contained range: HOLD alone is not a volatility forecast.
The market-condition and structure genes still have to establish whether either
entry population helps each structure. No profitability claim follows from the gate.

The defining gate cannot be toggled off by the GA. The HOLD arm removes both the
low-confidence gate and expected-profit gate: HOLD has no directional price target.
The low-confidence arm removes the conflicting HOLD flag. Its confidence cutoff
retains the existing expert-specific search bounds.

## Live/backtest contract

`neutral_option_entry_mode` is an optional expert setting. Missing or null means
`legacy`; existing results and live instances keep their prior behavior. Unknown
non-null values fail closed. The two new modes opt into identical signal eligibility
in live TradeManager and the daily backtest engine. Only straddle, strangle and
iron-condor entry actions are allowed; equity/mixed action lists are rejected.

Option actions use the shared option sizing/submission path and its existing
capacity guards. Automated-opening and duplicate-position guards remain active.
The live path selects the newest recommendation before testing the mode, avoiding
resurrection of an older HOLD after a newer BUY. HOLD does not become an equity BUY.

The setting travels in the optimization's base expert settings, through trial
configuration and the saved Top-N `expertFixedSettings`, then through deploy export
and the existing importer. No schema migration or live-account activation is needed.

## Driver and cache

Add `--neutral-entry-modes hold,low_confidence` to `tools/stage1_run.sh` (forwarded to
the discovery driver). This produces 19 jobs per expert: 13 unchanged structures
plus 6 neutral experiments. Without the flag the existing 16-job plan remains.
The launcher also accepts `--neutral-entry-mode hold` or `low_confidence` for a
single O_STRD/O_STRG/O_IC optimization.

Neutral arm names include their mode and a configuration digest. Their Top-N
backtests inherit those names. Other job identities do not change, permitting
checkpoint continuation. Do not reuse an earlier neutral experiment name manually.

These modes need no additional provider data. Reuse the warmed ThetaData and OHLCV
caches and the pinned `ohlcv-v1` / `ta-structure-v1` manifests. The existing mandatory
verify/prepare-host checks still run before launch. No fetch-on-miss is introduced.

Compare within the same window, capital policy and fitness objective. Keep 2026
as an untouched holdout. Review return, CAR, drawdown, trade count, concentration
and yearly stability; a winning in-sample score alone does not establish an edge.
