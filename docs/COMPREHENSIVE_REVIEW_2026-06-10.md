# Comprehensive Platform Review — 2026-06-10

Scope: expert strategies, agent prompts, risk managers (smart + classic), code quality/optimization.
This document defines the required changes. Each item has an ID, a severity, the files involved, and a
description precise enough to implement in a separate session.

Severity legend:
- **P1 — Bug / correctness**: wrong behavior today, fix first.
- **P2 — Strategy / prompt quality**: affects trading decisions or LLM output quality.
- **P3 — Code quality / optimization**: maintainability, performance, hygiene.

---

## ✅ Implementation status — ALL 27 ITEMS COMPLETED (2026-06-11)

Implemented across `review-fixes-2026-06-10` (Opus 4.8 sessions 1–8, merged in `afd3f32`) plus two
follow-up commits from the reviewing session (`a8bc9ba`, `cc84f9d`). Test suite grew from 723 to 777
passing tests. See `COMPREHENSIVE_REVIEW_2026-06-10.triage.md` for the adversarial-verification
corrections applied to this doc's claims (notably: the PR-1 "ERROR rows" symptom was latent, not live;
RM-4 was an edge case, not systematic; RM-8's real count was 21 sites outside except blocks).

| Items | Where implemented |
|---|---|
| PR-1, PR-2, EX-1, EX-5, EX-6, RM-1..RM-5, CQ-6 | `4242b74` (sessions 1–3) + `7f8ea89` (diversification factor defaults to 1.0 = off) |
| PR-3, PR-5 | `f41e40f` |
| PR-4, PR-6, PR-7, PR-8 | `0b775c9` (session 4) |
| RM-6, RM-7, CQ-4 (functional) | `ed7f39f` (session 5) — prompts split into `core/SmartRiskManagerPrompts.py` |
| EX-2 (amount-parser dedup + bug fixes) | `ea36c34` (session 6) — shared `parse_fmp_amount_range` in `core/utils.py` |
| EX-3, EX-4 (settings extraction) | `03d01d9` (session 7) |
| CQ-1, CQ-2, CQ-5, RM-8 | `5bcdf08` (session 8) |
| CQ-4 (cosmetic dedent), RM-8 leftover artifacts | `a8bc9ba` (review pass) |
| EX-2/CQ-3 (full mixin extraction: `modules/experts/expert_mixins.py`), EX-4 (full split: `screening.py`/`monitoring.py`/`data_gathering.py`) | `cc84f9d` (review pass) |

Notable deviations from the letter of this doc (all justified, see triage doc):
- **EX-2**: instead of one `FMPCongressTradingBase` class, the dedup landed as composable mixins
  (`AnalysisStatusRenderMixin`, `FMPApiKeyMixin`, `FMPCongressTradingMixin`) so FMPRating/FinnHubRating
  could share the render scaffolding (CQ-3) without inheriting congress-trading code.
- **RM-5**: the hardcoded 0.7 diversification factor became a per-expert setting defaulting to **1.0
  (off)** rather than 0.7, so the per-instrument cap governs diversification unless explicitly lowered.
- **EX-4**: methods were moved verbatim into mixins composed by `PennyMomentumTrader` (no
  delegation layer); `__init__.py` went from 3,531 to 607 lines.

---

## 1. Agent Prompts (TradingAgents framework)

### PR-1 (P1) Duplicate prompt constants silently override the good versions
`ba2_trade_platform/thirdparties/TradingAgents/tradingagents/prompts.py`

`SIGNAL_PROCESSING_SYSTEM_PROMPT` is defined twice (line 253 and line 313) and
`REFLECTION_SYSTEM_PROMPT` is defined twice (line 259 and line 315). Python keeps the **second**
definition, so `PROMPT_REGISTRY` (line 527) registers the weak versions:

