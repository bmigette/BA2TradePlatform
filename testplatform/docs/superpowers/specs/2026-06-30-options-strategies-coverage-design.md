# Options Strategy Coverage + Options Grid — Design

**Date:** 2026-06-30
**Goal:** Make the options-trading experts able to run all the tastytrade
"Strategy" menu structures (screenshot), validate them with a real optimization
job, and ship a reusable options test grid (FMPRating × option strategies).

## Background

The backtest engine already supports option entry actions (single + multi-leg,
parent+child orders via `submit_option_order`, per-leg `ratio_qty`, at-expiry
exercise/assignment, short-premium buying-power reserve). Strike selection uses
**percent-OTM + DTE** only — the Alpaca options cache has **no historical
greeks/IV**, so delta selection is unsupported.

### Gap analysis vs. the screenshot (11 structures)

| # | Strategy | Engine today | Status |
|---|---|---|---|
| 1 | Option (Long Call/Put) | `buy_call` / `buy_put` | ✅ |
| 2 | Covered Call | `sell_covered_call` | ✅ |
| 3 | Vertical | bull-call / bear-put / bear-call spreads | ✅ |
| 11 | Stock | equity `buy` | ✅ |
| 7 | Strangle (short) | `open_strangle` is **long-only** | ⚠️ add short |
| 8 | Straddle (short) | `open_straddle` is **long-only** | ⚠️ add short |
| 9 | Iron Condor | — | ❌ new (4 legs) |
| 10 | Jade Lizard | — | ❌ new (3 legs) |
| 5 | Butterfly | — | ❌ new (1-2-1) |
| 6 | Ratio Spread | — | ❌ new (1-2) |
| 4 | **Calendar** | — | **OUT OF SCOPE** (two expiries; separate follow-up) |

**Scope (user-confirmed):** all 6 gaps **except Calendar**.

## Decisions (user-confirmed)

- **Cadence:** daily entry (`--run-schedule daily`); exits/assignment already run
  per fill-bar.
- **Execution clock:** `--interval 1d` (option cache bars are daily → daily fill
  clock matches the data and is fast).
