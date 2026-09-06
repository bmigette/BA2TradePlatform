# Code and strategy audit — 2026-09-06

## Assessment

**The inspected implementation has material defects in ML evaluation and risk sizing.** In particular, directional labels reach the classifier as input features, and the classic risk manager wires the dedicated ATR risk budget into stop distance instead of sizing. Do not interpret affected model scores or strategy rankings as validated performance until these paths are corrected and rerun.

This is a targeted source audit, not an exhaustive certification of the monorepo or a profitability assessment. No production account, stored strategy configuration, trading database, or historical performance dataset was inspected. No orders, application startup, deployment, source fixes, commits, or pushes were performed.

- Revision: `6d08a34e74b5363ef922ee13283ef0b6c7557a9b`.
- Working tree: pre-existing untracked audit files, logs and other artifacts were left untouched. Findings below were independently checked against source, not copied from those audits.
- Reviewed areas: shared position sizing and classic risk management; live and backtest entry submission; ML target construction, normalization and sequencing; selected strategy fitness and expert scoring/portfolio code; test collection and parity workflow.
- Depth: detailed on the findings below; sampled on experts and execution. Smart RM, all broker implementations, authentication, frontend security, option lifecycle, and every expert were not exhaustively audited.
- Severity: P1 = material risk or invalid results; P2 = meaningful correctness/validation gap requiring a fix. Strategy design observations are explicitly separate from confirmed defects.

## Findings

### F1 — P1: Classification input contains the future-derived answer

**Evidence:** `testplatform/backend/app/services/job_handler.py:1415`, `:1622`, `:1705`; `testplatform/backend/app/services/tsai_training.py:312` and `:315`.

Directional targets are created as `direction_up_<horizon>bar` / `direction_down_<horizon>bar`. Feature selection excludes OHLCV, Date, ticker and names starting with `price_`, but does not exclude these directional targets or the full `all_target_columns` collection. The resulting feature list is passed into classification preparation. The caller supplies `prediction_horizon=0` because labels are already shifted (`job_handler.py:2338`). Consequently the final input timestep includes the exact label being predicted.

**Reproduction:** The companion probe executes the source feature-selection statements and sequence builder. For a directional target it confirms `X[:, label_feature, -1] == y` for every generated sample.

**Impact:** Training and held-out evaluation can learn an unavailable future outcome. Scores cannot establish forecasting skill, and inference cannot produce that feature causally. Other generated target families need the same exclusion audit.

**Fix:** Derive features from a positive list of causal input columns; explicitly exclude the selected target and every generated target. Add an end-to-end test asserting that target columns never occur in model inputs or saved inference metadata.

### F2 — P1: Dedicated risk budget controls stops; sizing still uses the old setting

**Evidence:** `packages/common/ba2_common/core/TradeRiskManagement.py:1202`, `:1235`, `:1255`, `:1274`; intended contract at `packages/common/ba2_common/core/interfaces/MarketExpertInterface.py:156`.

`_ensure_safeguard_stop` reads `atr_risk_budget_pct` and passes it to stop synthesis. `_risk_atr_quantity` instead reads `risk_per_trade_pct` as the dollar-risk budget. This reverses the documented separation: dedicated budget for size, existing percentage for stop distance. Since safeguard creation also applies to notional sizing, the budget can unexpectedly change notional-mode stops.

**Reproduction:** Execute the actual two methods with equity $100,000, entry $100, dedicated budget 1%, old setting 8%, minimum stop distance 7%, ATR disabled and $30,000 position cap. Result: stop $93, 300 shares, $2,100 loss at that stop versus a configured $1,000 budget. Under the documented separation, the stop is $92 and 125 shares use $1,000 risk. These are arithmetic exposures, before gaps, fees or fill effects.

**Impact:** A dedicated budget is not enforced as advertised; strategy genes have incorrect meanings and risk-based sizing can collapse onto the notional cap.

**Fix:** Read the dedicated budget in `_risk_atr_quantity`, falling back only when it is absent. Keep stop-distance settings in `_ensure_safeguard_stop`. Test the real methods together for both sizing modes, explicit stops, cap binding and regime scaling. Rerun affected optimizations after correction.

**Coverage gap:** `packages/common/tests/test_atr_risk_budget_decoupling.py:93` counts readers but does not check which function owns the reader. Its stop-synthesis check at `:117` checks text near the call, not the origin of `risk_pct`. Several tests reproduce the intended formula independently of the actual risk manager; they do not establish correct wiring.

### F3 — P1: Normalization learns from held-out data

**Evidence:** `testplatform/backend/app/services/tsai_training.py:190–205`, `:233–249`; `testplatform/backend/app/services/darts_training.py:124–125`, `:155–156`, `:193–204`.