- The effective signal-processing prompt says "transform decisions into clear, actionable formats"
  and **does not instruct the model to output only one rating word**. The strict version at line 253
  ("Output only the single rating word, nothing else") is dead code. `SignalProcessor.process_signal()`
  (`graph/signal_processing.py`) therefore can return verbose text; the fallback path in
  `modules/experts/TradingAgents.py:521` checks `processed_signal in ['BUY', ...]` and degrades to
  `OrderRecommendation.ERROR` when the text isn't an exact match.
- The effective reflection prompt is a one-liner; the detailed 5-section reflection prompt
  (lines 259–284) is dead code, weakening the memory/lessons-learned loop.

**Required change:** delete the two weak duplicates at lines 313–315, keep the detailed/strict
versions, and add a startup assertion or unit test that every `PROMPT_REGISTRY` key maps to the
intended constant (e.g. test that the signal-processing prompt contains "Output only the single rating word").

### PR-2 (P1) Market analyst prompt references tools that no longer exist
`prompts.py:44` — `MARKET_ANALYST_SYSTEM_PROMPT` instructs: *"Please make sure to call
`get_YFin_data` first to retrieve the CSV"*. The current toolkit (`agents/utils/agent_utils_new.py`)
exposes `get_ohlcv_data` / `get_indicator_data`; `get_YFin_data` does not exist. The prompt also warns
against selecting `stochrsi`, which is not in the offered indicator list. Stale instructions cause failed
tool calls / wasted turns.

**Required change:** rewrite the tool-usage paragraph to reference `get_ohlcv_data` and
`get_indicator_data`, and remove or fix the `stochrsi` example. Audit all analyst prompts against the
actual tool names exported by `agent_utils_new.py`.

### PR-3 (P2) Rating-scale inconsistency between research manager and trader/risk manager
`prompts.py` — `RESEARCH_MANAGER_PROMPT` asks for **Buy / Sell / Hold** (3-tier), while
`TRADER_SYSTEM_PROMPT` and `RISK_MANAGER_PROMPT` use the 5-tier scale
(BUY/OVERWEIGHT/HOLD/UNDERWEIGHT/SELL). The trader has to upgrade a 3-tier plan into a 5-tier
recommendation with no guidance, and OVERWEIGHT/UNDERWEIGHT nuance is lost at the research stage.

**Required change:** move `RESEARCH_MANAGER_PROMPT` to the same 5-tier scale (with the same
one-line definitions used in the risk-manager prompt), or explicitly document why research stays 3-tier
and tell the trader how to map plan → 5-tier rating.

### PR-4 (P2) Risk debaters and managers are missing the macro report
- `agents/risk_mgmt/aggresive_debator.py`, `conservative_debator.py`, `neutral_debator.py`: prompts
  include market/sentiment/news/fundamentals but **not** `state["macro_report"]`, although a macro
  analyst exists and bull/bear researchers receive `macro_report`.
- `agents/managers/research_manager.py:17`, `managers/risk_manager.py:17`, `trader/trader.py:17`:
  `curr_situation` (used for memory retrieval) also omits the macro report, so past-lesson matching
  ignores the macro regime.

**Required change:** thread `state.get("macro_report", "")` into the three debator prompts and into
`curr_situation` for research manager, risk manager, and trader (bull/bear already do this — copy that pattern).

### PR-5 (P2) Bull/Bear researcher prompts don't know about the 5-tier output
`BULL_RESEARCHER_PROMPT` / `BEAR_RESEARCHER_PROMPT` ask for a debate but never state what the
downstream decision space is. Telling them the judge will pick BUY/OVERWEIGHT/HOLD/UNDERWEIGHT/SELL
(and asking each side to state which tier they believe is justified and why) gives the research manager
much better material for a graded verdict.

**Required change:** add a short "decision space" paragraph to both researcher prompts.

### PR-6 (P3) Hardcoded "past mistakes" framing even when memory is empty
`RESEARCH_MANAGER_PROMPT` says "Here are your past reflections on mistakes: '{past_memory_str}'" —
when memory is empty the model receives an empty quote and sometimes invents lessons.
`TRADER_SYSTEM_PROMPT` has the same issue (trader.py at least defaults to "No past memories found.",
the managers pass ""). Also `n_matches=2` is hardcoded in 4 node files.

