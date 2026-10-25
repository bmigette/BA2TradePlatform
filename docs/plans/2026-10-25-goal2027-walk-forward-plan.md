# goal2027 walk-forward in the matrix driver: implementation plan

Design: `2026-10-25-goal2027-grid-design.md` section 1.2 and 2.5, `2026-10-02-walk-forward-engine-design.md`.

## Findings that shape the plan

- `--warm-start-from <opt id>` exists in `ba2-test optimize` (`warmStartFromOptimizationId`). It seeds from the LAST
  `population` entries of the source's `all_results` (an approximation of the final generation) and the WF wrapper
  forbids it in passthrough args. It is the wrong source for chain seeding: not the final population, no
  space check, no lineage.
- A GA checkpoint is cleared on completion, and its fingerprint contains generations and population, which differ
  between fold 1 (30 generations) and folds 2-3 (15). So the checkpoint cannot carry the seed either.
- The final EVALUATED population is not stored anywhere. `GeneticOptimizer.optimize` evolves one more
  (unevaluated) generation after the last evaluated one, so even a live reference to `population` at exit is
  wrong. We snapshot the population right after each generation is evaluated and return the last snapshot.

## Design

1. **Persist.** `optimize()` returns `final_population` (genes + fitnesses of the last evaluated generation). The
   handler stores it on the completed row as `optimization_config["finalPopulation"]` together with the gene-space
   signature (name, type, min, max, step, choices per gene, in order) and the generation it was taken at.
2. **Read.** New `app/services/chain_seed.py`: `build_chain_seed_population(source_opt, optimizer, target_size,
   this_end)`. Refuses (`ChainSeedError`, the job fails loudly) when the source is not completed, has no stored
   final population (no silent fallback to `all_results`), the gene-space signature differs (names the differing
   genes), or the source window ends after this job's window ends (look-ahead). A smaller stored population is
   padded randomly through `seeded_population`; a larger one is cut to the best `target_size`. The pad count is
   logged and recorded.
3. **Lineage.** The new row gets `optimization_config["seededFrom"] = {optimization_id, name, population_size,
   seeded, filled_random, source_end, ...}`. A checkpoint-resumed job records the id/name with
   `resumed_from_checkpoint`.
4. **CLI.** `ba2-test optimize --chain-seed-from <job name>` resolves the unique completed optimization of that
   name to `chainSeedFromOptimizationId`; mutually exclusive with `--warm-start-from`.
5. **Engine.** `walk_forward.initial_population_for_fold(fold, prev=None, prefix=None)` returns a `SeedSpec` for
   fold k+1 (source = job of fold k) and asserts `prev.train_end < fold.test_start` (no look-ahead).
   `train_command` renders `--chain-seed-from`; `Plan.fold_extra_args` carries per-fold generations.
   `Thresholds.require_test_years` + `--pass-require-fold`.
6. **Driver.** `tools/wf_matrix_chain.py` (pure orchestration over the `Runner` protocol + an `OptimizeLauncher`
   callable) builds a cell's folds (FMPRating floor, `--wf-data-end` clamp), runs `-wf1..-wf3`, decides the verdict,
   runs `-refit` only for a passing cell, writes the per-cell JSON and the grid summary. A fold with a stored fold
   result and a completed train job is skipped. `run_screener_capband_matrix.py --walk-forward` builds the cell's
   base command exactly as today and hands it to the chain.

## Tasks (TDD, one commit each)

1. final population persisted (genetic + handler) - `test_wf_chain_seed.py`
2. chain seed read/validate/lineage + handler wiring + launcher flag
3. engine: seed hook, no-look-ahead, per-fold args, pass rule, report
4. folds for a cell + chain orchestration + fold cache + refit + reports - `test_wf_matrix_chain.py`
5. matrix driver flags + `run_walk_forward --pass-require-fold`
6. versions check, full runs, diff against origin/dev if any old test fails
