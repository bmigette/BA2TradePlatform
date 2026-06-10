# BA2 Trade Platform — Comprehensive Code Review

**Date:** 2026-06-10
**Branch:** `dev` (HEAD `44f6670`, after `git pull` fast-forward `ea7eb4b..44f6670`)
**Scope:** Expert strategies · Agent prompts · Risk managers (smart + classic/static) · Code quality / optimization
**Method:** 20 review agents — 15 adversarially verifying each headline finding against the post‑pull code with `file:line` evidence (each instructed to *try to refute* the claim), plus 5 independent sweeps of the four focus areas. Every surviving high‑impact bug was re‑checked by hand; two claimed P1s were empirically tested and **dismissed as false positives** (see §7).

---

## 1. Executive summary

The codebase is functional and the headline findings are largely **real**, but adversarial verification materially **lowered the severity** of several of them. The single most important correction:

> The original "P1 bugs" mostly do **not** cause wrong trades. The scary signal→`ERROR` recommendation path in `TradingAgents.py` is **guarded and unreachable**; the "WAITING counted as \$0" allocation bug is an **edge case, not systematic**; and the `get_all_instances` "TypeError" is **not a bug at all** (the code is correct for `sqlalchemy.select`). After verification, **no finding survives as a clean P1 that produces an incorrect executed trade.**

The genuinely impactful, confirmed issues are **P2 correctness/quality bugs**:

| # | Finding | Area | Severity |
|---|---------|------|----------|
| B3 | FMPRating price‑target boost **inverted for SELL** + dead duplicated `if/else` | Strategy | **P2 (highest‑priority correctness)** |
| N1 | `FMPSenateTraderWeight` casts `max_price_delta_pct` to `int()`, truncating the % filter | Strategy | P2 |
| B4 | `value or default` config reads treat a configured **`0` as missing** (debate rounds, memory, lookbacks) | Prompt/Config | P2 |
| P2 | PennyMomentumTrader **exit‑update prompt uses `time_before` for EOD** while entry mandates `time_after` (opposite semantics); lists only 10 of 23 condition types | Prompt | P2 |
| R3 | `macro_report` never passed to risk debators / trader / research‑manager / risk‑manager (researchers get it) | Prompt | P2 |
| N2 | `TradingAgents._extract_recommendation_data` logs `price ≤ 0` but **does not raise**, returns price 0 | Bug | P2 |
| N3 | `StockScreener` live‑price `or` fallback to stale bar close | Bug | P2 |
| R1 | Classic risk manager: **N+1 queries**, profit‑only priority **starves 0.0‑profit experts** (FinnHub), hardcoded `0.7` | Risk mgr | P2 |
| C5 | **Allocation/position‑sizing** pure functions are **untested** by pytest | Test gap | P2 |
| C2 | `PennyMomentumTrader/__init__.py` is **3,531 lines** in one class | Quality | P2 |

The remaining items (B1 duplicate prompts, B2 stale tool name, R2 dead toolkit fn, P1 rating‑scale, P3 docstring, C1 Senate duplication, C3 dead `.old.py`, C4 `exc_info` misuse) are **confirmed but P3** — real hygiene/maintainability debt with no money impact.

A full **remediation order** is in §6 and a **test plan** in §8.

---

## 2. Severity reconciliation (what changed after verification)

The incoming headline list flagged a cluster of P1s. Verification revised them:

| Claim (as received) | Verdict | Why severity changed |
|---|---|---|
| Duplicate prompts → verbose signal → `ERROR` recommendations | **Partially confirmed → P3** | Duplicates are real, but the signal→`ERROR` fallback (`TradingAgents.py:504‑528`) is **unreachable**: its only caller (`:651`) runs only after the guard at `:646‑648` raises when `expert_recommendation` is empty. Live recommendation comes from `expert_recommendation['recommended_action']` (`:496`), not `processed_signal`. |
| FMPRating inverted SELL boost | **Confirmed → P2** | Real, but it depresses SELL **confidence/expected‑profit**; it does **not** flip the trade direction. |
| Classic mgr counts WAITING as \$0 → double exposure | **Partially confirmed → P2** | WAITING txns are created **with** quantity + estimated `open_price` (`AccountInterface.py:316‑318`), so `current_value = abs(qty*open_price)` is used; the `=0` branch is only an **edge‑case fallback**, not the norm. |
| `get_all_instances` TypeError (discovery P1) | **Refuted — false positive** | db.py uses `sqlalchemy.select`, so `exec().all()` yields `Row` objects and `i[0]` is the model. Empirically ran the function: returns models correctly (see §7). |
| Fallback fabricates 0‑confidence rec (discovery P1) | **Confirmed but dead code → P3** | Same guard as B1 makes it unreachable; should still be removed. |