**Required change:** normalize: when no memories match, inject an explicit "No past reflections
available — do not fabricate lessons." string in all four nodes; lift `n_matches` into config.

### PR-7 (P3) Typos and noise in debator prompts
`aggresive_debator.py` (filename typo "aggresive"), "halluncinate" in all three debator prompts.
Low impact, but these prompts ship to the model.

**Required change:** fix the typos in prompt text; optionally rename the file (update imports in
`graph/setup.py` / `agents/__init__.py`).

### PR-8 (P2) FINAL_SUMMARIZATION prompt duplicates what the structured parser already does
`FINAL_SUMMARIZATION_AGENT_PROMPT` spends ~70 lines on JSON formatting rules (trailing commas,
comments), while `summarization.py` already uses `CleanJsonOutputParser` + Pydantic
`ExpertRecommendation` with `format_instructions`. The duplicated schema (in the prompt AND in
format_instructions) is a drift risk — the prompt schema says `confidence: 0.0` example while field doc
says 0–100; `details` max 2000 chars in both places must stay in sync.

**Required change:** strip the hand-written JSON schema/formatting rules from the prompt and keep only
the decision-framework content (follow final_trade_decision, expected-profit sign conventions, risk/time
horizon guidance); let `format_instructions` own the schema. Keep one source of truth.

---

## 2. Risk Managers

### RM-1 (P1) Classic risk manager: `or`-fallback treats valid 0 values as missing
`modules/experts/TradingAgents.py:291–300` (and the same pattern in `_create_tradingagents_config`
lines 326–347): `int(self.settings.get('debates_new_positions') or settings_def[...]['default'])`.
A user configuring `0` debate rounds (or `0` lookback days) silently gets the default instead. This
pattern appears throughout the config builder.

**Required change:** replace `x or default` with explicit `if x is None` checks (helper:
`_setting_or_default(key)`), consistent with the CLAUDE.md "no hidden defaults" rule.

### RM-2 (P1) Classic risk manager: N+1 query loads every pending order in the system
`core/TradeRiskManagement.py:_get_pending_orders_for_review` (lines 236–273) selects **all** PENDING
orders with a recommendation, then calls `session.get(ExpertRecommendation, ...)` per order to filter
by expert. With many experts/orders this is O(N) queries per risk-manager run.

**Required change:** single join query:
`select(TradingOrder).join(ExpertRecommendation).where(TradingOrder.status == PENDING,
ExpertRecommendation.instance_id == expert_instance_id)`. Also remove the tuple-unpacking workaround
(`order_tuple[0] if isinstance(...)`) — `session.exec(select(Model))` returns model instances.

### RM-3 (P2) Prioritization ignores confidence and risk level
`TradeRiskManagement._prioritize_orders_by_profit` sorts purely by `expected_profit_percent`
(with `or 0.0` fallback). Two consequences:
- Experts that don't estimate profit (FinnHubRating stores `expected_profit_percent=0.0`,
  `FinnHubRating.py:252`) always sort last regardless of conviction.
- A 40%-confidence/30%-profit moonshot outranks an 85%-confidence/10%-profit setup.

**Required change:** score = f(expected_profit, confidence, risk_level), e.g.
`expected_profit_percent * (confidence/100)` with a documented fallback ranking when profit is 0/None
(rank by confidence). Make the formula a small pure function with unit tests.

### RM-4 (P2) WAITING transactions counted as $0 allocation → over-allocation window
`TradeRiskManagement._get_existing_allocations` (lines 334–369): transactions in WAITING status get
`current_value = 0`. Pending-but-sized orders from a previous run therefore don't consume the
per-instrument cap, so a subsequent run can size another order on the same symbol up to the full cap
(double exposure once both fill).

**Required change:** for WAITING transactions, estimate allocation as `quantity × open_price`
(if set) or `quantity × current_price`; document the choice. Add a test covering two consecutive runs.

