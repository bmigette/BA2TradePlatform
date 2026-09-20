"""Response shape for ``GET /backtests/{id}/trade-chart`` (spec 2026-09-20, step 2).

The models are the contract the frontend consumes, and the route validates the
service's output through them so a field renamed in the service fails loudly here
instead of reaching a chart as `undefined`.

Semantics that the types alone cannot carry:

* A money/contract value is ``None`` when it was NOT RECORDED. It is never 0 and
  never 1 -- ``multiplierRecorded`` says whether the number beside it is evidence
  or a serialization default.
* ``quality`` on a historical reference says how the underlying price was
  obtained, because "we could not tell" must be distinguishable from "it was
  exactly this at the fill".
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

ReferenceQuality = Literal[
    "recorded_snapshot", "last_known_bar", "daily_reference", "unavailable"
]
CacheStatus = Literal["complete", "partial", "missing", "unavailable"]
PositionStatus = Literal["closed", "open_at_end", "unknown"]


class HistoricalReference(BaseModel):
    """The underlying at one event, with its provenance."""

    price: Optional[float] = None
    eventAt: Optional[str] = None
    observedAt: Optional[str] = None
    availableAt: Optional[str] = None
    quality: ReferenceQuality = "unavailable"
    source: Optional[str] = None
    reason: Optional[str] = None


class ContractDetail(BaseModel):
    """Greeks/IV/OI for one contract at one event, as the option cache recorded them.

    A NULL field is NOT RECORDED -- the column migration left older rows NULL deliberately.
    ``quality`` says how much of the row was actually there: ``cache_bar`` (iv present),
    ``partial`` (the bar exists but the greeks were never fetched) or ``unavailable``.
    """

    asOf: Optional[str] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    openInterest: Optional[float] = None
    volume: Optional[float] = None
    quality: Literal["cache_bar", "partial", "unavailable"] = "unavailable"
    source: Optional[str] = None
    reason: Optional[str] = None


class TradeChartLeg(BaseModel):
    """One saved leg, normalised, with the option terms the recorder published."""

    id: int
    symbol: Optional[str] = None
    underlyingSymbol: Optional[str] = None
    contractSymbol: Optional[str] = None
    optionType: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    multiplier: Optional[float] = None
    multiplierRecorded: bool = False
    direction: Optional[str] = None
    size: Optional[float] = None
    entryAt: Optional[str] = None
    exitAt: Optional[str] = None
    entryPrice: Optional[float] = None
    exitPrice: Optional[float] = None
    pnl: Optional[float] = None
    pnlPercent: Optional[float] = None
    exitReason: Optional[str] = None
    transactionId: Optional[str] = None
    rowBasis: Literal["aggregate_round_trip"] = "aggregate_round_trip"
    positionStatus: PositionStatus = "unknown"
    entryUnderlying: HistoricalReference = Field(default_factory=HistoricalReference)
    exitUnderlying: HistoricalReference = Field(default_factory=HistoricalReference)
    entryContract: ContractDetail = Field(default_factory=ContractDetail)
    exitContract: ContractDetail = Field(default_factory=ContractDetail)
    unavailableFields: List[str] = Field(default_factory=list)


class ChartBar(BaseModel):
    date: str
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None


class UnderlyingHistory(BaseModel):
    symbol: Optional[str] = None
    provider: Optional[str] = None
    interval: Literal["1d"] = "1d"
    cacheStatus: CacheStatus = "unavailable"
    provenance: Literal["run_snapshot", "current_historical_cache", "unknown"] = "unknown"
    bars: List[ChartBar] = Field(default_factory=list)


class Notice(BaseModel):
    code: str
    message: str


class TradeChartContext(BaseModel):
    schemaVersion: Literal[1] = 1
    backtestId: int
    resultDigest: str
    selectedTradeId: int
    transactionId: Optional[str] = None
    legs: List[TradeChartLeg] = Field(default_factory=list)
    underlying: UnderlyingHistory = Field(default_factory=UnderlyingHistory)
    notices: List[Notice] = Field(default_factory=list)