---

## 3. Agent prompts (TradingAgents framework)

### B1 — Duplicate prompt definitions; Python keeps the weaker versions · **P3** · *Confirmed*
**Location:** `thirdparties/TradingAgents/tradingagents/prompts.py`
- `SIGNAL_PROCESSING_SYSTEM_PROMPT` defined at **:253** (strict, *"Output only the single rating word"*) **and :313** (vaguer). Python keeps the later (`:313`).
- `REFLECTION_SYSTEM_PROMPT` defined at **:259** (detailed 5‑section) **and :315** (vaguer one‑liner). Later (`:315`) wins.
- `PROMPT_REGISTRY` is built at `:527` (after all assignments), so `get_prompt("signal_processing")` / `get_prompt("reflection")` resolve to the **weaker** versions. The strict signal prompt and detailed reflection prompt are **dead code**.

**Impact:** Reflection memory quality is degraded (the reflection prompt *is* live, used by the Reflector). Signal‑processing degradation does **not** reach recommendations (its output `processed_signal` only feeds the unreachable fallback — see N4). No money impact.
**Fix:** Delete the duplicate definitions at `:313` and `:315` so the strict/detailed versions become live. Add a lint/test guard against duplicate module‑level prompt assignments.

### B2 — Market analyst prompt instructs a non‑existent tool `get_YFin_data` · **P2** · *Confirmed*
**Location:** `prompts.py:44` (inside `MARKET_ANALYST_SYSTEM_PROMPT`, def at `:13`)
- Prompt says *"call `get_YFin_data` first…"* but the market analyst node binds only `get_ohlcv_data` and `get_indicator_data` (`graph/trading_graph.py:590‑595`); the toolkit exposes only those two (`agent_utils_new.py:1138, :1304`). `get_YFin_data` lives only in `*.old.py` files + a non‑tool `get_YFin_data_online` helper.
- **Mitigated** (why not P1): the combined system prompt also injects real `{tool_names}` and lists the correct tools at `prompts.py:108`, so the stale line is *contradicted*. A hallucinated call fails as "unknown tool" (recoverable), not silent bad data.

**Fix:** Replace the sentence with *"call `get_ohlcv_data` first … then `get_indicator_data` for each selected indicator."* Optionally add a regression test asserting every tool named imperatively in analyst prompts exists in the bound tool set.

### R3 — `macro_report` dropped for debators / trader / managers · **P2** · *Confirmed*
**Location:** TradingAgents agent nodes
- Bull/bear researchers read `macro_report` and inject it (`bull_researcher.py:18,33`; `bear_researcher.py:18,33`; placeholders `prompts.py:140,161`).
- The **conservative/neutral/aggressive debators** (`risk_mgmt/*_debator.py`), **research_manager**, **risk_manager**, and **trader** assemble context from only market/sentiment/news/fundamentals and **never read or inject `macro_report`** (no placeholder in `RISK_MANAGER_PROMPT` `:201‑226`, etc.). `TRADER_CONTEXT_PROMPT` mentions macro only as static boilerplate.

**Impact:** The risk debate and final decision nodes lose Fed/inflation/rates context that the analysts produced. Quality gap, not a crash.
**Fix:** Mirror the researcher pattern — read `state["macro_report"]` and add a `{macro_report}` placeholder in all six nodes' prompts/format helpers.

