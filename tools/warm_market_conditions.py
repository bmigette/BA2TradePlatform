#!/usr/bin/env python
"""Warm the central market-condition feature store: plan / build / verify.

ONE preparation for a whole search (design section 4.4). Typical use:

    # 1. What is missing? (inventory + source preflight; writes the plan, fetches nothing)
    python tools/warm_market_conditions.py plan --profile ohlcv-v1 \
        --universe-file tools/options_universe_top100.txt \
        --start 2020-01-02 --end 2026-09-15 --out plan.json

    # 2a. Offline: build only from what is already cached (nonzero exit with the inventory
    #     if raw coverage is missing -- it fetches NOTHING).
    python tools/warm_market_conditions.py build --plan plan.json --cache-only

    # 2b. Or let it fetch the missing coverage through the existing provider cache path.
    python tools/warm_market_conditions.py build --plan plan.json --fetch-missing --concurrency 4

    # 3. Prove a published snapshot (re-hashes every referenced object).
    python tools/warm_market_conditions.py verify --manifest <digest>

Exit codes: 0 success; 1 an actionable inventory, a failed preflight, a build error or a failed
verification; 2 a configuration error (unknown profile/source profile, bad dates, missing files).
``prepare-host`` (the per-host mapped arrays) lands in Task 7 of the implementation plan.

Nothing here decides anything: arguments and reporting only. The orchestration lives in
``ba2_providers.market_conditions.warmup`` so the CLI, the job queue and live pre-analysis
preparation produce the same plan and the same manifests.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXIT_OK, EXIT_ACTIONABLE, EXIT_CONFIG = 0, 1, 2


def _out(obj) -> None:
    print(json.dumps(obj, indent=1, sort_keys=True, default=str))


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r} is not an ISO date (YYYY-MM-DD): {e}")


def _universe(path: str):
    """One symbol per line; blank lines and ``#`` comments ignored."""
    with open(path, "r", encoding="utf-8") as f:
        syms = [line.split("#", 1)[0].strip().upper() for line in f]
    return [s for s in syms if s]


def _cache_root(arg):
    if arg:
        return os.path.abspath(arg)
    from ba2_common import config
    return config.CACHE_FOLDER


def make_source(cache_root: str):
    """The provider the warmup fetches through (seam: tests replace this)."""
    from ba2_providers.market_conditions.fmp_source import FMPWarmupSource
    return FMPWarmupSource(cache_root)


def cmd_plan(args) -> int:
    from ba2_providers.market_conditions import warmup as W

    root = _cache_root(args.cache_root)
    if not os.path.isfile(args.universe_file):
        print(f"universe file not found: {args.universe_file}", file=sys.stderr)
        return EXIT_CONFIG
    universe = _universe(args.universe_file)
    try:
        plan = W.plan(args.profile, universe, args.start, args.end, args.source_profile, root,
                      source=make_source(root), log=_logger(args))
    except W.WarmupConfigError as e:  # includes a source whose provider writes elsewhere
        print(f"configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    if args.out:
        plan.save(args.out)
    inventory = plan.blocking_inventory()
    _out({"plan": plan.summary(), "out": args.out, "preflight_errors": plan.preflight_errors,
          "inventory": inventory})
    # Nonzero means "action needed before this can be built", not "the plan failed": a failed
    # source preflight, or coverage that a cache-only build could not satisfy.
    return EXIT_ACTIONABLE if (plan.preflight_errors or inventory) else EXIT_OK


def cmd_build(args) -> int:
    from ba2_providers.market_conditions import warmup as W

    if not os.path.isfile(args.plan):
        print(f"plan file not found: {args.plan}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        plan = W.WarmPlan.load(args.plan)
    except (ValueError, KeyError, TypeError) as e:
        print(f"configuration error: {args.plan} is not a usable plan: {e}", file=sys.stderr)
        return EXIT_CONFIG
    source = None
    if args.fetch_missing:
        try:
            source = make_source(plan.cache_root)
        except W.WarmupConfigError as e:
            print(f"configuration error: {e}", file=sys.stderr)
            return EXIT_CONFIG
    try:
        report = W.build(plan, fetch_missing=bool(args.fetch_missing), concurrency=args.concurrency,
                         log=_logger(args), source=source)
    except W.WarmupConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    _out(report.to_dict())
    return report.exit_code


def cmd_verify(args) -> int:
    from ba2_common.core.market_condition_store import ManifestError
    from ba2_providers.market_conditions import warmup as W

    root = _cache_root(args.cache_root)
    try:
        report = W.verify(args.manifest, root)
    except FileNotFoundError as e:
        print(f"verification failed: {e}", file=sys.stderr)
        return EXIT_ACTIONABLE
    except ManifestError as e:
        print(f"verification failed: {e}", file=sys.stderr)
        return EXIT_ACTIONABLE
    _out(report.to_dict())
    return EXIT_OK if report.ok else EXIT_ACTIONABLE


def cmd_prepare_host(args) -> int:
    print("prepare-host (per-host mapped arrays) lands in Task 7 of "
          "docs/plans/2026-09-15-option-market-condition-genes-impl.md; it is not implemented yet.",
          file=sys.stderr)
    return EXIT_CONFIG


def _logger(args):
    if getattr(args, "quiet", False):
        return lambda _msg: None
    return lambda msg: print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    ap = argparse.ArgumentParser(description=__doc__, parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", parents=[common], help="inventory + source preflight; writes a plan JSON")
    p.add_argument("--profile", required=True)
    p.add_argument("--universe-file", required=True)
    p.add_argument("--start", required=True, type=_date, help="first DECISION session (ISO date)")
    p.add_argument("--end", required=True, type=_date, help="last DECISION session (ISO date)")
    p.add_argument("--source-profile", default=None)
    p.add_argument("--cache-root", default=None)
    p.add_argument("--out", default=None, help="where to write the plan JSON")
    p.set_defaults(func=cmd_plan)

    b = sub.add_parser("build", parents=[common], help="build and publish the manifest for a plan")
    b.add_argument("--plan", required=True)
    mode = b.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cache-only", action="store_true", help="never fetch; stop with the inventory")
    mode.add_argument("--fetch-missing", action="store_true", help="fetch the missing coverage first")
    b.add_argument("--concurrency", type=int, default=4)
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", parents=[common], help="re-hash every object a manifest references")
    v.add_argument("--manifest", required=True)
    v.add_argument("--cache-root", default=None)
    v.set_defaults(func=cmd_verify)

    h = sub.add_parser("prepare-host", parents=[common], help="(Task 7) prepare this host's mapped arrays")
    h.add_argument("--manifest", default=None)
    h.set_defaults(func=cmd_prepare_host)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:                      # argparse exits 2 on usage errors == config error
        return EXIT_CONFIG if e.code else EXIT_OK
    if getattr(args, "source_profile", None) is None and args.command == "plan":
        from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY
        args.source_profile = SOURCE_PROFILE_FMP_DAILY
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
