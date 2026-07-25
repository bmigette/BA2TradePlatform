"""Vendor-neutral INGEST interface for historical option data.

This is the seam that lets the offline options cache be built from a DIFFERENT vendor.
It is deliberately separate from ``OptionsAccountInterface`` (which is the BROKER side:
submitting orders, live quotes, buying power) — this one only reads history in bulk to
populate ``option_chain`` / ``option_bar``, whose schema is already vendor-agnostic.

Why it exists: the cache builder was hard-wired to Alpaca, whose options history starts
2024-01-18 (MEASURED, not documented — four long-dated contracts trading well before 2024
all return their first bar on exactly that date, and the "history to 2016" docs page is
wrong). That floor is a vendor limit, not a subscription one: Alpaca's Algo Trader Plus
buys OPRA-quality prints but ZERO extra history. Extending the backtest window past 2024
therefore requires a different vendor, which requires this seam.

Two shapes of vendor API are supported by the same two methods:
  * per-contract (Alpaca): discover contracts, then request bars for a list of OCC symbols.
  * bulk-by-underlying (ThetaData): one request returns a whole chain's EOD across a date
    range; ``fetch_eod_bars`` filters that down to the requested contract set.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Iterator, List, Optional


@dataclass(frozen=True)
class OptionContractMeta:
    """One option contract's identity — the fields the cache's chain rows need."""
    occ_symbol: str
    underlying: str
    option_type: str          # "call" | "put" (matches OptionRight values)
    strike: float
    expiry: date


@dataclass(frozen=True)
class OptionEodBar:
    """One contract's end-of-day bar.

    ``iv``/greeks are intentionally ABSENT: the platform derives them itself by
    Black-Scholes inversion of the contract's own close (see backtest/option_greeks.py),
    so a vendor need only supply prices. ``bid``/``ask`` are optional — Alpaca's bars
    endpoint returns none, ThetaData's EOD report does — and where present they give a
    real spread instead of a synthesized one.
    """
    occ_symbol: str
    bar_date: date
    open: float
    high: float
    low: float
    close: float
    volume: Optional[int] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    open_interest: Optional[int] = None


class OptionsDataProviderInterface(ABC):
    """Bulk historical option data source used to BUILD the offline cache."""

    #: Registry key, e.g. "alpaca" / "thetadata".
    name: str = "unknown"

    @abstractmethod
    def history_floor(self) -> date:
        """Earliest date this vendor has ANY option data for.

        Callers reject earlier windows up-front rather than silently building a cache
        with empty leading months (which surfaces much later as an expert that never
        trades). Must reflect the vendor+subscription actually in use.
        """

    @abstractmethod
    def discover_contracts(
        self,
        underlying: str,
        *,
        expiry_gte: date,
        expiry_lte: date,
        strike_min: Optional[float] = None,
        strike_max: Optional[float] = None,
        max_contracts: Optional[int] = None,
    ) -> List[OptionContractMeta]:
        """Contracts for ``underlying`` expiring in the window — INCLUDING EXPIRED ones.

        Expired contracts are the whole point for a historical build; a vendor whose
        listing endpoint defaults to "currently tradable" must explicitly include them.
        ``max_contracts`` caps the build size and, when it bites, implementations should
        keep the strikes NEAREST the band centre (near-the-money is what strategies
        select), not an arbitrary slice.
        """

    @abstractmethod
    def fetch_eod_bars(
        self,
        contracts: Iterable[OptionContractMeta],
        *,
        start: date,
        end: date,
    ) -> Iterator[OptionEodBar]:
        """Daily bars for ``contracts`` between ``start`` and ``end`` (both inclusive).

        Yields lazily so a multi-year build streams into the cache instead of being
        assembled in memory. Contracts with no data in the window simply yield nothing —
        that is normal (a contract listed later than ``start``), not an error.
        """