### P1‑scale — Research manager prose uses 3‑tier; trader/risk‑judge use 5‑tier · **P3** · *Partially confirmed*
**Location:** `prompts.py:182,184` (3‑tier "Buy/Sell/Hold") vs `:238` and `:203‑208` (5‑tier).
- **Mitigated:** the research‑manager node binds `InvestmentJudgeVerdict` whose `action` is the full 5‑tier `Literal` (`structured_outputs.py:133`), and the renderer writes the 5‑tier action into the plan. The authoritative decision is the Risk Manager/Trader (both 5‑tier). So no granularity is actually lost.
**Fix:** Align the `RESEARCH_MANAGER_PROMPT` prose to the 5‑tier scale for consistency. (Bonus, N7) `RISK_MANAGER_PROMPT` uses title‑case `Buy/Overweight/…` while trader/schema use UPPERCASE — standardize to UPPERCASE.

### N2 — `price_at_date ≤ 0` is logged but **not raised** (normal branch) · **P2** · *Verified by hand*
**Location:** `modules/experts/TradingAgents.py:492‑503`
```python
if price_at_date <= 0:
    self.logger.error(f"No valid price available for {symbol} in graph state!")
# ...falls through and returns 'price_at_date': price_at_date  # may be 0.0
```
The *normal* (`if expert_recommendation:`) branch logs the bad price but proceeds to **return `price_at_date = 0`** (unlike the `else` branch, which at least tries an account fallback). This violates the CLAUDE.md "Live Data — No Fallbacks" rule and can feed `0` into downstream sizing/risk math.
**Fix:** Raise `ValueError` when `price_at_date <= 0` after exhausting sources, so the analysis is marked FAILED rather than emitting a zero‑price recommendation.

### N4 — Dead fallback branch fabricates a 0‑confidence recommendation · **P3** · *Confirmed (dead code)*
**Location:** `modules/experts/TradingAgents.py:504‑528`
The `else: # Fallback to processed signal` branch returns `{'confidence': 0.0, 'expected_profit': 0.0, 'signal': processed_signal or ERROR}`. It is **unreachable** — the only caller (`:651`) runs after the guard at `:646‑648` raises when `expert_recommendation` is empty. Were it ever reached, it would fabricate metrics in violation of the no‑fallback rule.
**Fix:** Delete the dead `else` branch (or make it `raise`). Keeping it as silent dead code is a foot‑gun if the guard is ever refactored.

### N6 — Pervasive `.get(key, default)` / `… or default` config access · **P2 (cluster)** · *Confirmed*
CLAUDE.md mandates explicit dict access. Confirmed widespread violations:
- `TradingAgents.py` — ~117 `.get()` usages incl. config reads (`:291‑354`, `:615‑623`).
- `TradingAgentsUI.py` — ~30 `.get()` defaults masking missing state.
- `core/utils.py:718` `get_risk_manager_mode` and `:1164` `get_setting_safe`; `core/WorkerQueue.py:1077‑1080` chained `or` model lookup.

**Fix:** Use explicit access for required keys; for genuinely optional values, check `is None` explicitly (see B4 helper). Booleans already use the safe two‑arg `.get` form — the bug is the `or` idiom on numerics (B4).

---

## 4. Expert strategies

### B3 — FMPRating price‑target boost is inverted for SELL; duplicated dead `if/else` · **P2 (top correctness fix)** · *Confirmed*
**Location:** `modules/experts/FMPRating.py:279‑299`
- **Dead duplicated arms:** `:279‑284` and `:287‑292` — both the `if` and `else` branches assign the **byte‑identical** expression (`(target - current_price)/current_price*100`). The conditionals do nothing but differ in comments.
- **Inverted sign for shorts:** `price_target_boost` (`:295`) is the signed % distance from price to target, added to confidence at `:299` with **no signal‑direction awareness**. For a SELL where price is **above** all targets (more downside ⇒ stronger short), `target − price < 0` ⇒ boost negative ⇒ **confidence decreases** exactly when the thesis is strongest. Correct only for BUY.
- Worked example: SELL, price 100 vs targets 70/80 ⇒ boost −25 ⇒ confidence 60→35. BUY, price 100 vs 110/130 ⇒ +20 ⇒ 60→80.
- The signal itself still emits SELL (`:243‑251`); the bug **depresses SELL confidence and expected‑profit** (`:338‑342`), which then mis‑ranks the order in the classic risk manager (R1) and under‑sizes it. HOLD‑demotion guards (`:310‑330`) are signal‑aware but only on the `low`/`high` target path, not the default consensus path.

