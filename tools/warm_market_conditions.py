#!/usr/bin/env python
"""Warm the central market-condition feature store: plan / build / verify.

ONE preparation for a whole search (design section 4.4). Typical use:

    # 1. What is missing? (inventory + source preflight; writes the plan, fetches nothing)
    python tools/warm_market_conditions.py plan --profile ohlcv-v1 \
        --universe-file tools/options_universe_top100.txt \
        --start 2020-01-02 --end 2026-09-15 --ohlcv-history-source fmp --out plan.json

    # 2a. Offline: build only from what is already cached (nonzero exit with the inventory
    #     if raw coverage is missing -- it fetches NOTHING).
    python tools/warm_market_conditions.py build --plan plan.json --cache-only --ohlcv-history-source fmp

    # 2b. Or let it fetch the missing coverage through the existing provider cache path.
    python tools/warm_market_conditions.py build --plan plan.json --fetch-missing --concurrency 4 --ohlcv-history-source fmp

    # 1b. Repair ONLY (no build): full re-fetch of every split-basis-refused name, top-up of
    #     missing coverage, then the re-check. Exit 1 names what still refuses (a human
    #     decision: an override, --keep-newer-cached-tail for a vendor file that ends early, or
    #     an exclusion). Refused while the US session is open (the re-fetch would store the
    #     day's partial bar) unless --allow-open-session.
    python tools/warm_market_conditions.py repair --plan plan.json --ohlcv-history-source fmp [--keep-newer-cached-tail SATS]

    # 2c. Publish anyway, recording the symbols that cannot be warmed (never the default).
    python tools/warm_market_conditions.py build --plan plan.json --cache-only --allow-exclusions --ohlcv-history-source fmp

    # 3. Prove a published snapshot (re-hashes every referenced object).
    python tools/warm_market_conditions.py verify --manifest <digest> --ohlcv-history-source fmp

    # 4. Prepare THIS host: verify, then build the memory-mapped arrays every worker maps.
    #    Run it on the master and on every worker before dispatching a search.
    python tools/warm_market_conditions.py prepare-host --manifest <digest> --jobs 4 --ohlcv-history-source fmp

EVERY subcommand takes ``--ohlcv-history-source {fmp,alpaca}`` (REQUIRED, no default: a typo or an omission
must never silently pick a vendor). It selects the source profile recorded in the manifest and the price
folder read (fmp = <CACHE>/FMPOHLCVProvider, alpaca = <CACHE>/AlpacaHistoryOHLCVProvider). For alpaca the
cache is built ONLY by ``tools/prepare_history.py``: ``plan`` runs the same split-basis preflight against the
splits that build applied (``_meta/build_manifest.json``), ``build --fetch-missing`` is refused, and
``verify`` / ``prepare-host`` refuse a manifest of the other source.

Exit codes: 0 success; 1 an actionable inventory, a failed preflight, a build error or a failed
verification (``prepare-host`` included: a corrupt object NEVER becomes a mapping); 2 a
configuration error (unknown profile/source profile, bad dates, missing files).

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


def make_source(cache_root: str, history_source: str, **kw):
    """The WarmupSource of ``history_source`` (seam: tests replace this). ``kw`` are FMP-source options
    (``keep_newer_cached_tail``); the Alpaca source takes none."""
    from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY, source_profile_for_history
    if source_profile_for_history(history_source) == SOURCE_PROFILE_FMP_DAILY:
        from ba2_providers.market_conditions.fmp_source import FMPWarmupSource
        return FMPWarmupSource(cache_root, **kw)
    if kw:
        raise ValueError(f"the {history_source} source takes no options, got {sorted(kw)}")
    from ba2_providers.market_conditions.alpaca_history_source import AlpacaHistoryWarmupSource
    return AlpacaHistoryWarmupSource(cache_root)


def _history_source(text: str) -> str:
    """argparse ``type``: a known history source or a usage error (exit 2)."""
    from ba2_common.core.ohlcv_history import UnknownHistorySource, check_history_source
    try:
        return check_history_source(text)
    except UnknownHistorySource as e:
        raise argparse.ArgumentTypeError(str(e))


def _check_manifest_source(root: str, digest: str, profile, history_source: str):
    """None when the manifest's recorded source profile is ``history_source``'s, else the refusal text."""
    from ba2_common.core.market_condition_source import source_profile_for_history
    from ba2_common.core.market_condition_store import MarketConditionStore
    wanted = source_profile_for_history(history_source)
    have = MarketConditionStore(root).read_manifest(digest, profile)["source_profile"]
    if have != wanted:
        return (f"manifest {digest} was built on source profile {have!r}, not {wanted!r} "
                f"(--ohlcv-history-source {history_source}); refusing")
    return None


def us_session_open() -> bool:
    """True while a regular NYSE session is in progress (seam: tests replace this)."""
    from datetime import timedelta, timezone
    from ba2_common.core.market_calendar import nyse_regular_sessions
    now = datetime.now(timezone.utc)
    today = now.date()
    return any(o <= now < c for o, c in nyse_regular_sessions(today - timedelta(days=1),
                                                                today + timedelta(days=1)))


def cmd_repair(args) -> int:
    from ba2_providers.market_conditions import warmup as W

    if not os.path.isfile(args.plan):
        print(f"plan file not found: {args.plan}", file=sys.stderr)
        return EXIT_CONFIG
    session_open = us_session_open()
    if session_open and not args.allow_open_session:
        print("refusing to re-fetch while the US regular session is open: the full re-fetch "
              "would store today's PARTIAL bar. Re-run after the close, or pass "
              "--allow-open-session and repeat the repair after the close.", file=sys.stderr)
        return EXIT_CONFIG
    try:
        plan = W.MarketConditionWarmPlan.load(args.plan)
    except (ValueError, KeyError, TypeError) as e:
        print(f"configuration error: {args.plan} is not a usable plan: {e}", file=sys.stderr)
        return EXIT_CONFIG
    from ba2_common.core.market_condition_source import SOURCE_PROFILE_FMP_DAILY, source_profile_for_history
    if (args.ohlcv_history_source != "fmp" or plan.source_profile != SOURCE_PROFILE_FMP_DAILY
            or source_profile_for_history(args.ohlcv_history_source) != SOURCE_PROFILE_FMP_DAILY):
        print(f"configuration error: repair is FMP-only (--ohlcv-history-source {args.ohlcv_history_source}, "
              f"plan source profile {plan.source_profile!r}): the Alpaca cache is built only by "
              f"tools/prepare_history.py", file=sys.stderr)
        return EXIT_CONFIG
    keep = [s.strip().upper() for s in (args.keep_newer_cached_tail or "").split(",") if s.strip()]
    try:
        source = (make_source(plan.cache_root, args.ohlcv_history_source, keep_newer_cached_tail=keep)
                  if keep else make_source(plan.cache_root, args.ohlcv_history_source))
    except W.WarmupConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    report = W.repair(plan, source=source, concurrency=args.concurrency, log=_logger(args))
    report["keep_newer_cached_tail"] = keep
    # Re-fetched during the session: today's partial bar is in the files. Say so -- the repair
    # must be run again after the close before anything is built from them.
    report["rerun_after_close_required"] = bool(session_open)
    _out(report)
    return EXIT_OK if report["ok"] else EXIT_ACTIONABLE


def cmd_plan(args) -> int:
    from ba2_providers.market_conditions import warmup as W

    from ba2_common.core.market_condition_source import source_profile_for_history
    root = _cache_root(args.cache_root)
    source_profile = source_profile_for_history(args.ohlcv_history_source)
    if not os.path.isfile(args.universe_file):
        print(f"universe file not found: {args.universe_file}", file=sys.stderr)
        return EXIT_CONFIG
    universe = _universe(args.universe_file)
    try:
        plan = W.plan(args.profile, universe, args.start, args.end, source_profile, root,
                      source=make_source(root, args.ohlcv_history_source), log=_logger(args),
                      gap_fill=args.gap_fill)
    except W.WarmupConfigError as e:  # includes a source whose provider writes elsewhere
        print(f"configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    if args.out:
        plan.save(args.out)
    inventory = plan.blocking_inventory()
    _out({"plan": plan.summary(), "out": args.out, "preflight_errors": plan.preflight_errors,
          "fatal_preflight_errors": plan.fatal_preflight_errors(), "inventory": inventory})
    # Nonzero means "action needed before this can be built", not "the plan failed": a failed
    # source preflight, or coverage that a cache-only build could not satisfy.
    return EXIT_ACTIONABLE if (plan.preflight_errors or inventory) else EXIT_OK


def cmd_build(args) -> int:
    from ba2_providers.market_conditions import warmup as W

    if not os.path.isfile(args.plan):
        print(f"plan file not found: {args.plan}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        plan = W.MarketConditionWarmPlan.load(args.plan)
    except (ValueError, KeyError, TypeError) as e:
        print(f"configuration error: {args.plan} is not a usable plan: {e}", file=sys.stderr)
        return EXIT_CONFIG
    from ba2_common.core.market_condition_source import source_profile_for_history
    if plan.source_profile != source_profile_for_history(args.ohlcv_history_source):
        print(f"configuration error: the plan was made on source profile {plan.source_profile!r} but "
              f"--ohlcv-history-source {args.ohlcv_history_source} is "
              f"{source_profile_for_history(args.ohlcv_history_source)!r}; re-run `plan` with the wanted source",
              file=sys.stderr)
        return EXIT_CONFIG
    if args.fetch_missing and args.ohlcv_history_source != "fmp":
        print(f"configuration error: --fetch-missing is not available for --ohlcv-history-source "
              f"{args.ohlcv_history_source}: that cache is built only by tools/prepare_history.py "
              f"(use --cache-only)", file=sys.stderr)
        return EXIT_CONFIG
    if args.gap_fill is not None and args.gap_fill != plan.gap_fill:
        print(f"configuration error: --gap-fill {args.gap_fill!r} does not match the plan's own "
              f"gap_fill {plan.gap_fill!r} (set at `plan` time; re-run `plan` with the wanted "
              f"--gap-fill instead of overriding it at `build` time)", file=sys.stderr)
        return EXIT_CONFIG
    source = None
    if args.fetch_missing:
        try:
            source = make_source(plan.cache_root, args.ohlcv_history_source)
        except W.WarmupConfigError as e:
            print(f"configuration error: {e}", file=sys.stderr)
            return EXIT_CONFIG
    try:
        report = W.build(plan, fetch_missing=bool(args.fetch_missing), concurrency=args.concurrency,
                         log=_logger(args), source=source, allow_exclusions=bool(args.allow_exclusions))
    except W.WarmupConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG
    if args.print_digest:
        # STDOUT is then the digest and NOTHING else, so a shell step can capture it with a plain
        # command substitution; the JSON report still goes out, on stderr, because a build whose
        # counters nobody can see is a build nobody can audit. An unpublished build (no manifest)
        # prints nothing and keeps its nonzero exit code -- a caller must not read an empty
        # capture as "no snapshot needed".
        print(json.dumps(report.to_dict(), indent=1, sort_keys=True, default=str), file=sys.stderr)
        if report.manifest_digest:
            print(report.manifest_digest)
    else:
        _out(report.to_dict())
    return report.exit_code


def cmd_verify(args) -> int:
    from ba2_common.core.market_condition_store import ManifestError
    from ba2_providers.market_conditions import warmup as W

    root = _cache_root(args.cache_root)
    try:
        refusal = _check_manifest_source(root, args.manifest, None, args.ohlcv_history_source)
        if refusal:
            print(f"verification failed: {refusal}", file=sys.stderr)
            return EXIT_ACTIONABLE
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
    """Verify the manifest on THIS host, then build its mapped arrays (design section 4.4 step 5).

    Verification comes first and is not optional: the mapping is an immutable, atomically
    published array set that every worker process on the host then maps, so building it from an
    object that failed its hash would bake corrupt numbers into the whole box. A failure exits 1
    and leaves no mapping behind."""
    from ba2_common.core.market_condition_reader import prepare_host
    from ba2_common.core.market_condition_store import ManifestError

    root = _cache_root(args.cache_root)
    if not args.manifest:
        print("prepare-host needs --manifest <digest>", file=sys.stderr)
        return EXIT_CONFIG
    try:
        refusal = _check_manifest_source(root, args.manifest, args.profile, args.ohlcv_history_source)
        if refusal:
            print(f"prepare-host failed: {refusal}", file=sys.stderr)
            return EXIT_ACTIONABLE
        report = prepare_host(root, args.manifest, args.profile, jobs=args.jobs, log=_logger(args))
    except FileNotFoundError as e:
        print(f"prepare-host failed: {e}", file=sys.stderr)
        return EXIT_ACTIONABLE
    except ManifestError as e:
        print(f"prepare-host failed: {e}", file=sys.stderr)
        return EXIT_ACTIONABLE
    _out(report.to_dict())
    return EXIT_OK if report.ok else EXIT_ACTIONABLE


def _logger(args):
    if getattr(args, "quiet", False):
        return lambda _msg: None
    return lambda msg: print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    from ba2_common.core.market_condition_source import GAP_FILL_POLICIES

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    ap = argparse.ArgumentParser(description=__doc__, parents=[common],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    # REQUIRED on every subcommand, no default: a typo or an omission must never silently pick a vendor.
    hs = argparse.ArgumentParser(add_help=False)
    hs.add_argument("--ohlcv-history-source", required=True, type=_history_source, metavar="{fmp,alpaca}",
                    help="the OHLCV history the snapshot is built on / must have been built on: fmp = "
                         "<CACHE>/FMPOHLCVProvider, alpaca = <CACHE>/AlpacaHistoryOHLCVProvider (built only "
                         "by tools/prepare_history.py). Selects the manifest's source profile.")

    p = sub.add_parser("plan", parents=[common, hs], help="inventory + source preflight; writes a plan JSON")
    p.add_argument("--profile", required=True)
    p.add_argument("--universe-file", required=True)
    p.add_argument("--start", required=True, type=_date, help="first DECISION session (ISO date)")
    p.add_argument("--end", required=True, type=_date,
                   help="last DECISION/backtest-bar session (ISO date). Must be a COMPLETED "
                        "session: the snapshot's last row is that session itself, so an unclosed "
                        "end is refused (live needs rows only through the previous session)")
    p.add_argument("--cache-root", default=None)
    p.add_argument("--out", default=None, help="where to write the plan JSON")
    p.add_argument("--gap-fill", default=None, choices=list(GAP_FILL_POLICIES),
                   help="densify each symbol's raw series, carrying the PREVIOUS valid bar "
                        "forward into a missing/invalid session inside its own listed range "
                        "(never before its first bar or after its last). Default: no fill, "
                        "byte-identical to every plan/manifest built before this flag existed.")
    p.set_defaults(func=cmd_plan)

    r = sub.add_parser("repair", parents=[common, hs],
                       help="full re-fetch of the split-basis-refused names + top-up; no build")
    r.add_argument("--plan", required=True)
    r.add_argument("--concurrency", type=int, default=4)
    r.add_argument("--keep-newer-cached-tail", default=None, metavar="SYM[,SYM...]",
                   help="for THESE symbols only: when the vendor's full history ENDS before the "
                        "cache's, keep the cache's own newer bars (at most a few, no split among "
                        "them, the shared recent sessions agreeing, the merged file passing the "
                        "split-basis check) instead of refusing the re-fetch. Logged, and listed "
                        "per symbol under kept_tails in the report.")
    r.add_argument("--allow-open-session", action="store_true",
                   help="re-fetch although the US session is open (the day's partial bar is "
                        "stored; the report then says rerun_after_close_required and the repair "
                        "MUST be repeated after the close)")
    r.set_defaults(func=cmd_repair)

    b = sub.add_parser("build", parents=[common, hs], help="build and publish the manifest for a plan")
    b.add_argument("--plan", required=True)
    mode = b.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cache-only", action="store_true", help="never fetch; stop with the inventory")
    mode.add_argument("--fetch-missing", action="store_true", help="fetch the missing coverage first")
    b.add_argument("--concurrency", type=int, default=4)
    b.add_argument("--gap-fill", default=None, choices=list(GAP_FILL_POLICIES),
                   help="must match the plan's own --gap-fill (set at `plan` time); passing it "
                        "here is an explicit consistency check, not a way to change it post hoc.")
    b.add_argument("--allow-exclusions", action="store_true",
                   help="publish even though some symbols cannot be warmed (an unreadable split "
                        "calendar, an unverifiable basis, no source data); each is recorded in the "
                        "manifest coverage exceptions and in the report. Without it such a symbol is "
                        "an actionable inventory item and NOTHING is published.")
    b.add_argument("--print-digest", action="store_true",
                   help="print ONLY the published manifest digest on stdout (the JSON report goes "
                        "to stderr), so a launch script can capture it into "
                        "--market-condition-manifest.")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", parents=[common, hs], help="re-hash every object a manifest references")
    v.add_argument("--manifest", required=True)
    v.add_argument("--cache-root", default=None)
    v.set_defaults(func=cmd_verify)

    h = sub.add_parser("prepare-host", parents=[common, hs],
                       help="verify a manifest here, then build this host's mapped arrays")
    h.add_argument("--manifest", default=None)
    h.add_argument("--profile", default=None,
                   help="only needed to disambiguate a digest across profiles (the manifest "
                        "names its own profile)")
    h.add_argument("--cache-root", default=None)
    h.add_argument("--jobs", type=int, default=4,
                   help="concurrent object reads while building the mapping")
    h.set_defaults(func=cmd_prepare_host)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:                      # argparse exits 2 on usage errors == config error
        return EXIT_CONFIG if e.code else EXIT_OK
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
