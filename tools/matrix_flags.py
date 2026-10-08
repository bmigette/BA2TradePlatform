"""Shared ``ba2-test optimize`` flag passthrough for the GA matrix drivers.

The three matrix drivers (``run_options_matrix.py``, ``run_senate_matrix.py``,
``run_screener_capband_matrix.py``) each forward the same profit-cap knobs to every job they
launch. Keeping ONE implementation here is what stops the falsy-zero bug from being fixed in
one driver and left in the other two.

Imported as a plain sibling module (``import matrix_flags``): every driver is run as
``python tools/<driver>.py``, so ``tools/`` is already ``sys.path[0]``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, List


def cap_passthrough(args: Any) -> List[str]:
    """``--profit-cap-pct`` / ``--profit-share-cap-pct`` tokens for an ``optimize`` command.

    **``0`` must be FORWARDED, not omitted.** Every driver's help says "Pass 0 to disable",
    and ``ba2test_launcher`` maps a falsy value to ``None`` (= no cap) — but only if it
    actually receives the flag. Omitting it (the old ``if args.profit_cap_pct and ... > 0``
    guard, which treats ``0.0`` as "unset") makes the launcher re-apply its OWN default of
    2000.0 / 25.0, i.e. the exact opposite of what the user asked for.

    ``None`` is the only value that means "not configured at all" and is omitted.
    """
    out: List[str] = []
    if args.profit_cap_pct is not None:
        out += ["--profit-cap-pct", str(args.profit_cap_pct)]
    if args.profit_share_cap_pct is not None:
        out += ["--profit-share-cap-pct", str(args.profit_share_cap_pct)]
    return out


#: Appended to the base name of every job that carries the decision-time gene (the launcher
#: REQUIRES it in a ``--decision-times`` job's name; see ``ba2test_launcher._resolve_decision_times``).
DECISION_TIMES_NAME_TOKEN = "-timegene"


def decision_times_plan(raw: Any, interval: str) -> "tuple[list | None, str]":
    """``(times, header)`` for a GRID DRIVER's ``--decision-times`` flag.

    Drivers default to the gene ON (``when_unset="default"``: the shared
    ``DEFAULT_DECISION_TIME_CHOICES``); ``fixed`` turns it off; a comma list overrides. On a daily
    clock an UNSET flag resolves to "not optimizable" (an explicit one is refused). ``header`` is
    the line the driver prints once, stating the EFFECTIVE choice."""
    from ba2_common.core.knowability import DEFAULT_DECISION_TIME
    from ba2_common.core.schedule_genes import interval_minutes, parse_decision_times_arg

    times = parse_decision_times_arg(raw, interval, when_unset="default")
    if times:
        return times, f"decision times (GA gene schedule:time): {','.join(times)}"
    try:
        interval_minutes(interval)
    except ValueError:
        return None, "decision time: daily clock (not optimizable)"
    return None, (f"decision time: fixed {DEFAULT_DECISION_TIME} "
                  f"(DEFAULT_DECISION_TIME; --decision-times fixed)")


def decision_times_tokens(times: "list | None") -> List[str]:
    """``--decision-times`` tokens for one job's argv ([] when the gene is off)."""
    return ["--decision-times", ",".join(times)] if times else []


OPTION_DRIVER_DT_HEADER = "decision time: daily clock (not optimizable)"


def add_refused_decision_times_flag(ap: Any) -> None:
    """Declare ``--decision-times`` on an OPTION driver's parser purely to REFUSE it with a reason.

    Option backtests run on a DAILY clock (the option price data are daily bars), so there is no
    decision time to search; see docs/plans/2026-10-07-decision-time-gene.md section on options."""
    ap.add_argument("--decision-times", default=None, help="REFUSED: option jobs run on a daily "
                    "clock, the decision time is not optimizable.")


def refuse_decision_times_and_announce(args: Any) -> None:
    """Exit loudly if an option driver was given ``--decision-times``; else print the header."""
    if getattr(args, "decision_times", None) is not None:
        raise SystemExit(
            f"--decision-times {args.decision_times!r} is refused by this option driver: option "
            f"backtests run on a DAILY clock (one bar per session; option prices are daily "
            f"bars), so the decision time is not optimizable.")
    print(OPTION_DRIVER_DT_HEADER, flush=True)


def with_decision_times_name(name: str, times: "list | None") -> str:
    """``name`` with the gene's name token appended when the job carries the gene."""
    return name + DECISION_TIMES_NAME_TOKEN if times and DECISION_TIMES_NAME_TOKEN not in name else name


#: The STATIC-UNIVERSE rule of a ``--screener`` job is part of its identity (the launcher builds the
#: true superset, ``ba2_providers.screener.universe_superset.RULE_ID``). A job name WITHOUT this token was
#: launched under the old cap-ranked top-50 rule: its completed row, its skip-completed check and its
#: checkpoint (also fingerprinted, see ``checkpoint_fingerprint``) must never be taken for a superset-
#: universe job's. Appended to EVERY screener job name by the cap-band driver.
UNIVERSE_RULE_NAME_TOKEN = "-sup1"


