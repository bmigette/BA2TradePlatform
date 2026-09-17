"""The evaluation-scoped context the market-condition entry gates read.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` §4.1, §5, §7.

A ``MarketConditionContext`` is immutable and scoped to ONE decision: the full decision time,
the trading session it belongs to, the session whose completed daily bar the gates may read
(``prior_session`` under ``prior_session_v1``), the provider/source profile, the timing policy,
the calculator version, a read-only ``MarketConditionReader`` and an optional capture/replay
recorder.

It reaches the conditions through the host-installed resolver seam in ``TradeConditions``
(``set_market_condition_context_resolver``), mirroring the provider-resolver seam. It is
resolved LAZILY, only when a market-condition leaf that survived rule building is evaluated:
a ruleset without such leaves performs no additional clock or market-data reads.

This module is pure data + validation: no I/O, no provider access.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable

from ba2_common.core.market_conditions import Observation

#: v1 timing policy: a decision made during session S reads the observation computed from the
#: completed daily bars through the PRIOR regular session (never S's own, still-forming bar).
TIMING_POLICY_PRIOR_SESSION_V1 = "prior_session_v1"
TIMING_POLICIES = (TIMING_POLICY_PRIOR_SESSION_V1,)


@runtime_checkable
class FeatureRowLike(Protocol):
    """A computed feature row: ``{field name: Observation}``. ``MarketConditionValues`` (the
    ohlcv-v1 trio) satisfies it structurally, as does a field-generic row."""

    def by_field(self) -> Mapping[str, Observation]:
        ...


#: ``recorder(symbol, session, row)`` -- called once per successful (valid) evaluation.
MarketConditionRecorder = Callable[[str, date, FeatureRowLike], None]


@runtime_checkable
class MarketConditionReader(Protocol):
    """Read-only access to computed feature rows."""

    def observe(self, symbol: str, session: date) -> Optional[FeatureRowLike]:
        """The feature row for ``symbol`` computed through ``session``; ``None`` when no row
        exists for that symbol/session (reported as ``missing_session``, never a default)."""
        ...


class DictMarketConditionReader:
    """A ``MarketConditionReader`` over an in-memory ``{(symbol, session): row}`` mapping.
    Used by tests and as the backing store of the backtest adapter.

    The mapping is COPIED ON CONSTRUCTION (O(rows)), so later mutation of the caller's dict
    cannot change a decision -- and an adapter must build the reader ONCE (per run / per
    loaded window), never per decision."""

    def __init__(self, rows: Mapping[Tuple[str, date], FeatureRowLike]):
        self._rows: Dict[Tuple[str, date], FeatureRowLike] = dict(rows)

    def observe(self, symbol: str, session: date) -> Optional[FeatureRowLike]:
        return self._rows.get((symbol, session))

    def __len__(self) -> int:
        return len(self._rows)


def _is_plain_date(d) -> bool:
    # datetime is a date subclass; a session label is a calendar day, never a timestamp.
    return isinstance(d, date) and not isinstance(d, datetime)


@dataclass(frozen=True)
class MarketConditionContext:
    decision_time: datetime
    session_label: date
    prior_session: date
    source_profile: str
    timing_policy: str
    calc_version: str
    reader: MarketConditionReader
    recorder: Optional[MarketConditionRecorder] = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_time, datetime):
            raise ValueError(f"decision_time must be a datetime, got {type(self.decision_time).__name__}")
        if self.decision_time.tzinfo is None or self.decision_time.utcoffset() is None:
            raise ValueError(f"decision_time must be timezone-aware, got naive {self.decision_time!r}")
        if not _is_plain_date(self.session_label):
            raise ValueError(f"session_label must be a date, got {self.session_label!r}")
        if not _is_plain_date(self.prior_session):
            raise ValueError(f"prior_session must be a date, got {self.prior_session!r}")
        if not self.prior_session < self.session_label:
            raise ValueError(
                f"prior_session {self.prior_session} must precede session_label {self.session_label}")
        if not self.source_profile or not isinstance(self.source_profile, str):
            raise ValueError(f"source_profile must be a non-empty string, got {self.source_profile!r}")
        if self.timing_policy not in TIMING_POLICIES:
            raise ValueError(f"unknown timing_policy {self.timing_policy!r}; known: {TIMING_POLICIES!r}")
        if not self.calc_version or not isinstance(self.calc_version, str):
            raise ValueError(f"calc_version must be a non-empty string, got {self.calc_version!r}")
        if not callable(getattr(self.reader, "observe", None)):
            raise ValueError(f"reader must provide observe(symbol, session), got {type(self.reader).__name__}")
        if self.recorder is not None and not callable(self.recorder):
            raise ValueError("recorder must be callable or None")