### RM-5 (P3) Classic risk manager hardcodes strategy constants
- Diversification factor `0.7` hardcoded (line 487).
- Fallback `max_virtual_equity_per_instrument_percent = 10.0` (line 94) contradicts the no-defaults rule.
- `_update_orders_in_database` swallows all exceptions (logs only) — a failed quantity sync leaves
  entry/TP/SL inconsistent without surfacing the failure to the caller, which then logs SUCCESS activity.

**Required change:** move the diversification factor into expert settings (default 0.7); raise instead
of defaulting the per-instrument cap; propagate DB-update failures so the activity log reflects partial failure.

### RM-6 (P3) Smart Risk Manager: ~300 lines of dead duplicate tool definitions
`core/SmartRiskManagerGraph.py:900–1208` `create_toolkit_tools()` defines 16 `@tool` wrappers that are
**never called** — the live definitions are the class-level `_create_research_tools()` (lines 2456–2770).
Two near-identical copies of every tool docstring/signature is a guaranteed drift source (it has already
drifted: the module-level set lacks `get_price_movement_tool`, pending-action tools, recommend-tools).

**Required change:** delete `create_toolkit_tools()` (verify no external imports first), or refactor both
call sites to a single factory.

### RM-7 (P2) Smart Risk Manager prompt/file size and consistency pass
`SmartRiskManagerGraph.py` is 3,400 lines containing prompts, state, callbacks, tools, nodes, and the
runner. Specific prompt issues to fix while splitting:
- `RESEARCH_PROMPT` mentions "Direction Policy" twice but no section with that name exists in the
  prompt — the model is told to apply a policy it never receives (it's presumably the buy/sell/hedging
  permissions from `SYSTEM_INITIALIZATION_PROMPT`; name them consistently).
- `PORTFOLIO_ANALYSIS_PROMPT` says actions need "FILLED Positions" transaction IDs; `RESEARCH_PROMPT`
  calls the same thing "CURRENT portfolio summary (Transaction #XXX)". Align terminology.
- The tool list inside `RESEARCH_PROMPT` is hand-maintained and interleaved with prose
  (the "Buy-the-Dip Analysis" paragraph sits in the middle of the bullet list, lines 716–717) —
  reorder so the tool list is contiguous, and consider generating it from the bound tools.

**Required change:** split the module (prompts.py / state.py / tools.py / nodes.py / runner.py under
`core/smart_risk_manager/`), fix the three prompt inconsistencies above.

### RM-8 (P3) `exc_info=True` outside except blocks (CLAUDE.md violation), 476 occurrences
Example: `TradeRiskManagement.py:62, 69, 138, 154` log errors with `exc_info=True` where no exception
is in flight (it logs `NoneType: None`). A repo-wide grep found ~476 `exc_info=True` usages across
core/modules; a meaningful share are outside `except` blocks.

**Required change:** sweep and remove `exc_info=True` from non-except-context logging; optionally add a
lint rule (custom flake8/ruff check) to enforce it.

---

## 3. Expert Strategies

### EX-1 (P1) FMPRating: price-target boost is directionally wrong for SELL signals
`modules/experts/FMPRating.py:272–302`. `price_target_boost` = average % distance from current price to
target-low and target-consensus, **added** to confidence regardless of signal direction:
- BUY + price below targets → positive boost → higher confidence. Correct.
- SELL + price **above** targets (more downside, the bearish thesis has more room) → negative boost →
  **lower** SELL confidence. Inverted: the more profitable the short setup, the less confident the expert.

Also lines 279–292: the `if current_price > target_low / else` branches compute the **identical**
formula in both arms (twice) — dead conditional that obscures the logic.

**Required change:** sign the boost by direction (`confidence += boost` for BUY,
`confidence -= boost` i.e. `+= -boost` for SELL, no boost for HOLD), and collapse the duplicate
branches. Add unit tests: SELL with price above targets must gain confidence.

