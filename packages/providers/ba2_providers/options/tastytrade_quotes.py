"""TastyTrade option chain QUOTE SNAPSHOT over REST (iv-v1 live, broker quote source). Read-only.

Three endpoints, all market data (measured on the live API 2026-11-15, see
docs/plans/2026-11-15-option-iv-conditions.md section 6.6):

* ``GET /option-chains/{underlying}/nested`` -- the listed contracts (root, expiries, strikes, the
  contract symbols). Only the STANDARD chain whose root is the underlying is read (an adjusted root
  delivers something else).
* ``GET /market-data/by-type?equity={underlying}`` -- the underlying's ``close`` and ``summary-date``
  (the session it is filed under): the centre of the strike band.
* ``GET /market-data/by-type?equity-option=...`` -- at most ``BATCH`` (100, the endpoint's combined
  limit) contracts per call: ``bid``, ``ask``, ``summary-date``, ``updated-at``, ``last``, ``volume``.
  (``prev-close-date`` is NOT usable: it reads 1970-01-01 or an old date on many contracts.)

Pure over a ``get_json(path, params) -> data`` callable, so the account class
(``TastyTradeAccount.get_option_chain_quote_snapshot``, through its own session) and the operator tools
share one mapping, and the tests run on recorded payloads.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ba2_common.core.option_chain_quotes import (
    FETCH_STRIKE_BAND, OptionChainQuote, OptionChainQuoteSnapshot, OptionChainQuotesFetchError,
)

__all__ = ["fetch_chain_quote_snapshot", "parse_quote_item", "occ_from_tt_symbol", "BATCH", "SOURCE_KIND"]

SOURCE_KIND = "tastytrade"
BATCH = 100


def occ_from_tt_symbol(symbol: str) -> str:
    """``"MSFT  261120C00502500"`` -> ``"MSFT261120C00502500"`` (the OCC symbol without padding)."""
    return "".join(str(symbol).split()).upper()


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _day(v: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _stamp(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_quote_item(item: Dict[str, Any], meta: Tuple[date, str, float]) -> OptionChainQuote:
    expiry, option_type, strike = meta
    return OptionChainQuote(
        occ=occ_from_tt_symbol(item["symbol"]), expiry=expiry, option_type=option_type, strike=strike,
        bid=_num(item.get("bid")), ask=_num(item.get("ask")),
        summary_date=_day(item.get("summary-date")), updated_at=_stamp(item.get("updated-at")),
        last=_num(item.get("last")), volume=_num(item.get("volume")))


def _underlying_close(get_json: Callable[..., Any], underlying: str) -> Tuple[Optional[float], Optional[date]]:
    data = get_json("/market-data/by-type", {"equity": [underlying]})
    items = (data or {}).get("items") or []
    for it in items:
        if str(it.get("symbol", "")).upper() == underlying:
            px = _num(it.get("close"))
            if px is None:
                px = _num(it.get("last"))
            return px, _day(it.get("summary-date"))
    return None, None


def _wanted_contracts(chain: Dict[str, Any], underlying: str, spot: float, expiry_min: date, expiry_max: date,
                      band: float) -> Dict[str, Tuple[date, str, float]]:
    out: Dict[str, Tuple[date, str, float]] = {}
    for item in (chain or {}).get("items") or []:
        if str(item.get("root-symbol", "")).upper() != underlying:
            continue                                   # an adjusted root delivers something else
        if str(item.get("option-chain-type", "Standard")) != "Standard":
            continue
        if int(float(item.get("shares-per-contract") or 100)) != 100:
            continue
        for exp in item.get("expirations") or []:
            e = _day(exp.get("expiration-date"))
            if e is None or not (expiry_min <= e <= expiry_max):
                continue
            for k in exp.get("strikes") or []:
                strike = _num(k.get("strike-price"))
                if strike is None or abs(strike / spot - 1.0) > band:
                    continue
                for side, otype in (("call", "call"), ("put", "put")):
                    sym = k.get(side)
                    if sym:
                        out[str(sym)] = (e, otype, strike)
    return out


def fetch_chain_quote_snapshot(get_json: Callable[[str, Optional[Dict[str, Any]]], Any], underlying: str, *,
                               expiry_min: date, expiry_max: date, strike_band: float = FETCH_STRIKE_BAND,
                               account_id: Optional[int] = None, pause: Callable[[], None] = lambda: None,
                               now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                               ) -> OptionChainQuoteSnapshot:
    """The latest quote of every standard contract of ``underlying`` with an expiry in the window and a
    strike within ``strike_band`` of the underlying's close. ``get_json`` returns the response's
    ``data`` object (and raises on an HTTP error). Raises ``OptionChainQuotesFetchError`` on a response
    that cannot be read."""
    sym = str(underlying).strip().upper()
    requests = 0
    try:
        spot, spot_day = _underlying_close(get_json, sym)
        requests += 1
        chain = get_json(f"/option-chains/{sym}/nested", None)
        requests += 1
    except (KeyError, TypeError, ValueError) as e:
        raise OptionChainQuotesFetchError(f"TastyTrade chain/underlying response for {sym} unreadable: {e}") from e
    fetched = now()
    if spot is None or spot <= 0:
        return OptionChainQuoteSnapshot(sym, SOURCE_KIND, fetched, None, spot_day, (), 0, account_id,
                                        strike_band, expiry_min, expiry_max, requests)
    wanted = _wanted_contracts(chain, sym, spot, expiry_min, expiry_max, strike_band)
    names = sorted(wanted)
    quotes: List[OptionChainQuote] = []
    for i in range(0, len(names), BATCH):
        if i:
            pause()
        batch = names[i:i + BATCH]
        try:
            data = get_json("/market-data/by-type", {"equity-option": batch})
            requests += 1
            for item in (data or {}).get("items") or []:
                meta = wanted.get(str(item.get("symbol")))
                if meta is not None:
                    quotes.append(parse_quote_item(item, meta))
        except (KeyError, TypeError, ValueError) as e:
            raise OptionChainQuotesFetchError(f"TastyTrade market-data response for {sym} unreadable: {e}") from e
    return OptionChainQuoteSnapshot(sym, SOURCE_KIND, fetched, spot, spot_day, tuple(quotes), len(names),
                                    account_id, strike_band, expiry_min, expiry_max, requests)