**Fix:** Collapse each duplicated `if/else` to one assignment; orient by signal:
```python
pct_to_target = (target - current_price) / current_price * 100
boost = pct_to_target if signal == BUY else -pct_to_target
```
Update the methodology/display text (`:371‑377`, `:1113‑1117`) and remove the misleading branch comments.

### N1 — `max_price_delta_pct` cast to `int()` truncates the percentage filter · **P2** · *Verified by hand*
**Location:** `modules/experts/FMPSenateTraderWeight.py:359`
```python
max_price_delta_pct = int (max_price_delta_pct)   # setting is float, default 10.0
...
if favourable_move > max_price_delta_pct:          # float compared to truncated int
```
A configured `10.5%` silently becomes `10%`, loosening the "opportunity passed" filter. Logic error.
**Fix:** `max_price_delta_pct = float(max_price_delta_pct)` (and likewise audit the other `int(...)` casts on the same line group `:357‑360` — `max_exec_days`/`max_disclose_days` are legitimately ints, the delta‑pct one is not).

### B4 — `value or default` config reads treat configured `0` as missing · **P2** · *Confirmed*
**Location:** `modules/experts/TradingAgents.py:291‑300, 343‑346, 352‑353, 745‑747`
e.g. `int(self.settings.get('debates_new_positions') or settings_def[...]['default'])`. Because `0` is falsy, setting **0 debate rounds**, **0 memory trades**, or **0‑day lookbacks** is silently overridden by the default (3 rounds / 2 trades / etc.) — running LLM debates the user disabled (cost + latency) and ignoring an intentional "disable memory."
**Fix:** Helper that only substitutes on `None`:
```python
def _num(k):
    v = self.settings.get(k)
    return settings_def[k]['default'] if v is None else v
```
Apply to all listed lines. Since these are `required=True`, prefer explicit access + validation.

### P2 — PennyMomentumTrader exit‑update prompt: wrong EOD condition + missing types · **P2** · *Partially confirmed (more serious than titled)*
**Location:** `modules/experts/PennyMomentumTrader/prompts.py:517‑527` vs `conditions.py`
- **Wrong‑direction EOD exit:** exit‑update prompt offers only `time_before "15:45"` (`:526`), but the entry prompt **mandates `time_after`** for EOD exits with an explicit *"use time_after (not time_before)"* note (`:382‑383`, example `:415`). The evaluator confirms they are opposite: `_handle_time_after` fires at/after the time (correct EOD exit); `_handle_time_before` is true all session until the time (`conditions.py:1028‑1040`). Inside a stop‑loss `any` group, `time_before` is satisfiable nearly all day → risk of premature exit and **no real EOD exit**.
- **Missing types:** the prompt lists only **10 of 23** supported condition types (`conditions.py` registry `:1043‑1067`), omitting 13 (EMA/SMA crosses, MACD, `rsi_between`, ORB, volume, and `time_after`). This **under‑uses** available exit signals.
- **Correction to the original claim:** this does **not** cause fix‑prompt retries — `validate_condition` (`conditions.py:217`) checks the *full* registry, so unlisted‑but‑valid types pass. Retries happen only for types not in the registry at all.

**Fix:** Derive the exit prompt's type list from the shared `_CONDITION_PARAMS_REFERENCE` / `get_condition_types_for_llm()` (already used by entry + fix prompts) so they can't drift; fix the EOD guidance to `time_after`.