Both tsai split methods fit normalization before splitting, including future test rows. Darts `prepare_data_split` calls `prepare_data` on the full series; that method fits target and covariate scalers before the split. The tsai comment explicitly describes fitting on full data to keep test values within range, contrary to its train-only docstring.

**Impact:** Validation is not strictly out of sample: future extrema and feature availability influence training transforms. This does not guarantee that every score improves, but it invalidates the intended independence and changes behavior relative to causal deployment.

**Fix:** Split chronologically first, fit transforms on training rows only, and persist/reuse them for validation and inference. For multiple datasets, combine only training portions when fitting. Test that changing test-only extrema leaves training arrays and scaler parameters unchanged.

**Additional scoped concern:** Darts `prepare_data` always refits `self.scaler`. The older per-model path calls it separately for train and test (`job_handler.py:1906–1917`), replacing the training scaler and using different scale systems. The currently inspected main route uses unified optimization; reachability of the older path was not established, so this is not counted as a separate active defect.

### F4 — P1: Pre-shifted training labels cross the chronological split

**Evidence:** `testplatform/backend/app/services/job_handler.py:1415–1417`, `:1593–1594`, `:2334–2345`; `testplatform/backend/app/services/darts_models.py:1567–1577`; `testplatform/backend/app/services/tsai_training.py:199–212`.

Future-derived targets are calculated over the combined frame before splitting. Both the ordinary splitter and tsai preparation cut rows at the split index without removing training examples whose outcome window reaches the validation period. Passing horizon zero to sequencing prevents double-shifting but also leaves no horizon-based purge there.

**Reproduction:** With 12 rows, an 80% split occurs at row 9. A two-bar label at training row 8 depends on row 10 in the validation portion. The probe confirms that boundary crossing. This remains a defect after removing labels from input features.

**Impact:** Training consumes validation-period outcomes. Longer horizons contaminate more boundary examples.

**Fix:** Carry each label's outcome-end timestamp and exclude training examples whose outcomes are not fully known before the validation boundary. Apply the same rule within every optimization fold, respecting per-symbol and multi-timeframe boundaries.

### F5 — P2: Unobservable directional outcomes are stored as negative examples

**Evidence:** `testplatform/backend/app/services/job_handler.py:1415–1417`; `testplatform/backend/app/services/tsai_training.py:119`, `:315`.

For the final `horizon` rows, `Close.shift(-horizon)` is missing. Comparing it with Close yields false, then `.astype(int)` turns unknown outcomes into zero labels. These zeros are ordinary valid integers and survive the inspected classification preparation.

**Reproduction:** The final two labels for a two-bar horizon are `[0, 0]`, although neither future price is present.

**Impact:** Label distributions and validation metrics include fabricated negatives. Bias is especially significant for short datasets or long horizons.

**Fix:** Preserve an outcome-availability mask, leave unknown labels missing and remove them before training/evaluation. Assert that every retained target has a complete future observation window.

### F6 — P2: Backtest and live entry submission implement different protective-stop policies

**Evidence:** `packages/common/ba2_common/core/position_sizing.py:261`; `testplatform/backend/app/services/backtest/daily_engine.py:1664–1669`, `:1718`; `ba2_trade_platform/core/TradeManager.py:2022–2027`.

The backtest reconciles transaction ruleset stops with RM safeguards using the tighter stop. Live submission passes `fo.stop_price` as its safeguard without the same reconciliation call. The shared helper explicitly documents that this policy difference is intentional and live does not call it.

**Trigger:** A ruleset bracket stop and safeguard both exist and differ. For example, a long ruleset stop at $95 and safeguard at $90 produce a reconciled backtest stop of $95; live passes $90 at the submission boundary. Whether existing dependent legs ultimately offset that difference depends on the account implementation and was not established here.

**Impact:** Shared sizing alone does not prove execution parity. Stop selection, dependent orders and resulting exits may differ precisely when two protection mechanisms interact.

**Fix:** Establish one explicit policy and test both full submission paths through order creation and fill, including long/short and conflicting stops. Update golden fixtures to cover this conflict before changing production behavior.

### F7 — P2: Pure sizing helper treats zero caps and negative cash as no restriction

**Evidence:** `packages/common/ba2_common/core/position_sizing.py:130–143`.

The notional cap is applied only when it is truthy and positive; cash is applied only when nonnegative. Passing `max_position_value=0` or negative cash therefore skips the respective constraint instead of producing zero quantity or rejecting invalid inputs.

**Reproduction:** Equity $100,000, price $100, risk 1%, stop $95, notional cap zero and cash -$1 returns 200 shares.

**Impact and limit:** This is a confirmed helper-contract defect. Caller checks may prevent these values in some live paths; this audit did not demonstrate a broker submission exploiting it. It is not evidence that current live trading necessarily exceeds a zero allocation.