- **Grid expert:** **FMPRating only** (rule-driven; FactorRanker bypasses
  rulesets so options can't attach without new code — dropped).
- **Test data:** 10 liquid mega-caps, **most-recent fully-available ~3-month
  window** (detected at options-cache build time; cache is Feb-2024+).
- **Grid set:** the **full set of 10** structures (4 already-supported + 6 new).
- **Success criterion:** for each strategy, the GA's best individual has
  `total_return > 0` (net of the default cost model). If a strategy stays
  negative: retry a few times, verify it is not a bug, try a different cached
  window; if still negative, report it for revisit. No forcing.

## Architecture

Mirror the existing **one explicit action class per strategy** pattern
(`packages/common/ba2_common/core/TradeActions.py`, subclassing
`_OptionEntryAction`). No generic parameterized multi-leg action (keeps optimizer
gene-mapping, rule UI, and `rule_builders` clean, and matches the CLAUDE.md
"explicit names over string params" convention).

### 1. New action types

Add to `ExpertActionType` (`packages/common/ba2_common/core/types.py`) and as
classes in `TradeActions.py`. Sign convention (existing): net limit `>= 0` =
debit (net buy), `< 0` = credit (net sell). Buy legs priced at **ask**, sell legs
at **bid**.

| Action type | Legs (ratio) | Strike selection | Net |
|---|---|---|---|
| `open_short_straddle` | SELL call + SELL put @ ATM (1,1) | `percent_otm`=0 | credit |
| `open_short_strangle` | SELL OTM call + SELL OTM put (1,1) | `strike_param` %OTM each side | credit |
| `open_iron_condor` | SELL OTM put + BUY wing put + SELL OTM call + BUY wing call (1,1,1,1) | short=`strike_param` %OTM; wings = short ± `wing_width_pct` | credit, defined risk |
| `open_jade_lizard` | SELL OTM put + SELL OTM call + BUY wing call (1,1,1) | short=`strike_param` %OTM; call wing = +`wing_width_pct` | credit |
| `open_call_butterfly` | BUY call + SELL 2 call + BUY call (1,2,1) | center ATM (`strike_param`≈0); wings ± `wing_width_pct` | debit |
| `open_put_ratio_spread` | BUY put + SELL 2 put (1,2) | long = near `strike_param` %OTM; short = further OTM (+`wing_width_pct`) | credit/even |

Each subclass:
- `_action_type_value()` returns its enum value.
- `_build_and_submit()`: fetch chain(s), select strikes (single expiry — reuse
  `select_single` + a new `select_wing` helper), pin all legs to the **same
  expiry** (as the existing straddle does for its put leg), build `OptionLeg`s
  with correct `side` / `position_intent` / `ratio_qty`, compute net premium,
  size, compute reserve, `_submit_option_order(..., option_strategy=<tag>,
  option_reserve=<reserve>)`.

### 2. Strike selection helper

`option_selector.py`: add
`select_wing(chain, *, center_strike, width_pct, direction, ...) ->
OptionContract | None` — picks the contract nearest `center_strike * (1 ±
width_pct/100)` (direction = farther-OTM call up / put down), within the chosen
expiry and liquidity filters. Reuses `_candidates` / nearest-strike logic.

### 3. Sizing & buying-power reserve

- **Debit structures** (long butterfly): size on net debit via existing `_size`.
- **Credit / naked-leg structures** (short straddle/strangle, ratio): net premium
  is negative, so size on the **reserve** like `SellCashSecuredPutAction` —
  `quantity = floor(virtual_equity * sizing% / reserve_per_contract)`.
- **Reserve** (`OptionsAccountInterface.option_reserve_required`, extend the
  `strategy` switch):
  - `iron_condor`, `call_butterfly`, `jade_lizard` (defined risk):
    `max_loss × 100 × qty` where `max_loss` = wing width − net credit
    (butterfly = net debit, max_loss = debit).
  - `short_straddle`, `short_strangle`, `put_ratio_spread` (undefined/naked):
    conservative `strike × 100 × qty` per naked short leg proxy.

### 4. Rule plumbing & optimizer genes

- `rule_builders.action_from_rule`: recognize the new action-type strings;
  forward the existing option params + the new `option_wing_width_pct`
  (alias `option_wing_width`).
- Optimizer (`strategy_param_space.py` / wherever `exit:<id>:option_dte` etc. are
  emitted): add an `option_wing_width` gene next to `option_strike_param` /
  `option_dte`. The optimizer already injects the options provider when the
  decoded strategy uses option actions — no new wiring there.

### 5. Launcher strategy builders

`ba2test_launcher.py`: register option-strategy builders in `_STRATEGY_BUILDERS`,
each an FMPRating `Strategy` with a **buy_entry** ruleset whose action is the
option action (optimizable `option_strike_param` / `option_dte` / `option_wing_width`
genes, plus the existing confidence/expected-profit entry gates) and an **exit**
ruleset = "close at +50% premium profit" + a time/DTE exit. Keys:

`O_LC` long call · `O_CC` covered call · `O_VERT` vertical (bear-put) · `O_STK`
stock · `O_SSTG` short strangle · `O_SSTD` short straddle · `O_IC` iron condor ·
`O_JL` jade lizard · `O_BF` butterfly · `O_RS` ratio spread.

(Covered call `O_CC` needs an equity long first — its entry buys stock then sells
the call, matching `sell_covered_call`'s held-shares requirement. If that proves
awkward in the grid, `O_CC` may pair the equity buy + covered-call as a two-rule
entry; resolved in the plan.)

### 6. Tests (TDD)

- Per-action unit tests (`packages/common/.../tests/` style of
  `tests/test_option_actions.py`): assert legs (count, side, `ratio_qty`,
  `option_type`, strike ordering), net premium sign (credit vs debit), reserve.
- Multi-leg e2e backtest-fill test (`testplatform/backend/tests/backtest/`):
  iron condor + butterfly fill against a fixture chain → positions + equity mark.

### 7. Options cache + run + grid script

- **Cache:** `ba2-test fetch-options --underlyings <10 syms> --start <S> --end <E>`
  over the detected recent ~3-month window.
- **Opt job (validation):** per strategy, small GA (FMPRating, daily cadence, 1d
  clock, modest pop/gen) → confirm fills happen + GA produces results + target
  `total_return > 0`.
- **Grid script:** `testplatform/scripts/run_options_grid.sh`, modeled on
  `run_phase1_grid.sh` but: builds the **options cache** (not just OHLCV),
  `EXPERTS=FMPRating`, `STRATEGIES=O_LC,O_CC,O_VERT,O_STK,O_SSTG,O_SSTD,O_IC,O_JL,O_BF,O_RS`,
  `--run-schedule daily`, `--interval 1d`. Env-overridable knobs (START/END/
  UNIVERSE/POPULATION/GENERATIONS/PARALLEL/FITNESS).

## Out of scope

- **Calendar spreads** (multi-expiry fill/cache support) — separate follow-up.
- **Delta-based** strike selection — blocked by cache (no historical greeks).
- FactorRanker options overlay.

## Risks

- **Data quality:** cache premium is a zero-spread proxy off daily contract bars;
  credit strategies are sensitive to it. Mitigated by reporting honestly and the
  retry/alt-window policy.
- **Liquidity filters** (min OI / max spread) may zero out fills on thin chains —
  start permissive, tighten only if fills look unrealistic.
- **Covered-call grid wiring** (needs equity leg) — flagged above.
- **Reserve proxy** for naked legs is conservative (may under-size) — acceptable
  for a validation run; documented.