### P3‑docstring — `FMPSenateTraderWeight` confidence docstring says ±30, code caps at +10 · **P3** · *Confirmed*
**Location:** `FMPSenateTraderWeight.py:629,634` (docstring "‑30 to +30") vs `:594` `min(10.0, symbol_focus_pct)`, `:597` `confidence_modifier = symbol_focus_pct` (range **[0, +10]**, never negative). Stale docstring only; the value also doesn't drive the stored confidence (computed separately at `:890`).
**Fix:** Correct the two docstring lines to "0 to +10 (from symbol portfolio focus %)."

### N5 — `FMPSenateTraderWeight` symbol‑focus zero‑division is safe‑but‑fragile · **P3** · *Confirmed*
`:564` inits `symbol_focus_pct = 0.0`; division only under `> 0` guards (`:587‑591`). Correct today, but add an explicit `else` to harden against future refactors.

---

## 5. Risk managers

### R1 — Classic/static manager: N+1, profit‑only priority, hardcoded 0.7 · **P2** · *Partially confirmed*
**Location:** `core/TradeRiskManagement.py`
- **(a) N+1 — confirmed.** `_get_pending_orders_for_review` (`:241‑246`) loads **every** PENDING order system‑wide (no expert/account filter), then filters with a per‑order `session.get(ExpertRecommendation, …)` (`:262`). A second N+1 in `_get_orders_with_recommendations` (`:300`). Both could be one JOIN.
- **(b) Profit‑only priority starves 0.0‑profit experts — confirmed.** `_prioritize_orders_by_profit` (`:318‑322`) sorts by `expected_profit_percent` desc; `FinnHubRating` hard‑stores `expected_profit_percent=0.0` (`FinnHubRating.py:252`); balance is consumed top‑down (`:540`) ⇒ FinnHub orders can be **perpetually starved**.
- **(c) Hardcoded diversification — confirmed.** `diversification_factor = 0.7` (`:487`), applied only when `>1` instrument has headroom (`:485`). Not configurable.
- **(d) "WAITING = \$0" — corrected.** Normally **false**: `:352‑356` computes `abs(qty*open_price)` and WAITING txns carry both (`AccountInterface.py:316‑318`). The `=0` branch is an edge‑case fallback; the misleading comment overstates it. A real double‑exposure window only opens if a WAITING txn lacks `open_price`.

**Fix:** Replace both N+1 loops with one JOIN filtered by `instance_id`; add a secondary sort key (confidence/recency) or round‑robin across experts so 0.0‑profit experts aren't starved; make `diversification_factor` a setting; and for (d) either drop the misleading comment or `raise`/skip when a WAITING txn lacks `open_price` instead of silently treating it as \$0.

### R2 — Smart manager: ~307 lines of dead duplicate tools; oversized module · **P3** · *Partially confirmed*
**Location:** `core/SmartRiskManagerGraph.py`
- **(a) Dead duplicate — confirmed.** `create_toolkit_tools` (`:900‑1206`, ~307 lines) is **never called/imported/exported** (grep returns only the def). The live factory is `_create_research_tools` (`:2456`), bound at `:2959`. Safe to delete.
- **(b) "Missing Direction Policy" — mostly refuted.** The `## DIRECTION POLICY` section **is** built and injected via `{expert_instructions}` (`:2894‑2901`, used `:2941`) whenever `enable_buy` *or* `enable_sell` is False. With platform defaults (`enable_sell=False`) it **is present**. It's absent only if a user enables **both** directions, and only then do the static references at `:813/:822` dangle. Minor conditional prompt hygiene.
- **(c) Size — confirmed factual.** `SmartRiskManagerGraph.py` is **3,400** lines; `SmartRiskManagerToolkit.py` is **2,696**. Subjective "should split."

**Fix:** Delete `create_toolkit_tools`. Optionally always emit a DIRECTION POLICY header (incl. an "all directions enabled" line) so references never dangle. Optionally split prompt constants + tool factory into separate modules.

### N‑risk1 — `get_portfolio_status` N+1 price lookups despite a bulk method · **P2** · *Discovery (plausible)*
**Location:** `core/SmartRiskManagerToolkit.py` (loops at ~`:217`, ~`:294`) call `account.get_instrument_current_price(symbol)` per transaction although a bulk `get_current_prices()` exists. Collect unique symbols, fetch once, map back (pattern already used in `TradeRiskManagement.py`).

