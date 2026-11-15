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

Pure over a ``get_json(path, params) -> data`` callable; :class:`TastyTradeMarketDataClient` is the live
one: app-level MARKET-DATA credentials (OAuth client secret + refresh token, app settings
``tastytrade_market_client_secret`` / ``tastytrade_market_refresh_token``), independent of any account
definition. It exchanges the refresh token for a 15-minute access token (TastyTrade does not rotate
refresh tokens: nothing is ever written back), and it can ONLY call the market-data paths above -- any
other path is refused before a request is made (no order, account or position endpoint can be reached).
The credentials never appear in a message, a log or a repr.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ba2_common.core.option_chain_quotes import (
    FETCH_STRIKE_BAND, OptionChainQuote, OptionChainQuoteSnapshot, OptionChainQuotesFetchError,
    OptionChainSourceUnavailable,
)

__all__ = ["fetch_chain_quote_snapshot", "parse_quote_item", "occ_from_tt_symbol", "BATCH", "SOURCE_KIND",
           "TastyTradeMarketDataClient", "API_URL", "ALLOWED_PATH_PREFIXES"]

SOURCE_KIND = "tastytrade"
BATCH = 100
API_URL = "https://api.tastyworks.com"          # production only: the sandbox's quotes are fake
API_VERSION = "20251101"
#: The ONLY paths the market-data client may call.
ALLOWED_PATH_PREFIXES = ("/option-chains/", "/market-data/by-type")
HTTP_TIMEOUT_S = 20.0


class TastyTradeMarketDataClient:
    """Market-data-only TastyTrade client over app-level OAuth credentials (see the module docstring).

    ``get_json(path, params)`` returns the response's ``data`` object. Raises
    ``OptionChainSourceUnavailable`` when the credentials are blank or rejected (HTTP 401/403 on the token
    exchange), ``OptionChainQuotesFetchError`` on any other failure; a path outside
    ``ALLOWED_PATH_PREFIXES`` raises ``PermissionError`` before any request."""

    def __init__(self, client_secret: Optional[str], refresh_token: Optional[str], *, http: Any = None,
                 clock: Callable[[], float] = None):
        import time as _time

        self.__secret = (client_secret or "").strip()
        self.__refresh = (refresh_token or "").strip()
        self._http = http
        self._clock = clock or _time.monotonic
        self._token: Optional[str] = None
        self._expires = 0.0
        self.requests = 0

    def __repr__(self) -> str:                 # never the credentials
        return "TastyTradeMarketDataClient(<credentials hidden>)"

    @property
    def configured(self) -> bool:
        return bool(self.__secret and self.__refresh)

    def _session(self):
        if self._http is None:
            import requests

            self._http = requests.Session()
            self._http.headers.update({"Accept": "application/json", "Content-Type": "application/json",
                                       "Accept-Version": API_VERSION, "User-Agent": "ba2-market-data"})
        return self._http

    def _redact(self, text: str) -> str:
        for s in (self.__secret, self.__refresh, self._token):
            if s:
                text = text.replace(s, "***")
        return text

    def _access_token(self) -> str:
        if self._token and self._clock() < self._expires - 60:
            return self._token
        if not self.configured:
            raise OptionChainSourceUnavailable(
                "TastyTrade market-data credentials are not set (app settings tastytrade_market_client_secret "
                "and tastytrade_market_refresh_token)")
        try:
            r = self._session().post(API_URL + "/oauth/token", json={
                "grant_type": "refresh_token", "client_secret": self.__secret, "refresh_token": self.__refresh},
                timeout=HTTP_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 -- transport errors of any HTTP library, redacted
            raise OptionChainQuotesFetchError(f"TastyTrade token exchange failed: {self._redact(type(e).__name__ + ': ' + str(e))[:200]}") from None
        self.requests += 1
        if r.status_code in (400, 401, 403):
            raise OptionChainSourceUnavailable(
                f"TastyTrade rejected the market-data credentials (HTTP {r.status_code} on the token exchange); "
                f"check tastytrade_market_client_secret / tastytrade_market_refresh_token")
        if r.status_code // 100 != 2:
            raise OptionChainQuotesFetchError(f"TastyTrade token exchange failed: HTTP {r.status_code}")
        body = r.json()
        self._token = str(body["access_token"])
        self._expires = self._clock() + float(body.get("expires_in", 900))
        return self._token

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not str(path).startswith(ALLOWED_PATH_PREFIXES):
            raise PermissionError(f"the TastyTrade market-data client may not call {path!r}")
        token = self._access_token()
        try:
            r = self._session().get(API_URL + path, params=params, timeout=HTTP_TIMEOUT_S,
                                    headers={"Authorization": f"Bearer {token}"})
        except Exception as e:  # noqa: BLE001 -- transport errors, redacted
            raise OptionChainQuotesFetchError(f"TastyTrade {path}: {self._redact(type(e).__name__ + ': ' + str(e))[:200]}") from None
        self.requests += 1
        if r.status_code == 401:
            self._token = None                 # expired early: one fresh token next call
            raise OptionChainQuotesFetchError(f"TastyTrade {path}: HTTP 401 (access token expired; retried next attempt)")
        if r.status_code // 100 != 2:
            raise OptionChainQuotesFetchError(f"TastyTrade {path}: HTTP {r.status_code}: {self._redact(r.text)[:160]}")
        try:
            return r.json()["data"]
        except (ValueError, KeyError) as e:
            raise OptionChainQuotesFetchError(f"TastyTrade {path}: response has no data ({type(e).__name__})") from None


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
                               pause: Callable[[], None] = lambda: None,
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
        return OptionChainQuoteSnapshot(sym, SOURCE_KIND, fetched, None, spot_day, (), 0,
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
                                    strike_band, expiry_min, expiry_max, requests)