def with_universe_rule_name(name: str) -> str:
    """``name`` with the superset-universe-rule token appended (idempotent)."""
    return name if UNIVERSE_RULE_NAME_TOKEN in name else name + UNIVERSE_RULE_NAME_TOKEN


def screener_dry_run_universe_note(store: str, band: str, start: str, end: str, interval: str,
                                   _memo: dict = {}) -> str:
    """The static-universe size of one screener job for a driver's dry-run line, e.g.
    ``static universe 2135 symbols`` (+ the uncached names that would REFUSE the launch). Memoised per
    (store, band, start, end, interval). Never silent: an error is printed as the note itself."""
    key = (store, band, start, end, interval)
    if key not in _memo:
        try:
            from ba2_providers.screener.universe_superset import preview_static_universe
            r = preview_static_universe(store, band, start, end, interval)
            note = f"static universe {r['size']} symbols"
            if r["uncached"]:
                note += (f"; {len(r['uncached'])} UNCACHED ({', '.join(r['uncached'][:8])}"
                         f"{'...' if len(r['uncached']) > 8 else ''}): launch REFUSES unless "
                         f"--exclude-uncached")
        except Exception as e:  # noqa: BLE001 - dry-run diagnostics must say why, not vanish
            note = f"static universe UNAVAILABLE ({type(e).__name__}: {e})"
        _memo[key] = note
    return _memo[key]


def job_name_with_digest(name: str, cmd: List[str]) -> str:
    """``name``, or ``name-d<digest>`` when ``cmd`` carries any token beyond the driver's base
    invocation (i.e. the caller only invokes this once it has decided a digest is warranted —
    see each driver's own trigger condition, e.g. ``run_screener_capband_matrix.py``'s
    ``mc_tokens or excl_tokens``).

    The digest is a sha256 (first 12 hex chars) of the job's own fully-resolved ``optimize``
    argv, EXCLUDING ``--name``/``--parallel``/``--workers`` (metadata that must not move the job
    identity: a resubmit that only changes concurrency or which workers it runs on must resume
    the SAME row, not mint a new one). Originally ``run_screener_capband_matrix.py:_job_name``
    (goal2027atr market-condition passthrough); pulled out here so
    ``tools/run_senate_matrix.py``'s Senate lane gets byte-identical digest behaviour instead of
    a second hand-copied implementation that could drift from this one.
    """
    tokens = [t for t in cmd if t]
    start = tokens.index("optimize") + 1 if "optimize" in tokens else 0
    tokens = tokens[start:]
    kept: List[str] = []
    skip = False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok in ("--name", "--parallel", "--workers"):
            skip = True
            continue
        kept.append(tok)
    digest = hashlib.sha256(json.dumps(kept, sort_keys=False).encode()).hexdigest()[:12]
    return f"{name}-d{digest}"



# --------------------------------------------------------------------------------------------------
# Failed jobs are REPORTED, never silently dropped (owner decision 2026-10-07: a job whose analyses fail
# en masse "should fail the job for analysis"; the job row is marked failed, so a re-run does NOT skip it
# as completed). The driver goes on to the next job, prints every failed job in its final summary and
# exits non-zero.
# --------------------------------------------------------------------------------------------------
#: A stored failure reason carrying this marker came from a job-fatal error (see
#: ``strategy_optimization_handler.JOB_FATAL_ERROR_TYPES``); the driver prints the reason verbatim.
JOB_FATAL_MARKER = "[job-fatal"
ANALYSIS_REFUSAL_TYPE = "AnalysisFailureRefusal"


def is_analysis_failure_reason(reason: str) -> bool:
    """True when a stored failure reason says the job was refused for failing analysis passes: the one
    job-fatal class that is a property of THAT job (its expert / data), so a campaign carries on to
    the next job instead of stopping."""
    return ANALYSIS_REFUSAL_TYPE in (reason or "") and JOB_FATAL_MARKER in (reason or "")


def note_job_exit(failed: list, job_name: str, rc: int, reason: str = "") -> None:
    """Record a job that exited non-zero (``reason`` = its stored failure text when known)."""
    if rc != 0:
        failed.append((job_name, rc, reason))


def finish_matrix(failed: list, label: str) -> int:
    """Print the final summary of failed jobs and return the driver's exit code (0 only if none
    failed). Clears ``failed`` so a second ``main()`` in one process starts clean."""
    jobs = list(failed)
    failed.clear()
    if not jobs:
        print(f"{label}: done.")
        return 0
    print(f"{label}: done, but {len(jobs)} job(s) FAILED:")
    for name, rc, reason in jobs:
        print(f"  FAILED {name} (exit={rc})" + (f": {reason}" if reason else ""))
    return 1
