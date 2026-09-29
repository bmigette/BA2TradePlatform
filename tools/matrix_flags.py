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