**Fix:** Distinguish `None` (no constraint) from zero (no allocation), reject negative caps/cash or explicitly produce zero, and validate finite numeric inputs. Cover these boundary values in the shared helper suite.

## Strategy design assessment

These observations describe code behavior and validation needs. They are not claims of measured investment performance.

1. **Equity CAR does not impose a maximum drawdown.** In `testplatform/backend/app/services/strategy_fitness.py:1053–1054`, the multiplier is `min(20 / max(abs(drawdown), 1), 2)`. Holding other factors constant, base return 7.5 with 5% drawdown contributes 15; base 15 with 10% drawdown contributes 30; base 30 with 20% drawdown also contributes 30. Proportional exposure increases can therefore improve or preserve fitness despite greater drawdown. This is an objective choice rather than an implementation error. If a hard drawdown ceiling is intended, it needs a separate eligibility rule. The option metric has a different penalty; scores across these objectives are not comparable.

2. **Expert confidence is a heuristic score, not demonstrated probability.** `DeterministicScorer/combine.py:258–260` maps absolute score directly onto confidence. `FMPSenateTraderCopy.py:617` derives confidence from a percentage of a trader's yearly activity. A threshold such as 80 in `rules/optimized_profit_strategy.json` therefore has different meanings across experts. Validate calibration by expert and horizon before interpreting equal confidence values as equal reliability.

3. **The supplied “optimized” ruleset name is not evidence of optimization quality.** The JSON declares entry thresholds, take-profit and stop adjustments, but this audit did not locate or validate the dataset/results used to justify that specific artifact. Evaluate net realized reward/risk after target adjustment, fees and stop execution; an expert's raw target percentage is not the trade's realized return.

4. **Selection performance needs an untouched evaluation period.** The strategy optimizer scores repeatedly through `strategy_optimization_handler.py:1065` and related trial machinery. Reusing those evaluated periods to choose the winner makes them selection data. This audit did not verify a complete independent holdout protocol across external grid orchestration. Require an untouched final interval, horizon-aware folds, a baseline and parameter-stability checks before treating a winning genome as generalizable.

5. **Execution realism remains a configuration-dependent question.** `backtest/backtest_account.py` includes commission, slippage and spread models; their presence is a positive control, not proof that deployed experiment settings are realistic. The option spread setting at `:410` documents a zero default. Inspect saved run configurations and stress costs, gaps and liquidity for each strategy family; no production configurations were read in this audit.

6. **Positive architectural controls exist.** Shared package implementations, simulated-clock ATR injection, historical/as-of provider paths, and a blocking parity suite reduce duplication and some lookahead risks. FactorRanker data code explicitly passes filing-aware `as_of` information. These controls deserve preservation, but do not cover the ML leaks or conflicting-stop case identified above. Portfolio construction and expert snippets were sampled, not fully verified.

## Verification and limitations

- Added `reports/audit_2026_09_06_probes.py`; all included assertions passed on the bundled Python runtime. It extracts selected source functions using AST, removes import statements and substitutes minimal collaborators, avoiding application startup, databases and broker IO. This validates local logic, not integration behavior.
- Confirmed by executable probe: risk-budget exposure mismatch, target present in sequence input, false tail labels, split-boundary overlap arithmetic, and cap boundary behavior.
- Confirmed by source tracing only: normalization order and protective-stop policy difference. No actual model training or broker fill simulation was run.
- The repository `.venv/Scripts/python.exe` failed to start its configured Python 3.11 interpreter. The bundled runtime has NumPy/pandas but no pytest. Consequently the pytest suites and parity gate were **not run**. No dependencies were installed or upgraded.
- Root `pytest.ini` collects `tests/` by default. The inspected parity workflow runs backend `tests/backtest`; package-specific regressions and ML service tests need explicit collection elsewhere. This is a coverage limitation of these entry points, not a claim that no other CI exists.
- Findings refer to the inspected revision and working tree. No empirical profitability, calibration, account exposure or real-world exploit claim is made beyond the reproduced local calculations.

## Recommended order of work

1. Correct F1 and F2 and add behavioral integration regressions. Mark affected historical model/strategy results as requiring reevaluation.
2. Correct F3–F5 together: causal features, train-only transforms, label-availability masks and purged chronological splits.
3. Resolve F6 with a documented shared stop policy and paired execution tests. Harden F7's numeric boundary contract.
4. Restore a working test environment; explicitly run live risk/order tests, package tests, ML preparation regressions and the blocking backtest parity suite.
5. Rerun strategy selection with independently held-out data and recorded execution-cost assumptions. Review risk objectives and confidence interpretation separately from code correctness.
