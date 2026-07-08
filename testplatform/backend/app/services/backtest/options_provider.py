"""As-of-clamped reader over OptionsHistoryCache. Returns ONLY data dated <= the engine
clock (no lookahead). Chain rows are mapped to OptionContract; bars stay dicts."""
from __future__ import annotations
from datetime import date
from typing import List, Optional
from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.types import OptionRight
from .options_cache import OptionsHistoryCache

def _to_contract(r: dict, greeks_row: Optional[dict] = None) -> OptionContract:
    # greeks_row (the AS-OF-CLAMPED daily bar for this contract, when available) carries the
    # POINT-IN-TIME iv/greeks computed by fetch_options.py's Black-Scholes inversion of that
    # day's close (see option_greeks.py) — preferred over the chain row's, which is a single
    # snapshot fixed at the build's start date and goes stale as the backtest clock advances.
    # Only prefer it when its OWN iv actually computed (non-None) — a bar can exist with
    # iv/greeks still None (missing underlying close that day, or a pre-existing cache built
    # before this feature), in which case fall back to the chain row rather than lose greeks.
    g = greeks_row if (greeks_row and greeks_row.get("iv") is not None) else r
    return OptionContract(
        symbol=r["occ_symbol"], underlying=r.get("underlying") or "",
        option_type=OptionRight(r["option_type"]), strike=r["strike"],
        expiry=date.fromisoformat(r["expiry"]), bid=r.get("bid"), ask=r.get("ask"),
        last=r.get("last"), implied_volatility=g.get("iv"), delta=g.get("delta"),
        gamma=g.get("gamma"), theta=g.get("theta"), vega=g.get("vega"),
        open_interest=r.get("open_interest"), volume=r.get("volume"))

class HistoricalOptionsProvider:
    def __init__(self, cache_db: str):
        self.cache = OptionsHistoryCache(cache_db)

    def get_chain(self, underlying: str, as_of: date, *, expiry_min: date, expiry_max: date,
                  option_type: Optional[OptionRight] = None, strike_min: Optional[float] = None,
                  strike_max: Optional[float] = None) -> List[OptionContract]:
        snap = self.cache.latest_chain_as_of(underlying, as_of.isoformat())
        if snap is None:
            return []
        out: List[OptionContract] = []
        for r in self.cache.read_chain(underlying, snap):
            r = {**r, "underlying": underlying}
            exp = date.fromisoformat(r["expiry"])
            if exp < expiry_min or exp > expiry_max:
                continue
            if option_type is not None and r["option_type"] != option_type.value:
                continue
            if strike_min is not None and r["strike"] < strike_min:
                continue
            if strike_max is not None and r["strike"] > strike_max:
                continue
            greeks_row = self.cache.latest_bar_on_or_before(r["occ_symbol"], as_of.isoformat())
            out.append(_to_contract(r, greeks_row))
        return out

    def get_quote(self, occ_symbol: str, as_of: date) -> Optional[OptionQuote]:
        bar = self.cache.read_bar(occ_symbol, as_of.isoformat())
        if bar is None:
            return None
        return OptionQuote(symbol=occ_symbol, bid=None, ask=None, last=bar.get("close"))

    def get_bar(self, occ_symbol: str, as_of: date) -> Optional[dict]:
        return self.cache.read_bar(occ_symbol, as_of.isoformat())

    def get_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        """Mean IV across the underlying's cached contracts as of ``as_of`` — feeds
        ``IVRankCondition``'s rolling history. Reads each contract's AS-OF-CLAMPED daily-bar IV
        (point-in-time, computed by fetch_options.py's Black-Scholes inversion), not the static
        start-date chain snapshot, so this tracks IV changes across the whole backtest window."""
        snap = self.cache.latest_chain_as_of(underlying, as_of.isoformat())
        if snap is None:
            return None
        ivs: List[float] = []
        for r in self.cache.read_chain(underlying, snap):
            bar = self.cache.latest_bar_on_or_before(r["occ_symbol"], as_of.isoformat())
            iv = (bar or {}).get("iv") or r.get("iv")
            if iv:
                ivs.append(float(iv))
        return float(sum(ivs) / len(ivs)) if ivs else None
