"""FMP company-profile ``ipoDate`` of every symbol of the Alpaca stores: the EVIDENCE the build uses to tell a REUSED ticker from a
long suspension of the same company (``build.py``, docstring "REUSED TICKERS").

A ticker whose series has a gap of >= 20 sessions may join two companies (Vonage -> Venture Global as VG).  The current company's
first trade date is its ``ipoDate``; when it falls at the END of the gap, the bars before the gap are another company's.

INPUT  FMP ``/api/v3/profile/<SYM,SYM,...>`` (up to BATCH symbols per request; the key is the AppSetting FMP_API_KEY of ``--db``,
       read only when a symbol is missing from the cache, never printed; request errors are redacted).
OUTPUT ``<out>/_meta/ipo_dates.json``::

           {"fetched_at": ts, "source": "fmp /api/v3/profile", "symbols": {SYM: "YYYY-MM-DD" | null}}

       ``null`` = FMP returned no profile for the symbol, or a profile without a valid ipoDate (listed, never guessed: the build then
       treats the symbol's gaps as having no evidence).  The file is the CACHE: a symbol already in it is never fetched again (a rerun
       over the same stores is OFFLINE and byte-identical); ``refresh=True`` / ``--refresh-ipo-dates`` fetches everything again.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

from ba2_providers.fmp_common import FMPError, fmp_http_get, redact
from ba2_providers.ohlcv.alpaca_history.split_build import SplitTableBuildError, atomic_write

IPO_DATES_NAME = "ipo_dates.json"
PROFILE_URL = "https://financialmodelingprep.com/api/v3/profile/"
BATCH = 100


def ipo_dates_path(out: str) -> str:
    return os.path.join(out, "_meta", IPO_DATES_NAME)


def load_ipo_dates(path: str) -> Optional[Dict[str, Optional[str]]]:
    """``{symbol: ISO date | None}`` of the file, or None when it does not exist.  A malformed file RAISES (a half-read evidence table
    would turn a reused ticker into a normal one without a word)."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)["symbols"]
        for sym, v in raw.items():
            if v is not None:
                date.fromisoformat(v)
        return dict(raw)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise SplitTableBuildError(f"{path} is malformed ({type(e).__name__}: {e}): delete it and rerun the ipo-dates step") from e


def _valid_date(text: Any) -> Optional[str]:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return date.fromisoformat(text.strip()[:10]).isoformat()
    except ValueError:
        return None


def make_profile_getter(key: str, getter: Optional[Callable] = None) -> Callable[[List[str]], List[Dict[str, Any]]]:
    def get(symbols: List[str]) -> List[Dict[str, Any]]:
        try:
            r = fmp_http_get(PROFILE_URL + ",".join(symbols), params={"apikey": key}, endpoint="profile", timeout=120, getter=getter)
        except (requests.RequestException, FMPError) as e:
            raise SplitTableBuildError(f"FMP profile {symbols[0]}..{symbols[-1]}: {type(e).__name__}: {redact(e)}") from None
        j = r.json()
        if not isinstance(j, list):
            raise SplitTableBuildError(f"FMP profile {symbols[0]}..{symbols[-1]}: unexpected payload {redact(str(j)[:200])!r}")
        return j
    return get


def fetch_ipo_dates(symbols: Iterable[str], out: str, get: Callable[[List[str]], List[Dict[str, Any]]],
                    refresh: bool = False, log: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    """Make ``<out>/_meta/ipo_dates.json`` cover ``symbols`` (store spelling, '-' for share classes); returns a summary.  ``get`` is
    called only for symbols not yet in the file (every one when ``refresh``).  A response symbol that was not asked for is ignored."""
    path = ipo_dates_path(out)
    have = {} if refresh else (load_ipo_dates(path) or {})
    want = sorted(set(symbols))
    todo = [s for s in want if s not in have]
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    got = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        by_sym = {str(r.get("symbol", "")).upper(): _valid_date(r.get("ipoDate")) for r in get(batch)}
        for s in batch:
            have[s] = by_sym.get(s)
            got += have[s] is not None
        log(f"ipo-dates: {min(i + BATCH, len(todo))}/{len(todo)}")
    if todo or refresh or not os.path.isfile(path):
        old = {}
        if os.path.isfile(path) and not refresh:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        body = {"fetched_at": fetched_at if todo or refresh or not old else old["fetched_at"],
                "source": "fmp /api/v3/profile", "symbols": dict(sorted(have.items()))}
        atomic_write(path, json.dumps(body, indent=0).encode("utf-8"))
    return {"path": path, "symbols_wanted": len(want), "fetched": len(todo), "with_ipo_date": got,
            "without_ipo_date": len(todo) - got, "cached": len(want) - len(todo), "total_in_file": len(have)}
