# Stage-2 option expert: what the ruleset layer can express (research, read-only)

Repo `C:\Users\basti\Documents\dev\BA2TradePlatform`, branch `dev` (checked 2026-10-24) plus branch `origin/plan/option-stage2` (tip af9793f9).
Path shorthand: L = `testplatform/ba2test_launcher.py` (dev), L2 = same file on `origin/plan/option-stage2`, C = `packages/common/ba2_common/core/`.
"Verified by running" means I imported the dev launcher and printed the built Strategy (market profiles set to `ohlcv-v1,ta-structure-v1`).

## 0. Headline

1. On `dev`, a multi-structure expert exists only as the fixed groups OS1..OS4 / O_LEAP / O_CONVEX. They give per-member entry gates, DTE, strike and sizing genes, but ONE shared exit set per group. They cannot choose a structure by horizon.
2. `origin/plan/option-stage2` (NOT merged to dev) already builds the real mechanism: `O_STYLE`, a spec-driven style expert (JSON members, per-member exits scoped by a new `entry_tag`, per-entry routing, member-scoped assigned-stock hold). Build Tasks 0-9 plus E1/E1b/E2 are done there. Tasks 10-21 (strict seeding, seed bundle, driver `--profile style`, `stage2_run.sh`, cost report, pilot) are NOT built.
3. Market-condition gates measure the UNDERLYING STOCK only (the symbol being evaluated), from 128 daily bars, as of the prior completed session. No gate reads SPY or any index. A SPY regime gate needs new code (section 3).
4. Structure choice is first-match on entry-rule CONDITIONS in member order (precedence), not best-of-N. One structure per underlying at a time, with or without the option RM.
5. "Horizon" can be expressed as per-member DTE windows, but one style cannot hold the same structure key twice (e.g. O_LC short AND O_LC long): the style loader refuses a repeated key ("listed twice", L2 `_load_style_spec` region ~L2:5927).

## 1. How a ruleset / entry is built from a launcher spec (dev)

Chain: `_OPTION_STRATS[kind]` row -> `_option_entry_action_for(kind)` (action dict with `option_*` keys) -> `_option_entry_rule(member)` (rule + condition tree) -> `_build_strategy_option(kind)` (single) or `_build_strategy_option_group(kind)` (group) -> Strategy(entry_rules, exit_rules) -> `rules_convert` writes live-style rules with `order_index` = list position.

- Row: `_OPTION_STRATS` L:2643 (action_type, strike method/param, `option_dte_min/max`, `option_dte_*_range/step`, `option_sizing`, optional `screener_gate_base`). O_LC at L:2644.
- Action: `_option_entry_action_for` L:3762. Adds `option_min_one_contract` (not for O_CONVEX*), `option_min_volume`, strike-method gene (percent_otm | delta), sizing gene (bands `_OPTION_SIZING_BANDS`), min-ARC gene (credit), entry-cross gene (0.75-1.0), selection-weight genes (`option_w_premium/iv/rvol`, `option_selection_half` debit | credit).
- Entry rule: `_option_entry_rule` L:6108. Rule id `<m>-entry`, `continue_processing=False`. One AND list, in this order:
  1. `<m>-signal`: `rec_direction` with a MODE gene (off/below/above), L:6068. Authored `above` for bullish members, `below` for O_LP/O_BEARCS (`_OPTION_ENTRY_GATE` L:3231). Neutral members (O_STRD/O_STRG/O_IC, `_NEUTRAL_ENTRY_MEMBERS` L:3246) use the flag `current_rating_neutral` (HOLD bucket).
  2. `<m>-flat`: `has_no_position` (not switchable; expert-level per symbol, `C/TradeConditions.py:593`).
  3. `shared-gate_confidence` (`confidence >`, 40-75) for directional members; neutral members get `<m>-low_confidence` (`confidence <=`, 10-70) instead (`_low_confidence_gate` L:5005).
  4. `<m>-iv_rank` (L:4856, direction flips debit/credit), `shared-rel_volume` (L:4923), `<m>-iv_rv` (L:4946), `<m>-exp_profit` (`expected_profit_target_percent >`, L:4975).
  5. Event members only: days-to-earnings gate (O_ERN, `_EVENT_ENTRY_MEMBERS` L:4995).
  6. Market-condition leaves `<m>-market-<short>` (`_market_condition_gates` L:5261, built by `C/market_condition_templates.py:market_condition_leaves`), LAST in the AND list. Each has a `cond:<id>:mode` gene (off/below/above) and a `cond:<id>:value` gene. The categorical `structure` leaf has mode choices off / bull / bear.
