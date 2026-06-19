"""As-of-clamped reader over OptionsHistoryCache. Returns ONLY data dated <= the engine
clock (no lookahead). Chain rows are mapped to OptionContract; bars stay dicts."""
from __future__ import annotations
from datetime import date
from typing import List, Optional
from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.types import OptionRight
from .options_cache import OptionsHistoryCache

def _to_contract(r: dict) -> OptionContract:
    return OptionContract(
        symbol=r["occ_symbol"], underlying=r.get("underlying") or "",
        option_type=OptionRight(r["option_type"]), strike=r["strike"],
        expiry=date.fromisoformat(r["expiry"]), bid=r.get("bid"), ask=r.get("ask"),
        last=r.get("last"), implied_volatility=r.get("iv"), delta=r.get("delta"),
        gamma=r.get("gamma"), theta=r.get("theta"), vega=r.get("vega"),
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
            out.append(_to_contract(r))
        return out

    def get_quote(self, occ_symbol: str, as_of: date) -> Optional[OptionQuote]:
        bar = self.cache.read_bar(occ_symbol, as_of.isoformat())
        if bar is None:
            return None
        return OptionQuote(symbol=occ_symbol, bid=None, ask=None, last=bar.get("close"))

    def get_bar(self, occ_symbol: str, as_of: date) -> Optional[dict]:
        return self.cache.read_bar(occ_symbol, as_of.isoformat())

    def get_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        snap = self.cache.latest_chain_as_of(underlying, as_of.isoformat())
        if snap is None:
            return None
        rows = [r for r in self.cache.read_chain(underlying, snap) if r.get("iv")]
        return float(sum(r["iv"] for r in rows) / len(rows)) if rows else None
