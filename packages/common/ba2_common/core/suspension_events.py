"""Exchange suspensions / delistings of the Alpaca history cache, and the one pure rule that says a symbol is suspended.

Alpaca's feed carries exchange prints only.  A symbol suspended from its exchange trades OTC, so the cache has NO bars for the
whole period (FMP, which carries OTC, shows trading) and, marked at its stale last close, the position then jumps on relisting
(NESR +257%, MDXG +98%, SMCI +73%).  The cache build detects these (``ba2_providers.ohlcv.alpaca_history.build``, module
docstring "EXCHANGE SUSPENSIONS / DELISTINGS") and writes ``<cache dir>/_meta/suspensions.json``::

    {"build": "<run_id> built <ts>", "rule": {...},
     "symbols": {SYM: [[last_session, resume_session|null, kind, missing_sessions, fmp_traded_share|null], ...]},
     "undecided": {SYM: [[last_session, resume_session, missing_sessions, reason], ...]}}

``undecided`` lists the >= 20-session gaps the build could not judge (no FMP file / FMP not covering): visibility only, NOT
suspensions; the loader ignores it.

A backtest holding SYM on a session strictly after ``last_session`` (and before ``resume_session``, when there is one) closes
it at the last exchange close and does not re-enter until the symbol trades again.  Pure: BT today, a live caller may use the
same ``is_suspended`` later (BT/live parity: one function, two callers).  No file (FMP cache, or an old Alpaca build) -> ``{}``:
the feature is simply off.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

SUSPENSIONS_FILE = os.path.join("_meta", "suspensions.json")
KINDS = ("otc_suspension", "delisted")

#: ``{symbol: ((last_session, resume_session | None), ...)}`` -- ascending, non-overlapping.
SuspensionEvents = Mapping[str, Tuple[Tuple[date, Optional[date]], ...]]


def load_suspension_events(cache_dir: str) -> Dict[str, Tuple[Tuple[date, Optional[date]], ...]]:
    """The events of ``<cache_dir>/_meta/suspensions.json``; ``{}`` when the file is absent.  A malformed file RAISES ValueError
    (a half-read event table would silently mark a suspended name as tradable)."""
    path = os.path.join(cache_dir, SUSPENSIONS_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        symbols = raw["symbols"]
        for key in ("build", "rule"):                      # present in every file the build writes
            raw[key]
        out: Dict[str, Tuple[Tuple[date, Optional[date]], ...]] = {}
        for sym, rows in symbols.items():
            evs: List[Tuple[date, Optional[date]]] = []
            for row in rows:
                last_s, resume_s, kind, missing, _share = row
                if kind not in KINDS:
                    raise ValueError(f"unknown kind {kind!r}")
                last = date.fromisoformat(last_s)
                resume = None if resume_s is None else date.fromisoformat(resume_s)
                if resume is not None and resume <= last:
                    raise ValueError(f"resume {resume} not after last {last}")
                if (kind == "delisted") != (resume is None):
                    raise ValueError(f"kind {kind!r} with resume {resume_s!r}")
                if not isinstance(missing, int) or isinstance(missing, bool) or missing < 1:
                    raise ValueError(f"bad missing_sessions {missing!r}")
                evs.append((last, resume))
            if not evs:
                raise ValueError("symbol with no events")
            evs.sort(key=lambda e: e[0])
            out[sym.upper()] = tuple(evs)
        return out
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{path} is malformed ({type(e).__name__}: {e}): rebuild the Alpaca history cache") from e


def is_suspended(symbol: str, session: date, events: SuspensionEvents) -> bool:
    """True when ``symbol`` is suspended on ``session``: some event has ``last_session < session`` and (no resume, or
    ``session < resume_session``).  The resume session itself is tradable (its first real bar)."""
    evs = events.get(symbol)
    if evs is None:
        return False
    for last, resume in evs:
        if last < session and (resume is None or session < resume):
            return True
    return False


def symbols_to_liquidate(held_symbols: Iterable[str], session: date, events: SuspensionEvents) -> List[str]:
    """The held symbols that are suspended on ``session`` (order of ``held_symbols``).  O(held) dict lookups."""
    if not events:
        return []
    return [s for s in held_symbols if is_suspended(s, session, events)]
