"""ThetaData v3 historical-options provider.

Chosen because it is the cheapest way to get PRE-2024 option history: Alpaca's floor is a
hard 2024-01-18 at every tier (measured), while ThetaData's tiers go 4y / 8y / 12y back
($40 / $80 / $160 per month as of 2026-07). That extra depth — not price — is the reason
this exists; Alpaca's own $99 OPRA upgrade buys quality but no additional history.

TRANSPORT: ThetaData is NOT a cloud API with a bearer key. A local "Theta Terminal"
process authenticates the subscription and serves REST on 127.0.0.1:25503; requests carry
no credentials. So the failure mode to expect during setup is a CONNECTION error (terminal
not running), not a 401 — hence the explicit, actionable error below.

BULK SHAPE: one ``/v3/option/history/eod`` call with ``expiration=*&strike=*&right=both``
returns an entire chain's EOD across the whole date range, so a build is one request per
underlying rather than Alpaca's thousands of per-contract batches. Both discovery and bars
are served from that single response (cached per (underlying, window)) instead of issuing
separate listing calls.

Docs: https://docs.thetadata.us/operations/option_history_eod.html
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import date, datetime
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from ba2_common.core.interfaces.OptionsDataProviderInterface import (
    OptionContractMeta, OptionEodBar, OptionsDataProviderInterface,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = os.getenv("THETADATA_BASE_URL", "http://127.0.0.1:25503")
# Per-tier history depth (Options Value 4y / Standard 8y / Pro 12y). Not auto-detected —
# the terminal does not advertise the plan — so it is configurable and defaults to the
# cheapest paid tier, which is the conservative choice: too-shallow only rejects windows
# the caller could have had, whereas too-deep would silently build a cache full of gaps.
_DEFAULT_HISTORY_YEARS = float(os.getenv("THETADATA_HISTORY_YEARS", "4"))


def _occ_symbol(underlying: str, expiry: date, right: str, strike: float) -> str:
    """Build the OCC symbol the cache keys on: ROOT + YYMMDD + C/P + strike*1000 (8 digits).

    ThetaData returns the contract as separate symbol/expiration/strike/right columns, so
    the canonical OCC id has to be reconstructed to match what the rest of the platform
    (and the Alpaca-built caches) use.
    """
    cp = "C" if str(right).lower().startswith("c") else "P"
    return f"{underlying.upper()}{expiry:%y%m%d}{cp}{int(round(float(strike) * 1000)):08d}"


def _parse_date(value: str) -> Optional[date]:
    """ThetaData accepts/returns YYYYMMDD or YYYY-MM-DD depending on field and version."""
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _f(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    if v is None or str(v).strip() == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # ThetaData uses 0 for "no quote" on bid/ask; a real option never trades at 0.00 bid AND
    # ask, and passing 0 through would look like a free contract to the arb guard.
    return f


def _i(row: dict, key: str) -> Optional[int]:
    f = _f(row, key)
    return int(f) if f is not None else None


class ThetaDataOptionsProvider(OptionsDataProviderInterface):
    """Historical options via a locally-running Theta Terminal (v3 REST)."""

    name = "thetadata"

    def __init__(self, base_url: Optional[str] = None,
                 history_years: Optional[float] = None,
                 timeout: int = 120):
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.history_years = float(history_years if history_years is not None
                                   else _DEFAULT_HISTORY_YEARS)
        self.timeout = timeout
        # (underlying, start, end) -> parsed rows. A build calls discover_contracts then
        # fetch_eod_bars over the SAME window, and the bulk endpoint already returned both;
        # caching avoids paying for that (large) response twice.
        self._bulk_cache: Dict[Tuple[str, str, str], List[dict]] = {}

    # -- interface ------------------------------------------------------
    def history_floor(self) -> date:
        today = date.today()
        return today.replace(year=today.year - int(self.history_years))

    def discover_contracts(self, underlying: str, *, expiry_gte: date, expiry_lte: date,
                           strike_min: Optional[float] = None,
                           strike_max: Optional[float] = None,
                           max_contracts: Optional[int] = None) -> List[OptionContractMeta]:
        # Derived from the bulk EOD response rather than the separate list-expirations /
        # list-strikes endpoints: those would report contracts that exist but have no EOD
        # rows in the window, which would then be written to the chain table as permanently
        # empty. Deriving from actual data keeps the chain and bars consistent by
        # construction.
        rows = self._bulk_eod(underlying, expiry_gte, expiry_lte)
        seen: Dict[str, OptionContractMeta] = {}
        for r in rows:
            exp = _parse_date(r.get("expiration", ""))
            strike = _f(r, "strike")
            if exp is None or strike is None:
                continue
            if exp < expiry_gte or exp > expiry_lte:
                continue
            if strike_min is not None and strike < strike_min:
                continue
            if strike_max is not None and strike > strike_max:
                continue
            right = str(r.get("right", "")).lower()
            otype = "call" if right.startswith("c") else "put"
            occ = _occ_symbol(underlying, exp, right, strike)
            if occ not in seen:
                seen[occ] = OptionContractMeta(occ_symbol=occ, underlying=underlying.upper(),
                                               option_type=otype, strike=strike, expiry=exp)
        out = list(seen.values())
        if max_contracts is not None and len(out) > max_contracts:
            # Keep strikes nearest the band centre — near-the-money is what gets selected.
            if strike_min is not None and strike_max is not None:
                centre = (strike_min + strike_max) / 2.0
            else:
                strikes = sorted(c.strike for c in out)
                centre = strikes[len(strikes) // 2]
            out = sorted(out, key=lambda c: abs(c.strike - centre))[:max_contracts]
        return out

    def fetch_eod_bars(self, contracts: Iterable[OptionContractMeta], *,
                       start: date, end: date) -> Iterator[OptionEodBar]:
        wanted = {c.occ_symbol for c in contracts}
        if not wanted:
            return
        by_underlying: Dict[str, List[OptionContractMeta]] = {}
        for c in contracts:
            by_underlying.setdefault(c.underlying.upper(), []).append(c)

        for underlying, group in by_underlying.items():
            exp_lo = min(c.expiry for c in group)
            exp_hi = max(c.expiry for c in group)
            for r in self._bulk_eod(underlying, exp_lo, exp_hi, start=start, end=end):
                exp = _parse_date(r.get("expiration", ""))
                strike = _f(r, "strike")
                bar_date = _parse_date(r.get("created", "") or r.get("date", ""))
                if exp is None or strike is None or bar_date is None:
                    continue
                if bar_date < start or bar_date > end:
                    continue
                occ = _occ_symbol(underlying, exp, str(r.get("right", "")), strike)
                if occ not in wanted:
                    continue
                close = _f(r, "close")
                if close is None:
                    continue  # a row with no close is not a usable bar
                yield OptionEodBar(
                    occ_symbol=occ, bar_date=bar_date,
                    open=_f(r, "open") if _f(r, "open") is not None else close,
                    high=_f(r, "high") if _f(r, "high") is not None else close,
                    low=_f(r, "low") if _f(r, "low") is not None else close,
                    close=close,
                    volume=_i(r, "volume"),
                    bid=_f(r, "bid") or None,   # 0 -> None: "no quote", not a free option
                    ask=_f(r, "ask") or None,
                    open_interest=_i(r, "open_interest"),
                )

    # -- transport ------------------------------------------------------
    def _bulk_eod(self, underlying: str, expiry_gte: date, expiry_lte: date,
                  start: Optional[date] = None, end: Optional[date] = None) -> List[dict]:
        """One bulk EOD request covering the whole chain + window, memoized per window."""
        s = (start or expiry_gte).strftime("%Y%m%d")
        e = (end or expiry_lte).strftime("%Y%m%d")
        key = (underlying.upper(), s, e)
        cached = self._bulk_cache.get(key)
        if cached is not None:
            return cached
        rows = self._get_csv("/v3/option/history/eod", {
            "symbol": underlying.upper(),
            "expiration": "*",     # whole chain in one call
            "strike": "*",
            "right": "both",
            "start_date": s,
            "end_date": e,
            "format": "csv",
        })
        self._bulk_cache[key] = rows
        return rows

    def _get_csv(self, path: str, params: dict) -> List[dict]:
        import requests

        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.ConnectionError as e:
            # The expected setup failure — be explicit rather than surfacing a bare socket
            # error, because "no credentials in the request" makes this easy to misdiagnose
            # as an auth problem.
            raise RuntimeError(
                f"Cannot reach the Theta Terminal at {self.base_url}. ThetaData v3 serves "
                f"REST from a LOCAL terminal process (there is no cloud API key): start the "
                f"Theta Terminal and sign in with the subscribed account, then retry. "
                f"Override the address with THETADATA_BASE_URL. ({e})"
            ) from e
        if resp.status_code == 472:
            # ThetaData's documented "no data for this request" — an empty window is normal.
            return []
        resp.raise_for_status()
        text = resp.text.strip()
        if not text:
            return []
        return list(csv.DictReader(io.StringIO(text)))
