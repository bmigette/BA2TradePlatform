# goal2027 item 2.2: market-profile gene (implementation plan)

Design source: `docs/plans/2026-10-25-goal2027-grid-design.md` section 2.2. Branch `feat/goal2027-market-profile-gene`,
stacked on `feat/goal2027-sizing-gene` (same shape: opt-in flag, name token, canonicalisation in `decode_params` +
`canonical_trial_params`, loud refusals, export check).

## Decisions

- **Flag:** `ba2-test optimize --search-market-profile` (store_true; optimize only, like `--search-sizing-mode`;
  `optimize-batch` resets the global). Requires `--market-condition-profile ohlcv-v1,ta-structure-v1` with BOTH
  manifests pinned, `--market-condition-mode searched`, and an equity S1-S7 strategy. Refused for bypass experts
  (FactorRanker) and option kinds.
- **Gene:** `market:profile`, a `choice` gene over `["off", "ohlcv-v1", "ta-structure-v1", "both"]` (the order is a
  persisted contract: the GA stores the index). It REPLACES `market:enabled`: the launcher stamps a second rule-level
  marker (`market_profile_gene`) next to `market_master_gene`; `collect_param_space` emits `market:profile` instead of
  `market:enabled` when it is present (still the LAST key). `decode_params` refuses a genome carrying both.
- **Decode** (the one path: GA, top-N persist, re-run, export): selected profiles = {} / {ohlcv} / {ta} / both.
  A market LEAF whose field belongs to an unselected profile gets mode `off`. A market RULE (the rule-level toggled
  `--market-exit` rules) whose leaves read any unselected profile gets `enabled=0`. `off` selects nothing, which is
  exactly what `market:enabled=0` does.
- **How exits tie to profiles:** a profile owns FIELDS (`PROFILES[p].fields`). `market_exit_rules` builds each rule from
  the fields of one profile only (`mkt-exit-structure` / `mkt-stop`: `structure_state` -> ta-structure-v1;
  `mkt-exit-slope` / `mkt-tp`: trend slope + ADX -> ohlcv-v1). The decoder derives a rule's profile set from its leaves'
  fields, so a profile that is not selected cannot drive an exit. No change to `packages/`.
- **Memo canonicalisation** (`neutralise_market_profile_genes`, called from `canonical_trial_params` with a member map
  computed once per run from the strategy): for an unselected profile, the leaf `cond:<id>:mode` becomes `off` (value
  gene then goes to its anchor through the existing mode-anchor pass) and every `exit:<rid>:*` / `cond:<leaf>:*` gene of
  an unselected rule is dropped, with `exit:<rid>:enabled` = 0. Returns the SAME object when the gene is absent, so old
  jobs keep their keys.
- **Driver:** `tools/run_screener_capband_matrix.py --search-market-profile` requires the name token `mktprof`, requires
  both profiles + both manifests (via `--market-condition-profile/-manifest`), is forwarded to classic jobs only (NOTE for
  FactorRanker) and folds into the job-name digest because the token is in the argv.
- **Export:** `_derive_export_payload` refuses a row whose optimization searched `market:profile` and whose
  `strategy_params` lacks it (otherwise decode would leave every market gene as individually decoded: a different
  phenotype). The decoded rules (off leaves pruned) are what `decode_params` already feeds the export.
- Versions: testplatform only, TEST_APP_VERSION 0150 -> 0151.

## Tasks (TDD, one commit each)

1. Failing tests; `strategy_param_space`: gene space, decode, member map, neutralise, `canonical_trial_params` wiring.
2. Launcher flag, requirements, refusals, marker stamp, gene space merge.
3. Capband driver token/passthrough.
4. Export refusal, version bump, regression runs, check_package_versions.