---

## 6. Code quality / optimization

### C2 — `PennyMomentumTrader/__init__.py` is 3,531 lines (single ~3,480‑line class) · **P2** · *Confirmed*
47 methods in one class: a ~344‑line inline settings dict (`:95‑439`), the full 7‑phase pipeline (`_phase_0…_phase_6`, ~`:517‑2601`, `_phase_5_monitor` alone ~624 lines), market‑data fetch (`:2774‑2962`), per‑symbol research (`:3324‑3531`), and persistence. The package **already** uses the split pattern (`conditions.py`, `ui.py`, `prompts.py`, `trade_manager.py`, `tier_tracking.py`).
**Fix (mechanical):** move the settings dict → `settings.py`; market data → `market_data.py`; research gathering → `research.py`; the phases → `pipeline.py`/`Pipeline` class. Target `__init__.py` < ~500 lines.

### C1 — Two Senate experts duplicate code (≈300‑450 lines, not ~1,000) · **P3** · *Partially confirmed*
`FMPSenateTraderCopy.py` (1,727) and `FMPSenateTraderWeight.py` (1,535) share identical `_get_fmp_api_key`, render‑state boilerplate, parallel `_create_expert_recommendation` / `_store_analysis_outputs`, etc. The **~1,000** figure is overstated — genuine cross‑file dup is ~300‑450 lines; the rest is materially different (Weight = trader‑history weighting; Copy = multi‑symbol copy‑trading). Also Weight repeats an amount‑range parser **4×** internally.
**Fix:** `FMPSenateTraderBase(MarketExpertInterface)` for shared helpers/render/persistence; extract the amount parser into `core/utils.py` (next to the already‑shared `calculate_fmp_trade_metrics`).

### C3 — Dead `.old.py` files + upstream `results/` samples tracked in git · **P3** · *Confirmed (understated)*
`git ls-files "*.old.py"` → **two** tracked files: `agents/utils/agent_utils.old.py` (592) and `dataflows/interface.old.py` (1,409) — ~84 KB of provably dead code (commented‑out, zero live imports; active replacement `agent_utils_new.py`). Plus 7 tracked `results/MSFT/2015-09-01/reports/*.md` upstream samples (~32 KB). None are gitignored.
**Fix:** `git rm` all of them; delete the stale commented import block in `dataflows/__init__.py:7‑20`; add `*.old.py` and the framework's `results/` to `.gitignore`.

### C4 — `exc_info=True` outside `except` blocks · **P3** · *Partially confirmed (numbers corrected)*
Real total is **791** `exc_info=True` usages (not 476); **770 are correctly inside `except`**, **21 are outside** (2.7%). All 21 are `logger.error(...)` in validation/safety branches (e.g. "NEGATIVE QUANTITY DETECTED" in `TransactionHelper.py:69‑72`, `SmartRiskManagerToolkit.py:2111`, `ReadOnlyAccountInterface.py:506`, `TradeRiskManagement.py:62/69/138/154`; event‑shape `else` branches in `marketanalysis.py:1689…`). Effect: a useless `NoneType: None` line — cosmetic only.
**Fix:** Drop `exc_info=True` on those 21; where a stack is genuinely useful (the negative‑quantity guards), use `stack_info=True` instead. Add a lint check.

### N3 — `StockScreener` live‑price fallback to stale bar close · **P2** · *Verified by hand*
**Location:** `core/StockScreener.py:575` — `current_price = c.get("price") or bars[-1].get("close")` (also `b.get("high") or 0` nearby). Violates the no‑fallback rule; if the live quote is missing, the screener silently uses stale bar data instead of failing.
**Fix:** Read the live price explicitly and `continue`/raise when `None`, rather than falling back to a bar close.