- Exits: `_option_exit_rules(kind)` L:4604. Rules `opt_tp` (`profit_loss_percent >`; debit 25-200 default 100, credit 25-75 default 50), `opt_time` (`days_opened >`), `opt_dte` (`days_to_expiry <=`, default band 0-21; per-key bands in `_OPTION_DTE_EXIT_BANDS` ~L:4410), credit only `opt_sl` (-200..-50), `opt_sl_ml` (`loss_pct_of_max_loss >`, 25-75) when any member has a measured max loss. All `close_option`, all toggleable. Genes look like `exit:opt_tp:enabled`, `cond:tp:value` (ids unprefixed).
- Single job: `_build_strategy_option` L:4750. Group: `_build_strategy_option_group` L:6219 (one entry rule per member with `toggle_optimize=True`, giving an `entry:<m>-entry:enabled` gene).

### Shape of a generated ruleset (verified by running)

O_BULLCS (single, both profiles on):
```
ENTRY o_bullcs-entry   continue_processing=False
  action a0: open_bull_call_spread
     option_strike_method {percent_otm|delta}, option_strike_param 2 (0-6 step 2), option_strike_delta 0.3 (0.05-0.5)
     option_dte_min 25 / max 45 (decoded from the option_dte gene, range 20-60 step 5)
     option_sizing 5 (1-10), option_entry_cross 0.75 (0.75-1.0), option_min_one_contract=True, option_min_volume=25
     option_selection_half=debit, option_w_premium / option_w_iv / option_w_rvol genes
  AND: o_bullcs-signal (rec_direction > 0, mode gene)   o_bullcs-flat (has_no_position)
       shared-gate_confidence (> 50, 40-75, toggle)     o_bullcs-iv_rank (< 30, 10-60, toggle)
       shared-rel_volume (> 0.5, 0.5-1.5, toggle)       o_bullcs-iv_rv (< 1.6, 1.0-1.6, toggle)
       o_bullcs-exp_profit (> 5, 2-20, toggle)
       o_bullcs-market-slope (> 0, -0.3..0.3)  -adx (< 25, 10-40)  -rv (< 1.0, 0.5-2.0)
       -dist-support (> 1.0, 0-5)  -dist-resistance (> 1.0, 0-5)  -chan-pos (< 0.5, 0-1)
       -vs-prior-high (> 0, -3..2)  -structure (== bull|bear, mode gene)
EXIT  opt_tp (profit_loss_percent > 100, 25-200)   opt_time (days_opened > 28, 10-45)
      opt_dte (days_to_expiry <= 21, 0-21)         opt_sl_ml (loss_pct_of_max_loss > 50, 25-75)
      (all close_option, all with toggle genes)
```
O_STRD with `--neutral-entry-mode joint` (`_configure_neutral_entry` L:6761): TWO entry rules, ids `o_strd-entry-hold` and `o_strd-entry-low_confidence`, both `toggle_optimize=True`.
- hold arm: signal = `current_rating_neutral` (fixed on); `exp_profit` and `low_confidence` leaves removed.
- low_confidence arm: no signal leaf; adds `o_strd-low_confidence` (`confidence <=`, fixed on) and `o_strd-directional` (`rec_direction != 0`).
- Both arms carry the same gate ids (iv_rank, shared-rel_volume, iv_rv, all 8 `market-*`), so those genes are shared (stage-2 plan section 3.7).
- Action: open_straddle, DTE 20-40 (gene range 10-55 step 5), sizing 5 (1-10), no strike genes (ATM). Exits are the same four rules (O_STRD is in `_DEBIT_OPTION_KINDS`, so debit bands).

