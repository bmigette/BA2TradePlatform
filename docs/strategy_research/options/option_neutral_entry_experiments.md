# Option stage 1: HOLD versus low-confidence entries

Updated September 20, 2026, after the request to keep admission simple and let
the existing rules decide. This supersedes the expert-mode implementation.

## HOLD admission

The existing rule engine understands HOLD (`current_rating_neutral`). The
open-positions paths already evaluate HOLD in both live and backtest. However,
the original entry paths exclude it before evaluating any rules:

- Live: `TradeManager.process_expert_recommendations_after_analysis` filters
  recommendations with `recommended_action != HOLD`.
- Backtest: `_recommendation_to_expert_recommendation` returns no entry row for
  HOLD unless `allow_hold` is true.

The only new expert setting is `evaluate_entry_rules_on_hold`, a boolean defaulting
to **false**. Missing and unsaved/null values also mean false. When enabled, HOLD
reaches the same entry rules and risk-management flow as other recommendations.
It does not choose a strategy, change the recommendation, or guarantee a trade.
Automated-opening, funding, duplicate-position and option-capacity guards still apply.

The live opt-in pure-option path calls the existing option actions, which size and
submit through their own shared risk guards, matching the backtest option path.
Equity actions continue through the existing candidate risk-manager flow. The
setting has no effect on open-position management.

No expert scoring, trade condition, action or risk-manager implementation changes.
The earlier `neutral_option_entry_mode` setting and its custom decoder are removed.
No database migration is needed: the boolean uses the existing settings table.
No live instance is automatically opted in.

## One job, ordinary rules

Use `--neutral-entry-modes joint` on the stage-1 driver. O_STRD, O_STRG and O_IC
each get one job containing two ordinary entry rules:

| Rule | Required conditions | GA control |
| --- | --- | --- |
| HOLD | `current_rating_neutral` | Existing rule-enabled gene and option parameters |
| Low confidence | `rec_direction != 0` and `confidence <= threshold` | Existing rule-enabled gene, confidence threshold and option parameters |

The defining signal conditions cannot be switched off individually. Each rule
also retains the applicable position and market gates. HOLD has no directional
target, so that rule excludes the expected-profit and confidence gates. Other
common condition IDs retain shared genes across the two rules.

The GA may select HOLD, low confidence, or both. Turning both rules off yields no
entries and receives the existing no-trade fitness treatment. The two signal
conditions are disjoint, so both rules cannot fire for the same recommendation.
There is no new condition class, expert-mode gene, decoder metadata or special
rule engine behavior. Saved rules use the normal normalization/export/import path.
The fixed true admission setting travels with `expertFixedSettings` to deployment.

Without the flag, existing strategy templates and parameter spaces are unchanged.
The campaign remains 16 jobs. The three new neutral identities include
`-joint-rules1-`; the other 13 identities retain checkpoint compatibility.
Fixed `hold` and `low_confidence` experiments remain available for comparisons.

## Data and comparison

No additional data or warmup is required. Reuse the warmed ThetaData/OHLCV caches
and pinned `ohlcv-v1` / `ta-structure-v1` manifests; the stage-1 cache verification
still runs. Compare under the same window, equity, fitness and population budget.
Keep previous jobs and Top-N backtests rather than overwriting their identities.