### EX-2 (P2) FMPSenateTrader (Copy + Weight): large-scale duplication and stale docs
`FMPSenateTraderCopy.py` (1,727 lines) and `FMPSenateTraderWeight.py` (1,535 lines) duplicate:
`_fetch_senate_trades`, `_fetch_house_trades`, `_filter_trades`, amount-range parsing
(implemented twice *within* Weight alone — `_calculate_trader_confidence` and `_calculate_confidence`),
`_create_expert_recommendation`, `_store_analysis_outputs`, and all five `_render_*` methods.
Additionally in Weight:
- `_calculate_confidence` docstring claims "trader pattern modifier (-30 to +30)" but the actual
  modifier is `symbol_focus_pct` capped at 0–10 (`_calculate_trader_confidence`), so real confidence
  range is ~50–80, not 20–100 as documented. Either the cap or the doc is wrong — decide the intended
  scale and align.
- Bare `except:` on date parsing (silently drops trades with unexpected date formats).

**Required change:** extract a shared `FMPCongressTradingBase` (fetching, parsing, amount helper,
rendering); fix the confidence-modifier documentation/intent; replace bare excepts with logged warnings.
Same base class can host the `_render_pending/_running/_failed/_skipped` scaffolding duplicated across
**all four** FMP/FinnHub experts (6 occurrences each per file).

### EX-3 (P2) PennyMomentumTrader: exit-update prompt contradicts entry prompt on time conditions
`modules/experts/PennyMomentumTrader/prompts.py`:
- `build_entry_conditions_prompt` (line 382): "IMPORTANT: use `time_after` (not `time_before`) for
  end-of-day exits".
- `build_exit_update_prompt` valid-types list (lines 517–527) offers **only `time_before`**
  ("exit before this time e.g. 15:45") and omits `time_after`, plus omits EMA/SMA/MACD/opening-range
  types that the entry prompt allows — so an update can invalidate a previously valid condition set,
  triggering needless fix-prompt round trips.

**Required change:** make `build_exit_update_prompt` use the shared `_CONDITION_PARAMS_REFERENCE`
(already used by the generate/fix prompts) instead of its own hand-rolled subset, and align the
time-condition semantics text in both places.

### EX-4 (P3) PennyMomentumTrader `__init__.py` is a 3,531-line monolith
Phases 0–6, FMP quote plumbing, news/fundamentals/insider/social gathering, JSON retry logic, and state
management all live in the package `__init__.py` even though the package already has `conditions.py`,
`prompts.py`, `trade_manager.py`, `ui.py`.

**Required change:** split into `pipeline.py` (phases), `screening.py` (phase 1/1b/1c),
`monitoring.py` (phase 5/6), `data_gathering.py` (news/fundamentals/insider/social + FMP quotes),
keeping `__init__.py` as the interface class that delegates. Pure mechanical move, no behavior change.

### EX-5 (P2) FinnHubRating: zero expected profit makes it invisible to classic risk sizing
`FinnHubRating.py:252` stores `expected_profit_percent=0.0`. Combined with RM-3, any FinnHub-driven
order sorts last and, in mixed-expert accounts, may never get funded. Either RM-3's confidence-aware
scoring fixes this, or FinnHub should derive a rough profit proxy (e.g. scaled by consensus mean
distance from 3.0) — decide one; don't do both silently.

### EX-6 (P3) TradingAgents expert: fallback recommendation hides failures as MEDIUM/SHORT_TERM
`modules/experts/TradingAgents.py:_extract_recommendation_data` (lines 504–528): when
`expert_recommendation` is missing, it fabricates `confidence=0.0, risk_level=MEDIUM,
time_horizon=SHORT_TERM` and proceeds to create an ExpertRecommendation. With PR-1 unfixed, the
processed signal is often unparseable → signal=ERROR rows with synthetic metadata. After PR-1, decide:
should a missing structured recommendation create a recommendation at all, or mark the analysis FAILED?
Recommend the latter (no recommendation row on fallback unless the signal parsed cleanly).

---

## 4. Code Quality / Optimization (cross-cutting)