Order/precedence: `C/rules_convert.py:571` writes `order_index = len(out)` (list position); `C/db.py:162-175` loads event actions `ORDER BY order_index`; the evaluator walks them in that order.

## 2. Existing groups OS1..OS4: members, gates, DTE, sizing, exits, precedence

Definition: `_OPTION_GROUPS_ALL` L:3370, filtered into `_OPTION_GROUPS` L:3394 (drops `_FULL_NOTIONAL_OPTION_KINDS` = O_CSP/O_JL/O_RS L:3307 and `_UNDEFINED_RISK_MEMBERS` = O_SSTG/O_SSTD L:3345). Searchable members (verified by running):
- OS1 = O_LC, O_LP, O_VERT, O_BF, O_BULLCS (debit). OS2 = O_IC only. OS3 = O_BEARCS, O_BULLPS. OS4 = O_STRD, O_STRG. O_LEAP = O_LEAPC + O_LEAPP. O_CONVEX = O_CONVEXC + O_CONVEXP.
- There is NO `OS_ALL` in code (only mentioned in comments, L:5191). A cross-style group does not exist on dev.

Can members have their own gates, DTE, sizing and exits?
- Gates: yes, except two. Every id is prefixed with the member key, so each member has its own `iv_rank`, `iv_rv`, `exp_profit`, signal mode and all 8 `market-*` genes. `shared-gate_confidence` and `shared-rel_volume` are deliberately ONE gene for the whole group (docstring of `_option_entry_rule`, L:6108).
- DTE, strike, sizing, entry-cross, selection weights: yes, per member (they live in each member's action dict; OS1 prints `option_dte_*_range` 20-60 and sizing 5/5/5/8/5). The selection-weight genes are keyed per half (`optsel:<half>:<w>`), so they are shared across members of one half.
- Exits: NO. A group builds one exit list from `_option_exit_rules(group_key)`. Exit ids are unprefixed (`opt_tp`, `td`, `dte`), and the debit or credit band comes from the GROUP key (`OS1`, `OS4`, `O_LEAP`, `O_CONVEX` are in `_DEBIT_OPTION_KINDS` L:4381; OS2/OS3 are not, so credit). Mixing debit and credit members in one group would give one side the wrong bands (spec 7.1). The exit rules act on any position of the expert, whichever member opened it.

Precedence ("later member wins" is wrong, it is FIRST match):
- `C/TradeActionEvaluator.py:446-500` iterates event actions in `order_index` and, after the first rule whose conditions are met, `break`s unless `continue_processing` (False for all option entry rules). Member order in the `_OPTION_GROUPS` list is the precedence (O_LC before O_LP before ...).
- It is first match on CONDITIONS, not on a successful fill. If member A's gates pass and its builder then refuses (no contract, ARC floor, unaffordable, no deep chain), the evaluator does not fall through to member B on that bar (build doc open question Q4; unchanged on the branch).
- Memory trap confirmed: order_index IS precedence. The deploy import used to reset it to 0 (build doc Q13, marked unfixed there).

One structure per underlying:
- Without the option RM: the `<m>-flat` leaf (`has_no_position`, expert-level) in every entry rule, plus the engine's one-position-or-waiting gate per expert per symbol (`daily_engine._has_open_or_waiting_position`; on the branch `trade_cycle.open_or_waiting_position`). Once any member opens on a symbol, no other member opens there until it closes.
- With the option RM (`classic_options`): additionally the rail `one_per_underlying` (`C/option_book.py:187`, `check_rails` step 2).

Joint vs legacy neutral entry (O_STRD / O_STRG / O_IC only):
- legacy (default, one rule): single entry rule gated on `current_rating_neutral` (mode gene, the GA may switch it off) AND the `low_confidence` leaf. `evaluate_entry_rules_on_hold` stays False, so HOLD recommendations never reach the rules (`C/hold_entry.py`, `daily_engine.py:1478-1486`, `TradeManager.py:2609`). Reading the code, a legacy neutral job can only trade when the GA turns the signal leaf off and fires on low-confidence directional recs (inference, not run).
- `hold` / `low_confidence`: one fixed arm each; both set `evaluate_entry_rules_on_hold=True` (`_apply_neutral_entry_setting` L:6809).
- `joint`: both arms as two toggleable rules in one job (hold arm first, so HOLD wins when both pass). Driver flag `--neutral-entry-modes` (`tools/run_options_matrix.py:290`; job names get `-<mode>-rules1`). Stage-1 neutral jobs used joint (DB opt 124 O_IC, 128 O_STRD, 129 O_STRG).

## 3. What the market-condition gates measure

Registry: `C/market_conditions.py` (`OHLCV_V1` L:639; ta-structure-v1 FieldSpecs L:1208-1242). Window `WINDOW = 128` regular-session daily bars of THE SYMBOL (L:88). Evaluated in `MarketConditionCompare.evaluate` (`C/TradeConditions.py:4213-`), which calls `ctx.reader.observe(self.instrument_name, ctx.prior_session)`. Unknown or invalid never passes. `prior_session` is the last completed session before the decision (`market_condition_context.py` requires prior_session < session_label). Pivot lookahead guard: a pivot needs 3 later bars to confirm.

Searched gates (what the GA sees), anchor = authored op/value, range = threshold gene:

| leaf short | field | measures | horizon | anchor | range / step |
|---|---|---|---|---|---|
| slope | `underlying_trend_slope_50_atr14` | (EMA50[t] - EMA50[t-5]) / (5 x ATR14): 5-session change of the 50-day EMA, in ATR units per session | 50-session trend read over 5 sessions | `>` 0 | -0.30..0.30 /0.05 |
| adx | `underlying_adx_14` | Wilder ADX 14 (trend strength, direction-free) | 14 sessions | `<` 25 | 10..40 /5 |
| rv | `underlying_realized_vol_ratio_5_20` | std of last 5 log returns / std of last 20 | 5 vs 20 sessions | `<` 1.0 | 0.5..2.0 /0.25 |
| dist-support | `structure_dist_support_atr` | distance from close down to the nearest confirmed pivot-low level, in ATR14 | pivots (span 3) in 128 sessions | `>` 1.0 | 0..5 /0.5 |
| dist-resistance | `structure_dist_resistance_atr` | distance up to the nearest confirmed pivot-high level, in ATR14 | same | `>` 1.0 | 0..5 /0.5 |
| chan-pos | `channel_pos_20` | position in the 20-close OLS channel (0 floor, 1 ceiling, unclamped) | 20 sessions | `<` 0.5 | 0..1 /0.1 |
| vs-prior-high | `close_vs_prior_high_20_atr` | (close - prior 20-session high) / ATR14 | 20 sessions | `>` 0 | -3..2 /0.25 |
| structure | `structure_state` (categorical) | swing state from the last two swing highs and lows: bull (HH+HL), bear (LH+LL), else none (never selectable) | 128 sessions | `==` | off / bull / bear |

Defined but NOT searched (`searched=False`, no gene): support/resistance touches, channel slope, channel width, close-vs-prior-low, bars-since-BOS, bars-since-CHoCH. Enabling one is a one-line FieldSpec change plus rebuilt manifests.

Answers:
- Underlying or market index? The UNDERLYING STOCK of the entry being evaluated. Nothing reads SPY. SPY and QQQ are not in any `tools/options_universe_*.txt` (grep), so the manifests hold no SPY rows.
- Could a gate describe the MARKET (SPY) regime? Not today. Closest existing pieces:
  - `C/market_regime.py` (SPY by default: SMA200 trend regime, realized-vol rank, `is_stressed`) and `C/regime_overlay.py`. They only SCALE risk, stop and take-profit (`regime_*_scale`, `TradeActions.py:1248-1270`). They are not a rule condition and cannot select a structure.
  - To add a SPY gate: (a) put `SPY` into the universe and market-condition manifests (`tools/warm_market_conditions.py plan/build/verify`; whether the OHLCV-built store accepts an ETF is not verified); (b) a new condition class, or a benchmark attribute on `MarketConditionCompare`, that calls `ctx.reader.observe("SPY", session)` instead of `self.instrument_name` (`TradeConditions.py` ~L:4290); (c) register leaves in `market_condition_templates.market_condition_leaves` with their own short ids (e.g. `spy-slope`), plus live per-symbol coverage in `market_condition_live.py`. BT/live parity is cheap because the reader is already shared, so (b) is the only new logic.
  - No-code partial route: the stock's own `adx` / `slope` / `structure` gates already act as a per-stock regime. Large caps correlate with the index, but that is not the index regime.
- Horizon: all measures are daily-bar, 5-128 sessions. The 20- and 50-session ones suit 25-90 DTE structures. None is tuned to LEAPS horizons (no 200-day measure).

## 4. Stage 1.5 drivers and job status

Drivers: `tools/run_options2_matrix.py` (grid 2, jobs `opt2-<expert>-<strategy>`) and `tools/run_convex_matrix.py` (`convex-<expert>-O_CONVEX`). Rows from `_OPTION_STRATS` (verified by running):

| key | structure | action | authored DTE window / gene range | strike | sizing % | intended horizon / role |
|---|---|---|---|---|---|---|
| O_LEAPC / O_LEAPP (group key O_LEAP) | LEAPS long call / long put, one shared exit set | buy_call / buy_put | 380-470 / centre 315-495 (entry floor lowered 365 -> 270 on 2026-09-30) | delta 0.8 | 5 | 9-18 months. Call arm on bullish, put arm on SELL. Exit-DTE band 90-240 step 30 |
| O_PMCC | LEAPS long call + rolling short call overlay (two expiries) | open_pmcc | long leg 380-470 / 315-495, overlay ~30-45 | delta [0.8, 0.2] | 5 | LEAPS version of a covered call. `days_to_expiry` reads the long leg |
| O_ERN | earnings long vol (straddle) before the print | open_straddle | 14-28 / 14-23 | delta 0.35 | 5 | short, event-driven. Needs the FMPEarningsEvent expert, not DS |
| O_CBS | call backspread 1x2 | open_call_backspread | 80-140 / 90-150 | delta [0.2, 0.4] | 5 | upside convexity, 3-5 months. Extra exit `opt_tp_mult` (2-6x premium) |
| O_PBS | put backspread (crash hedge) | open_put_backspread | 80-140 / 90-150 | delta [0.2, 0.4] | 5 | bearish / tail hedge, 3-5 months |
| O_CONVEXC / O_CONVEXP (group O_CONVEX) | far-OTM long-dated calls / puts, many small tickets | buy_call / buy_put | 300-420 / 240-480 | delta 0.2 | 1.0 (band 0.5-5) | 8-18 months convex harvest. Scored under `option_convex`, never `option_car`. No min-one-contract floor. `opt_time` authored OFF |
| O_CAL | ATM calendar | - | - | - | - | phase-gated, refused by the launcher |

Chain-depth preflight (`_MIN_DTE` in `run_options2_matrix.py`): O_LEAP / O_PMCC 270, O_CBS / O_PBS 180, O_ERN 7; convex 270. Default pop 40, gen 6.

Local DB `C:/Users/basti/Documents/ba2/test/dl_forecasting.db` (opened read-only):
- `strategy_optimizations` with id < 100: ONE row. id 1 `opt2-FMPRating-O_LEAP-hsalpaca`, status running, best_fitness 19.19, progress 74.7, fitness_metric `option_car`.
- No O_PMCC, O_ERN, O_CBS, O_PBS or convex job is in this DB (34 rows total: id 1 and ids 101-134). They may be on remote150 / remote227 DBs or not have run; I did not check those hosts.
- Stage 1 for comparison (ids 101-134 = old ids + 100). Completed: 115 O_LC 14.13, 117 O_LC 25.52 (st1fix), 119 O_BULLCS 16.54, 120 O_BULLPS 1.64, 121 O_WHEEL 7.48, 122 O_BF 12.09, 124 O_IC joint 0.01, 125 O_JL 0.11, 126 O_RS 1.19, 127 O_CSP 2.80, 128 O_STRD joint 17.41, 129 O_STRG joint 21.82, 130 O_CC 14.56, 131 O_PP 10.48, 132 O_LP 0.0, 133 O_VERT 0.09. Running: 134 O_BULLPS (netput rerun) at 1%. These use the soft30 fitness family, so they are not comparable with O_LEAP's `option_car`.

## 5. What is NOT possible today, and what is needed

### 5a. Gaps (dev vs branch)

| need | gap on dev | where it is / would be built |
|---|---|---|
| one expert, structures from different groups | no cross-group key; `OS_ALL` absent | superseded by `O_STYLE` on the branch |
| per-member exits (debit and credit side by side, own DTE bands) | exit ids unprefixed, band from the group key (`_option_exit_rules` L:4604) | branch: member-prefixed exit ids ANDed with an `entry_tag == N` leaf |
| knowing which member opened a position | no per-entry fact on the order row | branch: `C/entry_tag.py`, `EntryTagCondition` in `TradeConditions.py` |
| mixed equity and option entries (O_CC / O_PP / O_WHEEL beside spreads) | engine chose one entry path per run from `config["entry_action"]` | branch E1: `trade_cycle.entry_route`, per-entry routing, BT and live |
| assigned-stock hold for the wheel only | `hold_assigned_stock` is account-level (`_HOLDS_ASSIGNED_STOCK` L:5179) | branch E2: `hold_assigned_stock_tags`, `C/assignment_hold.py` |
| market (SPY) regime gate | gates read only the symbol | new condition class, section 3 |
| best-of-N structure choice by regime | evaluator is first-match on conditions, no fall-through on a refused action (Q4) | evaluator change at `C/TradeActionEvaluator.py:446-500` (shared BT and live, hits goldens), or use member ORDER as priority and disjoint gates |
| same structure at two horizons in one expert | loader refuses a repeated key (a member = one rule block + one tag) | member aliases: extend `_STYLE_MEMBER_TAGS` (L2:5593) and `_style_member_id` (L2:5828) |
| per-arm gate ids for neutral joint arms | arms share every gate except signal / low_confidence | plan 3.7, not built |
| asymmetric iron condor | one strike %OTM and one wing for both sides | plan 3.6, not built |
| no stock-price caps, bound by risk | `screener_gate_base.price_max` on O_CSP / O_JL / O_RS (L:2680-2797), merged by `_screener_gate_base_for_strategy` L:4268 | plan 3.9; style spec `price_cap_policy: none`; full-notional sizing cost must equal assignment notional |

### 5b. Stage-2 plan build items and how far each is

Read from `git log dev..origin/plan/option-stage2` and file existence on that branch.

| item | status |
|---|---|
| 1. Style groups | BUILT on the branch as `O_STYLE` (not as `_OPTION_GROUPS` entries): `_STYLE_KEY` L2:3087, `_STYLE_MEMBER_TAGS` L2:5593 (22 members, append-only tags), `_load_style_spec` L2:5960, `_build_strategy_style` L2:6329, CLI `--style-config FILE --style NAME`. Candidates in `tools/option_styles.candidates.json`: short, mid, leaps_convex, premium, all `status: candidate`; mid DTE 60-90 are placeholders. Strategy-gene counts (profile none / both profiles): short 116/191, mid 140/230, leaps_convex 99/174, premium 216/336. Not merged to dev. |
| 2. Multi-source seeding | NOT built (Tasks 10-16: `encode_params_strict`, handler `seedPopulation` / `rangeOverrides`, `authored_genome`, `style_seeding.py`, `tools/build_style_seed_bundle.py`, `--seed-population`). None of those files exist on the branch. On dev `warmStartFromOptimizationId` is still a single int (`strategy_optimization_handler.py:2261`, `L:7322`) and absent genes fill with `min`. Seeding policy is the user's call after stage 1. |
| 3. Composition driver (`--profile style`, `tools/stage2_run.sh`) | NOT built (Tasks 17-18). `run_options_matrix.py` has only `matrix` and `discovery`. |
| 4. Wheel / overlay composition checks | BUILT as tests (Task 9: `test_option_style_premium_wheel.py`, `test_style_wheel_assignment.py`), with E2 member-scoped hold and E1b (CSP collateral excluded from the equity RM budget, `option_collateral_budget.py`). |
| 5. Per-year and concentration report | not found on either branch (not searched deeper). |
| 6-7. Asymmetric IC, per-arm neutral gates | not built. |
| 8. Per-instrument cap ranges narrowed | not built. `_OPTION_RM_OVERRIDE` (L:1826) is still 5-50 step 5 for every option kind. |
| 9. No price caps plus option RM on | decided 2026-10-24 in the plan. `risk_manager_mode` is plumbed (L:1287) but no shipped spec selects it; rails must be declared in the expert's fixed settings. |
| Cost / pilot (Tasks 19-20), no-impact gate and version bump (21) | not built. |
| Open user questions (build doc) | Q4 precedence (member order), Q5 convex fitness (refusal under CAR family), Q6 LEAPS depth (own universe chosen), Q7 mid DTE windows, Q8 shared genes, Q10 experts, Q12 gene lattice anchored at zero (DTE gene can decode below the 365 floor), Q13 deploy `order_index` collapse. Q1 and Q2 were answered by E1 and E2. |

Stage-2 universe: `tools/options_universe_stage2.txt` (753 names = 820 minus 67 recorded exclusions); market-condition manifests built and synced (digests in plan section 2).

Traps for any regime-switching design: (1) nested OR is silently ANDed, so "regime A or regime B" needs two rules or a numeric mode gene; (2) the engine reads the ruleset-to-rule link, not `EventAction.subtype`; (3) `_build_daily_trial_config` (`strategy_optimization_handler.py:2777`) must list any new knob (expert settings pass wholesale per the launcher comment at L:1257, but a new top-level key such as `hold_assigned_stock_tags` or a seed field must be checked); (4) a style expert cannot be deployed until Q13 is fixed, and deploy parity refuses to export a member-scoped-hold run.

## 6. $20k account constraints

Sizing (budget and floor):
- `option_sizing` is % of virtual equity per structure. GA bands `_OPTION_SIZING_BANDS` (L:~3880): authored 5.0 -> 1-10 step 1 (long premium, spreads, straddle); 8.0 -> 2-16 (butterfly); 15.0 -> 5-30 step 2.5 (credit spreads); 20.0 -> 5-50 step 5 (IC, JL, CSP); 1.0 -> 0.5-5 step 0.5 (convex). An import-time assertion refuses an authored value without a band.
- `_size_by_cost` (`C/TradeActions.py:3168-3202`): `budget = equity x option_sizing%`, then `budget = min(budget, equity x max_virtual_equity_per_instrument_percent%)`, `qty = floor(budget / cost_per_contract)`. Cost is premium x 100 for debit (`_size` L:3340) and the reserve per contract for credit and short structures (`_size_by_reserve` L:3360; call sites L:4840-5704).
- `min_one_contract` floor (`_one_contract_floor` L:3204-3258), on by default via `_option_entry_action_for` except O_CONVEX*: when qty rounds to 0 it sizes 1 if one contract fits the REMAINING per-instrument room (cap minus `_committed_to_underlying` L:3260) and the virtual equity. It refuses when the cap is unset or the commitment is unmeasurable. Ordinary sizing (qty >= 1) checks only the new ticket against the cap, not exposure already open on that name (plan 3.8 says the same).
- `_virtual_equity` L:3093 = `option_capital_equity()` x `virtual_equity_pct` (100% in the plan), same in BT and live.
- Per-instrument cap: `_OPTION_RM_OVERRIDE` L:1826 makes `max_virtual_equity_per_instrument_percent` a gene 5-50 step 5 for every option kind except O_STK (`_rm_opt_for` L:1836). Stage-1 winners sat at 45-50; one contract once reached 36% of equity. Plan 3.8: debit structures should be about 5-25, keep 50 only for wheel / CSP / JL / RS.
- Affordability: full-notional structures (CSP / JL / RS) reserve about strike x 100: $9,000-9,400 at a $100 stock (45-47% of $20k), about $18k at $200. That is why stage 1 capped them at `price_max` $100. Plan 3.9 drops the caps and relies on cap plus sizing to refuse what does not fit, so each structure's sizing cost must be its true worst-case risk (assignment notional minus credit for CSP / wheel / RS / JL, wing minus credit for credit spreads, premium for debit).
- Backtest-only: `option_size_within_fill_volume` (10% of bar volume), `option_entry_cross` gene, calibrated spread model.

Option risk manager (`risk_manager_mode = classic_options`; `C/OptionRiskManagement.py`, `C/option_book.py`):
- Single choke point `_OptionEntryAction._submit_option_order`; same code live and BT; opt-in; an unknown mode string does not engage it (warning).
- Required rails, each declared by the expert or the entry is refused by name: `max_concurrent_structures`, `max_deployment_pct`, `max_notional_leverage`, `circuit_breaker_pct` (`REQUIRED_RAIL_SETTINGS` L:263), plus `undefined_risk_max_pct` only for undefined-risk candidates.
- `check_rails` order (`option_book.py:539-`): breaker halt, max concurrent, one per underlying, equity known, book measurable, candidate measurable, deployment (committed capital incl. debit outlay vs % of equity), short notional vs leverage, undefined-risk sub-cap, assignment capacity (cash to take delivery of every short put; a long put of the same expiry nets to the wing width).
- Breaker: sleeve drawdown trip, re-arm at half the trip depth (`BREAKER_REARM_DEPTH_FRACTION`); the latch gates entries in both runtimes. It is one breaker for the whole expert, i.e. all structures of the style.
- Stage 1 never ran the RM. Its portfolio-level limits were therefore the per-trade budget, the per-instrument cap and the account's reserve / buying-power checks in `BacktestAccount` (inference from comments, not measured). Plan 3.9 requires a pilot: re-run one stage-1 winner with the RM on and off, count refusals (zero trades scores 0 and the GA cannot tell why).

## 7. Practical reading for "one expert, several structures, by regime and horizon"

- A style on the branch can already encode: per-member structure, DTE window, strike or delta, sizing, own entry gates (market gates are per member, so slope / adx / structure can route O_LC to uptrends, O_STRD to low-ADX, O_LP to bear structure), own exits, member ORDER as priority, member toggles so the GA can drop members.
- It cannot encode: an SPY / index regime; the same structure at two DTE horizons; fall-through when the first matching member refuses; a ranking among qualifying members (only order); regime-conditional sizing (sizing is a per-member gene).
- Smallest additions for a true regime-and-horizon expert: (1) SPY benchmark gate (section 3); (2) member aliases such as `O_LC@short` / `O_LC@mid` in `_STYLE_MEMBER_TAGS` and `_style_member_id`; (3) evaluator fall-through on action refusal (shared change, needs goldens), or ordered members with disjoint gates; (4) finish Tasks 10-18 for seeding and the driver; (5) merge the branch to dev at a job boundary (TEST_APP_VERSION bump, plus package version bumps for `packages/common` changes per CLAUDE.md).
