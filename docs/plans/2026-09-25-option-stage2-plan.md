# Option Grid Stage 2: Composition on the Full Large-Cap Universe (Plan)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development)
> to implement the build tasks task-by-task. Nothing here launches until the user says so.

**Goal:** 2-3 deployable option experts, each a composition of the structures of ONE trading style.
Each expert is searched on the FULL large-cap universe with the market-condition gates on, and seeded
from the stage-1 (and LEAPS-grid) winners.

**Status (2026-09-25):**

- **Configured, not built.** Stage 1 (`-st1fix`, relaunched 2026-09-25 04:32Z on dev `3b3465c8`) is
  running on the 97-name universe.
- **Seeding is decided after stage 1:** the user will adjust it once the stage-1 results are in.
- **Design source:** `docs/superpowers/specs/2026-08-27-option-ga-grid-design.md` §5 "Stage 2 ·
  Composition" and §7 / §7.1 (the build list and what does not exist yet). This plan layers the
  decisions taken since then on top of that spec.

---

## 1. Decisions already taken (do not re-litigate)

| Topic | Decision | Source |
|---|---|---|
| Shape | **2-3 STYLE experts, NOT one composition** (user, 2026-09-25, overriding spec §5's single `OS_ALL` job). Each expert is a composition of the structures of one style, e.g. **short-term** directional, **mid-term**, **LEAPS / convex**, **pure premium** (credit/income). The final grouping is decided after stage 1 AND the LEAPS grid. Within an expert there is no survival gate: every structure of that style is a member, and stage-1 results only seed it. | user 2026-09-25; spec §5 |
| Universe | **Full large-cap universe, `tools/options_universe_large_cap.txt` (820 names)**, not the 97-name `options_universe_top100.txt`. Stage 1 finds the structures on 97 names; stage 2 composes them on everything. | user, 2026-09-25 |
| Market conditions | ON, both profiles (`ohlcv-v1,ta-structure-v1`), pinned manifests REBUILT for the 820-name universe. | user, 2026-09-23 ("keep in stage 1, we'll see at the end") + 2026-09-25 |
| Engine | dev `3b3465c8` or later: split-basis lots + re-key, daily stepping while holding, fixed DD refinement, structure-level concentration, ledger checks, close by transaction id. | engine-fix plan 2026-09-24 |
| Stage-1 option flags, carried over | `option_size_within_fill_volume` ON; `option_min_one_contract` ON for every cost-sized structure except O_CONVEX*; `option_entry_cross` gene 0.75-1.0; `rel_volume` gene ≤ 1.5; debit IV/RV floor ≥ 1.0; DeterministicScorer `macro_short_side="mirror"` on option jobs. | user decisions 2026-09-24 |
| Fitness | `option_car_target_soft30`, robust ON (concentration on structures, share lots joined with overlays). | stage 1 |
| Expert | DeterministicScorer (stage 1 runs DS only). Whether FMPRating joins is open (§5). | stage 1 |
| Bearish members | O_LP / O_BEARCS stay members like everything else (no survival gate). The stage-1 "hold puts if no viable genome by ~gen 10" rule governs STAGE 1 only. | user, 2026-09-24 |
| Window | 2020-01-01..2025-12-31 (the same as stage 1). | stage 1 |
| Capital | $20k account, virtual equity 100%. | spec §6 |
| Deploy / forward test | The 8082 forward test keeps running until a better setting exists. Tag every deployed backtest `ForwardTest`. Check concentration (top1/top5) before any deploy. | user; memory |

## 2. Data prerequisites (check before building the manifests)

Measured 2026-09-25:

- **ThetaData option cache:** 819 of the 820 names locally AND on remote227
  (`/home/debian/ba2-grid/home/common/cache/ThetaDataOptionsProvider`, 18 GB). Expiries run
  2020-01-03 .. 2026-09-11. Identify the one missing symbol, then fetch it or drop it.
- **FMP daily OHLCV:** 820 of 820 locally and on remote227.
- **Still to check:**
  1. **Split-basis preflight across all 820.** The stage-1 repairs (CRWD refetch, META←FB root,
     HON/NVS/SCCO overrides) covered only the 97 names. Run the split-calendar preflight and the
     `option_basis_guard` sample over all 820. Repair (refetch/override) or explicitly exclude every
     name the guard refuses. Never exclude silently: record the reason per name.
  2. **DeterministicScorer inputs for 820:** the fundamentals, statements and macro inputs the DS
     sections read, as-of and cache-only, 2020-2025. Count the misses per section.
  3. **Screener gate store** (`--screener-gate-store`, the metric_store) covers the 820 names over the
     window.
  4. **Dead chains:** SHEL, TTE, SAN, SMFG, MUFG and UBS had empty or illiquid chains in stage 1
     (O_LP diagnosis). Measure chain liquidity per name over 820. Keep them (the gates and the
     fill-volume sizing handle illiquidity), but report the count.
- **Market-condition manifests for 820:** `tools/warm_market_conditions.py plan/build/verify/
  prepare-host`, per profile, for the 820-name universe and the stage window.
  - Build on the LOCAL box and sync to remote227 (user, 2026-09-23: fetch/build locally, sync).
  - Record both new digests here.
  - The 97-name digests `ca4d65d4…` / `2b6e8ca8…` stay for stage 1.

## 3. What has to be built (from spec §7 / §7.1, still open)

1. **Style groups, 2-3 of them** (e.g. `OS_SHORT`, `OS_MID`, `OS_LONG` for LEAPS/convex,
   `OS_PREMIUM`), each carrying the structures of one style; the membership is decided after stage 1
   and the LEAPS grid. Candidate split, to confirm then:
   - **short-term directional (debit):** O_LC, O_LP, O_VERT, O_BULLCS, O_BF, at short DTE;
   - **mid-term:** the same directional families at longer DTE, O_STRD/O_STRG;
   - **LEAPS / convex:** O_LEAPC, O_LEAPP, O_PMCC, O_CONVEX*;
   - **pure premium (credit / income):** O_CSP, O_BULLPS, O_BEARCS, O_IC, O_JL, O_RS, O_WHEEL, O_CC.

   What spec §7.1 says about `OS_ALL` applies to each style group:
   - It must inherit `hold_assigned_stock` (the wheel member).
   - Per-member exits: today exit condition ids (`tp`, `td`, `dte`, `sl`) are unprefixed, so
     per-member exits cannot be expressed, and `_option_exit_rules(kind)` would give DEBIT members
     CREDIT bands because it keys on the group.
   - Fix/replace the two live tests that assert the opposite (e.g. O_CSP/O_JL/O_RS in no group).
   - Measure each group's real genome with `collect_param_space` first. The spec's ~240 was for ALL
     structures; each style group is smaller.
2. **Multi-source cross-job seeding:** `warmStartFromOptimizationId` is a single int. Stage 2 needs
   N sources merged.
   - `genetic.py` fills absent genes with `config['min']`, so an `enabled` toggle would seed to 0
     and the seeded individual would trade nothing. Seeding must set each seeded member's toggle
     ON and leave un-seeded members at their authored defaults.
   - The key translation per member (`cond:o_lc-…` → the member-prefixed id in OS_ALL, and the
     `shared-*` ids) must be explicit. `encode_params` drops unknown keys silently.
   - **Seeding policy is decided after stage 1 (user).** Build the mechanism with the policy as
     input: which jobs, which ranks (distinct TOP-N), per-member or per-job, narrowing ranges
     around seeds (spec §8.1).
3. **Stage-2 driver entry:** `run_options_matrix.py --profile composition` (or a
   `tools/stage2_run.sh` mirroring `stage1_run.sh`).
   - It carries every stage-1 flag in §1, the 820-name universe file, the new manifests, and the
     seeds.
   - The job name digest covers the universe, the manifests, the flags and the seed set, so a
     re-run can never resume a different configuration.
4. **Wheel / overlay composition checks** under OS_ALL: the covered-call guard, the assigned-stock
   hold, and the share-lot + overlay concentration grouping.
5. **Per-year and concentration report** at the end: per-year returns (2020 and 2022 especially) and
   top1/top5 concentration on structures. The soft30 consistency floor is ×0.25, so a single good
   year can carry a score.

## 4. Cost estimate (to be MEASURED in a pilot before launch)

- **Stage 1 on 97 names:** about 110-130 min/generation at 28 slots on remote227. Gen 1 of `-st1fix`
  O_LC took about 130 min.
- **Per-trial cost scales with symbols:** chain reads and IV/greeks are 40-75% of a trial. So 820
  names are roughly 8× per trial, before the composition's extra members.
- **Rough order** (pop ~300, gens ≤80, patience 10): about 11 batches of 28 per generation × ~20+ min
  = several hours per generation, so **1-3 weeks per style job**. The 2-3 jobs run one after another
  on remote227, or split across hosts. That is feasible, but it must be measured.
- **The pilot:** 1 generation, pop 28, 2023-2025 window, on remote227 while stage 1 runs (or at a
  stage-1 job boundary). Measure trial time, peak RSS per slot, and the shared-array/preload
  footprint for 820 names.
  - The 2020 ThetaData tree OOM'd at 20 slots for 98 names on 2026-09-14 before the shared arrays
    landed. Size `--parallel` from the measured RSS.
- **If it is infeasible, the fallback (user decides):** the 200-300 most option-liquid names from
  the 820, or stage 2 on the 97 names plus validation of the winner on all 820.

## 5. Open decisions (for the user, after stage 1)

1. **Seeding policy:** which stage-1 jobs/ranks seed which members, and whether to narrow gene
   ranges around seeds.
2. **Style grouping and membership** (after stage 1 + the LEAPS grid), then the population and
   generations per group after measuring each genome (spec reference for all structures: 300 / 80 /
   patience 10).
3. **Experts:** DS only, or also FMPRating (it has no bearish signal on large caps, per memory).
4. **Universe fallback** if the pilot says 820 is too slow.
5. **Deploy route** for the stage-2 winner (8082 forward test first).

## 6. Order of work

1. Now, in parallel with stage 1, on the local box: §2 data preflight (split basis, DS inputs,
   screener store, the missing ThetaData name) and the 820-name market-condition manifests (build
   locally, sync, record the digests).
2. Now, in a worktree: the §3 builds 1-4 with TDD, including the seeding MECHANISM with the policy
   as input.
3. At a stage-1 job boundary: the §4 pilot on remote227.
4. After stage 1: the user sets the seeding policy and the §5 decisions, then launch.