### CQ-1 (P3) Dead files and artifacts tracked in git
- `thirdparties/TradingAgents/tradingagents/agents/utils/agent_utils.old.py` (592 lines) and
  `dataflows/interface.old.py` — dead, only `agent_utils_new` is imported.
- `thirdparties/TradingAgents/results/MSFT/2015-09-01/**` — sample analysis output committed upstream;
  not used by the platform.
- Untracked clutter at repo root: `.DS_Store`, `logs.bak.20260531-230758.tar.gz` (add to `.gitignore`).

**Required change:** delete the `.old.py` files and `results/`; consider renaming `agent_utils_new.py` →
`agent_utils.py` (3 import sites) once the old file is gone; extend `.gitignore`.

### CQ-2 (P3) `test_files/` has 91 ad-hoc scripts
One-off investigation scripts (e.g. `probe_tastytrade_dividends.py`, `set_strategy_notes_instance5.py`)
accumulate alongside real tests. They confuse "Running Tests" guidance in CLAUDE.md.

**Required change:** move durable tests under `tests/` (pytest), archive or delete one-off probes;
document the split in CLAUDE.md.

### CQ-3 (P3) Per-render duplication of status scaffolding in experts
`_render_pending/_running/_failed/_skipped/_completed` are re-implemented in FMPRating,
FinnHubRating, both SenateTraders (4 × ~5 methods, near-identical NiceGUI code). Covered by EX-2's
base-class extraction — listing here so it's not skipped if EX-2 is descoped to the Senate pair only.

### CQ-4 (P3) `SmartRiskManagerGraph.action_node` body indentation / structure
Lines 2027+ — the `for idx, action in enumerate(...)` body is indented two levels (leftover from a
removed `with`/`try` block), and the loop logs `idx+1/{len(recommended_actions)}` while iterating
`optimized_actions` (counts can disagree after TP/SL merging). Cosmetic but confusing during incident
debugging.

**Required change:** normalize indentation, log against `len(optimized_actions)`.

### CQ-5 (P3) Config access pattern drift in thirdparties graph code
`graph/trading_graph.py` uses `self.config.get('max_debate_rounds', 1)`, `get('max_recur_limit', 100)`,
etc., contrary to the project's explicit-config rule. The BA2-side config builder always provides these
keys, so the defaults only hide future wiring mistakes.

**Required change:** switch to direct `config[...]` access inside thirdparties graph code for keys the
platform always supplies (keep `.get` only for genuinely optional flags like `enable_streaming`), so a
missing key fails loudly.

### CQ-6 (P2) No tests around the reviewed decision math
None of the following has unit coverage: `consensus_from_counts` (FinnHubRating — pure function,
trivially testable), FMPRating `_calculate_recommendation`, classic risk-manager quantity allocation,
Senate confidence calc, PennyMomentum condition validation. These are the highest-value test targets
because they are pure(ish) functions guarding real money.

**Required change:** add pytest coverage for the five calculators above **before** implementing
EX-1/RM-3/RM-4 so the behavior changes are assertable.

---

## Suggested implementation order (separate sessions)

| Session | Items | Rationale |
|---------|-------|-----------|
| 1 | PR-1, PR-2, EX-6 | Prompt-registry bug + stale tool refs; quick, high impact, low risk |
| 2 | CQ-6, EX-1, RM-3, EX-5 | Tests first, then the confidence/prioritization math changes |
| 3 | RM-1, RM-2, RM-4, RM-5 | Classic risk manager correctness + perf |
| 4 | PR-3, PR-4, PR-5, PR-6, PR-7, PR-8 | Prompt quality pass on TradingAgents framework |
| 5 | RM-6, RM-7, CQ-4 | Smart Risk Manager cleanup/split |
| 6 | EX-2, CQ-3 | Senate/rating experts base-class extraction |
| 7 | EX-3, EX-4 | PennyMomentumTrader prompt fix + module split |
| 8 | CQ-1, CQ-2, CQ-5, RM-8 | Hygiene sweep |

Each session should end with: tests passing (`pytest`), version bump per CLAUDE.md, and a focused commit.