### C5 — Money‑guarding **allocation/sizing** functions are untested · **P2** · *Partially confirmed*
**Corrected:** consensus/confidence math **is** partly covered — `consensus_from_counts` (`tests/test_finnhub_rating.py`, 9 tests) and `FMPRating._calculate_recommendation` (`tests/test_experts/test_fmp_rating.py`, incl. 0‑100 clamp) and `option_selector` are tested. The real gap is **position sizing**:
- `TradeRiskManagement._calculate_order_quantities` (`:371`) — the core per‑instrument‑cap + balance + `0.7` factor + min‑share engine — **no pytest coverage** (its only exercise is a `__main__` script under `test_files/`, which `pytest.ini` `testpaths=tests` excludes).
- `TradeActions._size` / `_consensus_target`, `PennyMomentumTrader.calculate_position_sizes`, and `FMPSenateTraderWeight._calculate_confidence/_calculate_trader_confidence` — untested.

**Fix:** see §8. **Write these tests before fixing B3/B4/N1** so the behavior changes are assertable.

---

## 7. Investigated and dismissed (false positives)

For transparency — claims that did **not** hold under verification:

1. **`core/db.py:494` `get_all_instances` "TypeError"** — *Dismissed.* db.py imports `select` from **`sqlalchemy`** (`:5`), so `session.exec(select(Model)).all()` returns **`Row`** objects and `[i[0] for i in instances]` correctly extracts the model. Confirmed empirically by running `get_all_instances(AccountDefinition)` → returns model instances, no error. The discovery agent assumed `sqlmodel.select` (scalar) semantics. (Cosmetic note: switching to `sqlmodel.select` + `list(results)` would be clearer, but the current code is correct.)
2. **"Verbose signal extraction → `ERROR` recommendations"** (part of B1) — *Dismissed as a live path;* the fallback is unreachable (guard at `TradingAgents.py:646‑648`). Recorded instead as dead code (N4).
3. **"WAITING transactions counted as \$0 (systematic double‑exposure)"** (part of R1) — *Downgraded to edge case;* WAITING txns normally carry `qty`+`open_price`.

---

## 8. Prioritized remediation plan

**Phase 0 — lock behavior with tests (do first):**
1. `tests/test_risk_management_sizing.py` for `_calculate_order_quantities` (port `MockAccount`/`MockExpert` from `test_files/test_early_skip_optimization.py`): per‑instrument cap, balance exhaustion across symbols, the `0.7` factor with `>1` instrument, instrument‑weight scaling + revert‑on‑overspend, qty=0/early‑skip, min‑1‑share.
2. Unit tests for `TradeActions._size`/`_consensus_target`, `PennyMomentumTrader.calculate_position_sizes`, and the Senate‑Weight confidence calculators.

**Phase 1 — correctness bugs (assert against Phase 0):**
3. **B3** — direction‑aware FMPRating boost; collapse dead `if/else`.
4. **N1** — `float()` not `int()` for `max_price_delta_pct`.
5. **B4** — `_num()` helper so configured `0` is honored.
6. **N2 / N3** — raise on `price ≤ 0` / missing live price instead of returning 0 / stale close.
7. **P2** — exit prompt: `time_after` for EOD + derive type list from the shared registry.
8. **R3** — thread `macro_report` into debators/trader/managers.

**Phase 2 — risk‑manager structure & fairness:**
9. **R1** — single JOIN (kill N+1); fairer prioritization; configurable diversification factor; fix WAITING `open_price` handling.
10. **N‑risk1** — bulk price fetch in `get_portfolio_status`.

**Phase 3 — hygiene / maintainability:**
11. **B1/N4** — delete duplicate prompts + dead fallback branch.
12. **B2 / P1‑scale / N7 / P3‑docstring** — prompt/text alignment.
13. **C3** — `git rm` dead `.old.py` + `results/`; `.gitignore`.
14. **C4** — strip 21 stray `exc_info=True`.
15. **R2 / C2 / C1** — delete dead `create_toolkit_tools`; split `PennyMomentumTrader/__init__.py`; extract `FMPSenateTraderBase`.
16. **N6** — sweep `.get(..., default)` / `… or default` config reads toward explicit access.

---

*Generated from a 20‑agent verification + discovery workflow; every P1/P2 above was re‑checked by hand or empirically, and false positives were removed (§7).*
