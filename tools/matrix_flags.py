"""Shared ``ba2-test optimize`` flag passthrough for the GA matrix drivers.

The three matrix drivers (``run_options_matrix.py``, ``run_senate_matrix.py``,
``run_screener_capband_matrix.py``) each forward the same profit-cap knobs to every job they
launch. Keeping ONE implementation here is what stops the falsy-zero bug from being fixed in
one driver and left in the other two.

Imported as a plain sibling module (``import matrix_flags``): every driver is run as
``python tools/<driver>.py``, so ``tools/`` is already ``sys.path[0]``.
"""
from __future__ import annotations

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
