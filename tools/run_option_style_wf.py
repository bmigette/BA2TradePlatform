"""Walk-forward CELL for one option STYLE job (``--strategy O_STYLE``): ``tools/option_style_job.sh
--walk-forward``.

    bash tools/option_style_job.sh <style> <suffix> none [optimize overrides] --walk-forward \\
        [--wf-defer-last-test --wf-data-end 2025-12-31] --wf-top-n 3 --wf-report-dir <dir> \\
        --pass-min-oos-return 0 --pass-max-dd-mult 2.5 --pass-min-folds 2 [--dry-run]

The style job script stays the ONE place the stage-2 recipe is written (style, expert, universe,
screen, market conditions, store, spread model, capital, GA knobs). With ``--walk-forward`` among its
extra args it does not exec ``optimize`` but this driver, handing it the operator's extra args and,
after ``--``, the very command the plain job would have run (minus the seed bundle when it is the
word ``none``):

    python tools/run_option_style_wf.py <extra args> -- <python> testplatform/ba2test_launcher.py optimize <recipe>

The driver splits the extra args into its own walk-forward flags (the option matrix's vocabulary,
``run_options_matrix.add_walk_forward_flags``: --wf-train-start, --wf-test-years, --wf-data-end,
--wf-defer-last-test, --wf-first-generations, --wf-next-generations, --wf-min-trade-rows, --wf-top-n,
--wf-report-dir, the --pass-* thresholds, ...) and optimize OVERRIDES, which go after the recipe
exactly as in the plain job (argparse keeps the last value). The cell is then run by the SAME engine
as the stock grid and the option matrix (``app.services.wf_chain`` + ``run_walk_forward.RealRunner``):

    <cell>-wf1   train 2020-01-01..2023-12-31  cold start (--wf-first-generations, default 30)  test 2024
    <cell>-wf2   train 2020-01-01..2024-12-31  seeded from wf1 (--wf-next-generations, 15)        test 2025
    <cell>-wf3   train 2020-01-01..2025-12-31  seeded from wf2                                    test 2026
    <cell>-refit train 2020-01-01..2026-12-31  seeded from wf3, PASSING cells only

REFUSED, each with its reason, before anything runs:
* any seed file (``--seed-population`` -- a style job's seed bundle other than ``none`` --,
  ``--seed-genomes``, ``--seed-check-only``, ``--warm-start-from``, ``--chain-seed-from``): walk-forward
  style cells are UNSEEDED (owner 2026-11-15). Fold 1 is a cold start, each fold seeds only the next;
  a stage-1 seed bundle was chosen on 2020-2025, so it has seen the 2024 and 2025 test years;
* ``--start``/``--end``/``--generations``/``--name`` among the overrides (the chain sets them per job);
* a non-O_STYLE command, a recipe name that is not ``optm-...``;
* ``--screen-preset`` (a FIXED screen, small-account jobs 6 and 7): it has no screener genes, and the
  out-of-sample re-run's test-window universe recompute
  (``rerun_handler.recompute_static_universe``) refuses a run without screener gene ranges, so every
  cell would ERROR after training fold 1;
* ``--print-universe`` (it prints and exits; no fold could complete);
* a plan that trains into the option holdout (``ba2test_launcher._OPTION_HOLDOUT_START``) or could
  refit into it: phase A is ``--wf-defer-last-test --wf-data-end 2025-12-31``.

NAMES. ``optwf-<expert>-O_STYLE-<style>-<suffix>[-optalphavantage]-d<digest>`` (the plain job's
``optm-`` name with the ``optwf-`` prefix), so a cell never resumes, skips on or chain-seeds from a
plain style job's rows. The digest covers the cell's base optimize args (no window, name or
generation budget; --parallel/--worker(s)/--labels excluded), the CONTENT of the style
(``spec_sha256``, not the --style-config path) and of the --screener-universe-file, the store
environment, the launcher, the option-only fixed settings and the walk-forward spec. Under
``--wf-defer-last-test`` the data values and locations are masked (manifests, universe, screener
store, daily panel, universe file, BA2_HOME, launcher path): phase B (January, new data) rebuilds
phase A's names, and its deferred tests read the CURRENT command's manifests, store and panel
(``ba2test_launcher._rebase_run_data``). The universe restriction stays the train job's recorded one.

RESUME is the engine's: a completed fold job is skipped and its stored fold report reused, a fold
whose runner was killed is relaunched with ``optimize --resume-interrupted``, one driver per cell
(``<report dir>/<cell>/.lock``). ``--dry-run`` prints the chain and runs nothing. Plan:
docs/plans/2026-11-02-goal2027-two-phase-walk-forward.md, "Option style cells".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from typing import List, Optional, Tuple

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import run_options_matrix as ROM  # noqa: E402
from matrix_flags import wf_optimize_args  # noqa: E402

STYLE_KEY = "O_STYLE"
#: Seed sources a walk-forward style cell refuses (owner 2026-11-15: unseeded folds).
SEED_FLAGS = ("--seed-population", "--seed-genomes", "--seed-check-only", "--warm-start-from",
              "--chain-seed-from")
SEED_REFUSAL = ("walk-forward style cells are UNSEEDED (owner 2026-11-15): fold 1 is a cold start and "
                "each fold seeds only the next fold (the chain's own --chain-seed-from). A seed "
                "population chosen from stage-1 winners (or any file picked with knowledge of 2020-2025) "
                "has seen the 2024 and 2025 test years, so it would be look-ahead. Pass the word 'none' "
                "as the seed bundle of tools/option_style_job.sh")
#: Values not part of a cell's identity (where it runs or how it is labelled, not what it searches).
_NOT_IDENTITY = ("--name", "--parallel", "--workers", "--worker", "--labels")
#: Under --wf-defer-last-test: flags whose value says WHICH data a cell reads (masked in the name).
DEFER_MASKED = ROM._WF_DEFER_MASKED_FLAGS + ("--screener-store", "--screener-panel",
                                             "--screener-universe-file")
_FAILED: list = []


def split_argv(argv: List[str]) -> Tuple[List[str], List[str]]:
    """(the operator's extra args, the plain job's command) around the FIRST ``--``."""
    if "--" not in argv:
        raise SystemExit("run_option_style_wf.py: expected '<extra args> -- <launcher> optimize <args>' "
                         "(tools/option_style_job.sh --walk-forward builds it)")
    k = argv.index("--")
    cmd = list(argv[k + 1:])
    if "optimize" not in cmd:
        raise SystemExit("run_option_style_wf.py: the command after '--' is not an optimize command")
    return list(argv[:k]), cmd


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ROM.add_walk_forward_flags(ap)
    ap.add_argument("--dry-run", action="store_true", help="Print the cell's chain; run nothing.")
    return ap


def _flag_given(tokens: List[str], flag: str) -> bool:
    return any(t == flag or t.startswith(flag + "=") for t in tokens)


def _value(tokens: List[str], flag: str) -> Optional[str]:
    """The LAST value of ``flag`` (argparse semantics: a later override wins)."""
    out = None
    for i, t in enumerate(tokens):
        if t == flag and i + 1 < len(tokens):
            out = tokens[i + 1]
        elif t.startswith(flag + "="):
            out = t.split("=", 1)[1]
    return out


def resolve(argv: List[str]):
    """Parse and validate. Returns ``(args, cmd)``: ``cmd`` is the plain-equivalent command (recipe +
    overrides). Every refusal exits with its reason."""
    extra, recipe = split_argv(argv)
    ap = build_parser()
    args, overrides = ap.parse_known_args(extra)
    typos = [t for t in overrides if t.startswith(("--wf-", "--pass-"))]
    if typos:
        ap.error(f"unknown walk-forward flag(s) {typos} (they would be handed to optimize)")
    if not args.walk_forward:
        ap.error("this driver runs walk-forward cells only (tools/option_style_job.sh enters it when "
                 "--walk-forward is among its extra args)")
    window = [f for f in ("--start", "--end", "--generations") if _flag_given(overrides, f)]
    ROM._resolve_walk_forward(ap, args, window)
    if _flag_given(overrides, "--name"):
        ap.error("--walk-forward: --name cannot be given: the cell is named from the style job's own "
                 "name (optm-... -> optwf-...-d<digest>) and the chain names each fold's job")
    cmd = recipe + overrides
    body = cmd[cmd.index("optimize") + 1:]
    seeds = [f for f in SEED_FLAGS if _flag_given(body, f)]
    if seeds:
        ap.error(f"--walk-forward: {', '.join(seeds)} refused: {SEED_REFUSAL}")
    if _value(body, "--strategy") != STYLE_KEY:
        ap.error(f"--walk-forward: this driver runs {STYLE_KEY} style jobs, got --strategy "
                 f"{_value(body, '--strategy')!r} (option structures: tools/run_options_matrix.py "
                 f"--walk-forward)")
    if not (_value(body, "--name") or "").startswith("optm-"):
        ap.error(f"--walk-forward: the style job's --name must start with 'optm-' (the cell takes it with "
                 f"the optwf- prefix), got {_value(body, '--name')!r}")
    if _flag_given(body, "--screen-preset"):
        ap.error("--walk-forward: --screen-preset (a FIXED screen) cannot be walk-forward-tested yet: it has "
                 "no screener genes, and the out-of-sample re-run's test-window universe recompute "
                 "(rerun_handler.recompute_static_universe) refuses a run without screener gene ranges, so "
                 "the cell would ERROR after training fold 1")
    if _flag_given(body, "--print-universe"):
        ap.error("--walk-forward: --print-universe prints the universe and exits, so no fold could "
                 "complete; run it on the plain style job")
    return args, cmd


def _style_sha(launcher: str, body: List[str]) -> str:
    """The content identity of the style the job builds (the launcher's own loader and hash)."""
    L = ROM._launcher_module(launcher)
    path, name = _value(body, "--style-config"), _value(body, "--style")
    try:
        return L._load_style_spec(path, name)["spec_sha256"]
    except (OSError, ValueError) as e:
        raise SystemExit(f"--walk-forward: --style-config {path} --style {name}: {e}")


def _file_sha(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return "sha256:" + hashlib.sha256(fh.read()).hexdigest()
    except OSError as e:
        raise SystemExit(f"--walk-forward: --screener-universe-file {path}: {e}")


def identity_config(tokens: List[str]) -> dict:
    """``{flag: value}`` of an optimize arg list, a switch as True; a later value wins (argparse).
    A token is a value when it does not start with ``--`` (the recipe's switches -- --screener,
    --benchmark-regime-gates, ... -- are followed by another flag)."""
    config, i = {}, 0
    while i < len(tokens):
        flag = tokens[i]
        if "=" in flag and flag.startswith("--"):
            flag, val = flag.split("=", 1)
            i += 1
        elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            val = tokens[i + 1]
            i += 2
        else:
            val = True
            i += 1
        if flag not in _NOT_IDENTITY:
            config[flag] = val
    return config


def style_cell_name(args, cmd: List[str]) -> str:
    """``optwf-...`` (the recipe's ``optm-`` name re-prefixed) + ``-d<digest>`` of the cell identity."""
    head, body = cmd[:cmd.index("optimize")], cmd[cmd.index("optimize") + 1:]
    launcher = head[-1]
    base = "optwf-" + _value(body, "--name")[len("optm-"):]
    tokens = wf_optimize_args(cmd)
    defer = args.wf_defer_last_test
    out = []
    for k, tok in enumerate(tokens):
        prev = tokens[k - 1] if k else None
        if defer and prev in DEFER_MASKED:
            tok = "<data>"
        elif prev == "--style-config":
            tok = "sha256:" + _style_sha(launcher, body)
        elif prev == "--screener-universe-file":
            tok = _file_sha(tok)
        out.append(tok)
    identity = {"schema": "optwf-style-1", "args": identity_config(out),
                "store": {key: ("<location>" if defer and key in ROM._LOCATION_ENV_KEYS
                                else os.environ.get(key)) for key in ROM._IDENTITY_ENV_KEYS},
                "launcher": "<location>" if defer else os.path.abspath(launcher),
                "wf": ROM.wf_spec_tokens(args)}
    fixed = ROM.option_fixed_settings(launcher, _value(body, "--expert"), STYLE_KEY)
    if fixed:
        identity["option_fixed_settings"] = fixed
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return f"{base}-d{digest}"


def _wf_selection(args) -> dict:
    return ROM._wf_selection(args)


def _make_wf_runner(args, selection):
    return ROM._make_wf_runner(args, selection)


def cell_spec(args, cmd: List[str], selection: dict):
    """The ``wf_chain.CellSpec`` of the style job, its plan validated here (the engine refuses a
    forbidden optimize flag, a bad threshold or fold layout before anything runs)."""
    WF, C = ROM._wf_import()
    body = cmd[cmd.index("optimize") + 1:]
    name = style_cell_name(args, cmd)
    spec = C.CellSpec(
        name=name, optimize_args=wf_optimize_args(cmd), launcher=cmd[:cmd.index("optimize")],
        train_start=args.wf_train_start_d, test_years=args.wf_years_l, data_end=args.wf_data_end_d,
        first_generations=args.wf_first_generations, next_generations=args.wf_next_generations,
        top_n=args.wf_top_n,
        thresholds=WF.Thresholds(args.pass_min_oos_return, args.pass_max_dd_mult, args.pass_min_folds,
                                 args.pass_max_oos_dd),
        require_test_years=tuple(args.pass_require_fold),
        exclusive_return=args.pass_min_oos_return_exclusive, defer_last_test=args.wf_defer_last_test,
        run_all_folds=args.wf_run_all_folds, test_parallel=args.wf_test_parallel,
        selection=dict(selection), expert=_value(body, "--expert"), strategy=STYLE_KEY,
        band=f"style/{_value(body, '--style')}", floor=None)
    try:
        C.make_cell_plan(spec)
    except WF.WalkForwardError as e:
        raise SystemExit(f"--walk-forward {name}: {e}")
    return spec


def rate_window(args) -> Tuple[str, str]:
    """The days the cell's jobs and tests read the risk-free rate for: the train start to the data
    end (phase A) or to the last test year's end (phase B)."""
    end = args.wf_data_end_d or args.wf_folds[-1].test_end
    return args.wf_folds[0].train_start.isoformat(), end.isoformat()


def main(argv: Optional[List[str]] = None) -> int:
    args, cmd = resolve(list(sys.argv[1:] if argv is None else argv))
    launcher = cmd[cmd.index("optimize") - 1]
    holdout = ROM._launcher_module(launcher)._OPTION_HOLDOUT_START
    why = ROM.wf_holdout_refusal(args.wf_folds, args.wf_deferred, holdout)
    if why is not None:
        raise SystemExit(f"run_option_style_wf.py: --walk-forward: {why}")
    selection = _wf_selection(args)
    spec = cell_spec(args, cmd, selection)
    print(f"option style walk-forward cell {spec.name} ({spec.expert} {spec.band}); reports under "
          f"{args.wf_report_dir}")
    print(f"Folds: train from {args.wf_train_start}, test years {args.wf_test_years}"
          + (f", data end {args.wf_data_end}" if args.wf_data_end else "")
          + (" (last test DEFERRED)" if args.wf_deferred is not None else "")
          + f"; generations {args.wf_first_generations} (fold 1, cold start) / "
          f"{args.wf_next_generations} (seeded folds, refit); option holdout from {holdout} "
          f"(launcher rail); unseeded (no external seed population)")
    print(f"Selection: top {args.wf_top_n} behaviour-distinct genomes per fold, {selection}")
    if args.dry_run:
        ROM.print_wf_dry_run([spec], ROM._completed_names())
        return 0
    start, end = rate_window(args)
    fred = os.path.join(_TOOLS_DIR, "refresh_fred_cache.py")
    if subprocess.run([sys.executable, fred, "--check-rate-window", start, end],
                      env=os.environ.copy()).returncode != 0:
        raise SystemExit(f"run_option_style_wf.py: the risk-free rate (DGS3MO) is not cached for "
                         f"{start}..{end} (tools/refresh_fred_cache.py --check-rate-window); nothing ran")
    runner = _make_wf_runner(args, selection)
    return ROM.run_wf_cells([spec], runner, args.wf_report_dir, _FAILED,
                            "option style walk-forward driver")


if __name__ == "__main__":
    raise SystemExit(main())
