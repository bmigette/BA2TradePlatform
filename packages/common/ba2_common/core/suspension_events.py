"""Exchange suspensions / delistings of the Alpaca history cache, and the one pure rule that says a symbol is suspended.

Alpaca's feed carries exchange prints only.  A symbol suspended from its exchange trades OTC, so the cache has NO bars for the
whole period (FMP, which carries OTC, shows trading) and, marked at its stale last close, the position then jumps on relisting
(NESR +257%, MDXG +98%, SMCI +73%).  The cache build detects these (``ba2_providers.ohlcv.alpaca_history.build``, module
docstring "EXCHANGE SUSPENSIONS / DELISTINGS") and writes ``<cache dir>/_meta/suspensions.json``::

    {"build": "<run_id> built <ts>", "rule": {...},
     "symbols": {SYM: [[last_session, resume_session|null, kind, missing_sessions, fmp_traded_share|null], ...]}}

``otc_suspension``: a run of >= 20 NYSE sessions with no Alpaca bar where FMP traded on >= 80% of them.  ``exchange_gap``: any other such
run (FMP shows no trading, does not cover it, or there is no FMP file; the cause is unknown: halt, delist-and-relist, merger, no trades).
``delisted``: the last bar is > 5 sessions before the store's last session (no resume).  The backtest treats all three alike.  The build
also writes ``_meta/reused_tickers.json`` (tickers whose pre-gap bars it DROPPED because the current company listed at the gap's end, and
the gaps it could not judge); the loader does not read it.

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
KINDS = ("otc_suspension", "exchange_gap", "delisted")

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


# ---------------------------------------------------------------------------------------------------------------------
# Terminal value of a DELISTING (kind ``delisted``): what a holder actually received, not the last exchange close.
#
# ``<cache dir>/_meta/delisting_terminals.json`` (written by ``tools/build_screener_universe.py --install-terminals`` from
# Alpaca corporate actions after a cache build) ::
#
#     {"build": "<the suspensions.json build string>", "symbols": {SYM: {"last_session": "YYYY-MM-DD",
#       "kind": "worthless" | "bankruptcy_otc" | "cash_merger" | "stock_merger" | "unknown", "price": 0.0 | 12.5 | null,
#       "source": "..."}}}
#
# ``worthless`` / ``bankruptcy_otc`` -> price 0 (equity cancelled / left the exchange on a bankruptcy ticker); ``cash_merger`` ->
# the per-share cash of the merger action; ``stock_merger`` / ``unknown`` -> null = the last close (counted and logged by the
# account).  Point-in-time safe: it is only read AT or after the delisting.  No file -> ``{}`` (every delisting at the last close).
# ---------------------------------------------------------------------------------------------------------------------
TERMINALS_FILE = os.path.join("_meta", "delisting_terminals.json")
TERMINAL_KINDS = ("worthless", "bankruptcy_otc", "cash_merger", "stock_merger", "unknown")
ZERO_KINDS = ("worthless", "bankruptcy_otc")

#: ``{symbol: (last_session, kind, price | None)}``
DelistingTerminals = Mapping[str, Tuple[date, str, Optional[float]]]


def load_delisting_terminals(cache_dir: str) -> Dict[str, Tuple[date, str, Optional[float]]]:
    """The terminals of ``<cache_dir>/_meta/delisting_terminals.json``; ``{}`` when absent.  A malformed file, or one stamped with
    another suspensions build than the cache's ``suspensions.json``, RAISES ValueError (a stale table would price a delisting
    from another cache)."""
    path = os.path.join(cache_dir, TERMINALS_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        with open(os.path.join(cache_dir, SUSPENSIONS_FILE), encoding="utf-8") as f:
            build = json.load(f)["build"]
        if raw["build"] != build:
            raise ValueError(f"terminals built for {raw['build']!r}, suspensions.json is {build!r}")
        out: Dict[str, Tuple[date, str, Optional[float]]] = {}
        for sym, row in raw["symbols"].items():
            kind, price = row["kind"], row["price"]
            if kind not in TERMINAL_KINDS:
                raise ValueError(f"unknown kind {kind!r}")
            if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0):
                raise ValueError(f"bad price {price!r}")
            if (kind in ZERO_KINDS and price != 0) or (kind in ("stock_merger", "unknown") and price is not None)                     or (kind == "cash_merger" and price is None):
                raise ValueError(f"{sym}: kind {kind!r} with price {price!r}")
            out[sym.upper()] = (date.fromisoformat(row["last_session"]), kind, None if price is None else float(price))
        return out
    except (KeyError, TypeError, ValueError, OSError) as e:
        raise ValueError(f"{path} is malformed ({type(e).__name__}: {e}): reinstall it with "
                         f"tools/build_screener_universe.py --install-terminals") from e


def terminal_exit(symbol: str, last_session: date, terminals: DelistingTerminals) -> Tuple[Optional[float], str]:
    """``(price, kind)`` for a delisting whose last session is ``last_session``.  ``price`` None = use the last close.  A terminal
    recorded for another last session (a reused ticker, a different event) is not this delisting's: ``(None, 'unknown')``."""
    row = terminals.get(symbol)
    if row is None or row[0] != last_session:
        return None, "unknown"
    return row[2], row[1]
