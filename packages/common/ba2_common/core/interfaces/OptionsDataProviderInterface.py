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
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Iterator, List, Optional, Set


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

    GREEKS are intentionally absent: the platform derives them itself by Black-Scholes
    inversion of the contract's own close (see backtest/option_greeks.py), so a vendor need
    only supply prices. ``bid``/``ask`` are optional — Alpaca's bars endpoint returns none,
    ThetaData's EOD report does — and where present they give a real spread instead of a
    synthesized one.

    ``iv`` is the one exception, and it is OPTIONAL rather than derived-only: dxfeed (via
    TastyTrade) puts ``imp_volatility`` on every candle, and a vendor print beats an
    inversion — the inversion needs a risk-free rate and a dividend assumption, and it
    cannot run at all where the close is missing. A vendor that does NOT serve IV leaves
    this ``None`` and the platform inverts as before; ``None`` must never be coerced to 0.0,
    which downstream reads as a free option rather than as "unknown".

    OHLC IS OPTIONAL, AND FOR THE SAME REASON (2026-09-03). An EOD bar's OHLC is a TRADE
    statistic: on a day the contract did not trade there is no open, high, low or close, and
    the vendors say so by reporting 0.0. Measured on ThetaData across 4 underlyings x 4 years
    (4,944 rows, zero exceptions): ``close > 0`` if and only if ``volume > 0``, and on a
    no-trade row open/high/low are 0.0 too. 44.9% of a liquid chain's rows are no-trade days,
    and 28.3% of the chain carries a real two-sided quote on such a day at a MEDIAN MID OF
    $60.75 — i.e. storing that 0.0 as a price marks a $60 option worthless.

    So ``None`` here means "did not trade", NOT "worthless", and consumers must treat the two
    differently. A no-trade row is NOT price-less: ``bid``/``ask`` carry the day's real quote,
    which is the correct mark for it. Only a row with neither a trade nor a quote is genuinely
    empty, and providers drop those rather than emit them.

    ``bid`` may legitimately be 0.0 and that is a REAL quote, not a missing one — it means
    nobody is bidding, which is exactly the information a liquidity gate needs. (Verified on
    ThetaData: a 0.00 bid still carries a real ``bid_exchange`` stamp and a real ask with
    size.) Coercing a 0.0 bid to ``None`` destroys the distinction between "we know nobody
    bids" and "we do not know", so it must not be done.
    """
    occ_symbol: str
    bar_date: date
    #: OHLC of the day's TRADES, or None on a day the contract did not trade (see above).
    #: None must never be coerced to 0.0 — that reads downstream as a free option.
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[int] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    open_interest: Optional[int] = None
    #: Vendor-supplied implied volatility as a DECIMAL (0.2841 == 28.41%), or None.
    #: Appended LAST so every existing positional construction is unaffected.
    iv: Optional[float] = None


@dataclass
class CandleBatch:
    """The outcome of one ``fetch_bars_detailed`` call — the per-contract-resolution-tracking
    sibling of ``fetch_eod_bars``, used by ``tools/warm_options_history.py``'s retry/requeue
    loop so a partial answer never re-fetches contracts it already has.

    Moved here from the tastytrade provider (2026-09-02, ThetaData integration): this is
    vendor-neutral by construction — a provider whose API is a plain request/response (no
    streaming "still pending" state, e.g. ThetaData) simply always returns ``unresolved``
    empty, while a streaming vendor (TastyTrade/DXLink) can leave contracts unresolved for a
    retry to pick up. ``empty`` and ``unresolved`` must never be merged: ``empty`` is a
    durable fact to record, ``unresolved`` is work still owed.
    """
    bars: List["OptionEodBar"] = field(default_factory=list)
    empty: Set[str] = field(default_factory=set)
    unresolved: Set[str] = field(default_factory=set)
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        """True when every requested contract was accounted for."""
        return not self.unresolved


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
