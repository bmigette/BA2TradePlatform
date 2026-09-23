"""Operator-maintained corrections to the vendor split calendar (BT/live option parity, Part G1b).

WHY. ``split_basis.resolve_symbol_split_basis`` builds the adjusted -> as-traded factor from
FMP's split calendar. The 2026-09-23 sweep (97 symbols, 2020-2025, put-call-parity spot of the
nearest expiry vs the converted FMP close) found price adjustments FMP applies to its daily
closes but never lists in that calendar:

  * spin-offs: HON (Solstice, ex 2025-10-30, x1.0614), NVS (Sandoz, ex 2023-10-04, x1.0575);
  * quarterly stock dividends: SCCO, six of them 2024-08..2025-11 (x1.0057..x1.0101).

A spin-off of ~5% is not provable from prices (it is the size of an ordinary day), so these
cannot be detected by ``check_split_basis``; they are MEASURED once (the parity plateau step)
and recorded here with their evidence.

THE ENTRY KINDS

  * ``add_price_adjustment``  -- the FMP closes carry an adjustment of ``ratio`` at
    ``event_date`` that the calendar lacks: it joins the factor exactly like a calendar split
    (``day < event_date <= basis_date``). No jump proof is asked of it.
  * ``exclude_calendar_event`` -- a calendar row at ``event_date`` that the FMP closes do NOT
    carry: it is dropped from the factor (and its verdict from the proof). The row is matched
    by DATE only, so a calendar row whose ratio FMP does not state (``ratio=nan``, which
    ``resolve_symbol_split_basis`` would otherwise refuse inside the file's range) can be
    excluded too; the entry's own ``ratio`` is informational for this kind (the ratio of the
    row as measured, for the evidence) and is never applied.
  * ``acknowledge_jump`` -- a REAL one-day move (an earnings gap, not a basis defect) that
    sits within ``MIXED_BASIS_WINDOW_DAYS`` of a calendar split and so classifies it
    ``mixed_basis``. ``event_date`` is the jump's bar date and ``ratio`` its close / previous
    close. It removes ONLY that jump from the mixed-basis scan: the split stays in the factor
    and keeps whatever verdict the ratio rule gives it. The jump must still be in the file at
    that date with that ratio (+-``ANCHOR_TOLERANCE``), else it acknowledges nothing (and
    ``resolve_symbol_split_basis`` refuses the symbol, naming it). Honoured by every
    ``check_split_basis`` caller (the warmup preflight and the live drift report included), so
    an acknowledged jump also stops the warmup's re-fetch loop.

VALID ONLY FOR THE BYTES THEY WERE MEASURED ON. Every entry names ``anchors``: (date, FMP close)
pairs of the file it was measured against. A re-fetch that re-bases the history (FMP adding the
missing event itself, a new dividend adjustment) moves those closes, and the entry would then
double count or miss. So any anchor off by more than ``ANCHOR_TOLERANCE`` REFUSES the symbol
(``SplitBasisRefused``): re-measure, never guess.

VERSIONED. ``OVERRIDES_VERSION`` plus a digest of the symbol's own entries goes into
``SymbolSplitBasis.overrides_version`` and so into ``identity()`` and every cache key over
as-traded values. A symbol with no entry keeps ``overrides_version=None`` -- its identity is the
vendor calendar's, unchanged. Bump the version on any edit.

Data: ``docs/plans/2026-09-22-bt-live-option-parity.md`` Part G; the proposed JSON and sweep
evidence (``option_basis_overrides.proposed.json``, ``sessions_ratio_all.csv``).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Optional, Tuple

__all__ = [
    "OVERRIDES_VERSION",
    "ANCHOR_TOLERANCE",
    "KIND_ADD_PRICE_ADJUSTMENT",
    "KIND_EXCLUDE_CALENDAR_EVENT",
    "KIND_ACKNOWLEDGE_JUMP",
    "BasisOverride",
    "acknowledged_jumps",
    "BASIS_OVERRIDES",
    "overrides_for",
    "overrides_version_for",
]

OVERRIDES_VERSION = "2026-09-23.2"

#: An anchor close may differ from the recorded one by at most this fraction (0.1%).
ANCHOR_TOLERANCE = 0.001

KIND_ADD_PRICE_ADJUSTMENT = "add_price_adjustment"
KIND_EXCLUDE_CALENDAR_EVENT = "exclude_calendar_event"
KIND_ACKNOWLEDGE_JUMP = "acknowledge_jump"
_KINDS = (KIND_ADD_PRICE_ADJUSTMENT, KIND_EXCLUDE_CALENDAR_EVENT, KIND_ACKNOWLEDGE_JUMP)


@dataclass(frozen=True)
class BasisOverride:
    symbol: str
    event_date: date
    #: As a calendar ratio: the factor multiplies an adjusted close of a day before
    #: ``event_date`` into the as-traded basis (a spin-off worth 5.75% of the parent -> 1.0575).
    ratio: float
    kind: str
    #: ((date, fmp_close), ...) of the FMP daily file the entry was measured on.
    anchors: Tuple[Tuple[date, float], ...]
    evidence: str

    def __post_init__(self):
        if self.kind not in _KINDS:
            raise ValueError(f"{self.symbol} {self.event_date}: unknown override kind {self.kind!r}")
        if not (self.ratio > 0):
            raise ValueError(f"{self.symbol} {self.event_date}: ratio must be > 0")
        if not self.anchors:
            raise ValueError(f"{self.symbol} {self.event_date}: an override needs >= 1 anchor")

    def key(self) -> tuple:
        return (self.symbol.upper(), self.event_date.isoformat(), float(self.ratio), self.kind,
                tuple((d.isoformat(), float(c)) for d, c in self.anchors))


_A2020 = date(2020, 1, 2)
_SCCO_EVIDENCE = (
    "quarterly stock dividend in FMP prices, absent from the FMP split calendar; exact ex-date "
    "and ratio measured 2026-09-23 from ThetaData as-traded close vs FMP close (each step "
    "coincides with an FMP cash-dividend ex-date); data in scratchpad/g4/scco_theta_eod.csv, "
    "scco_fmp_raw.json")

BASIS_OVERRIDES: Tuple[BasisOverride, ...] = (
    BasisOverride(
        "HON", date(2025, 10, 30), 1.0614, KIND_ADD_PRICE_ADJUSTMENT, ((_A2020, 178.71),),
        "Solstice spin-off ex-date; in FMP prices (2026-09-16 refetch), not in the calendar. "
        "Parity/FMP 1.0616 before / 1.0003 after. The calendar row 2026-06-28 1907/2000 IS in "
        "the prices and stays. Residual after fix: median 1.0001, 0 sessions > 2%."),
    BasisOverride(
        "NVS", date(2023, 10, 4), 1.0575, KIND_ADD_PRICE_ADJUSTMENT, ((_A2020, 89.85),),
        "Sandoz spin-off ex-date; FMP prices adjusted, FMP split calendar has no row. "
        "Parity/FMP median 1.0604 (20 sessions before) vs 1.0028 (20 after); 2020-2023 plateau "
        "1.057-1.059. Residual after fix: median 1.0006, 0 sessions > 5%."),
    BasisOverride("SCCO", date(2024, 8, 9), 1.0057, KIND_ADD_PRICE_ADJUSTMENT,
                  ((_A2020, 40.18),), _SCCO_EVIDENCE),
    BasisOverride("SCCO", date(2024, 11, 6), 1.0062, KIND_ADD_PRICE_ADJUSTMENT,
                  ((_A2020, 40.18),), _SCCO_EVIDENCE),
    BasisOverride("SCCO", date(2025, 2, 11), 1.0073, KIND_ADD_PRICE_ADJUSTMENT,
                  ((_A2020, 40.18),), _SCCO_EVIDENCE),
    BasisOverride("SCCO", date(2025, 5, 2), 1.0098, KIND_ADD_PRICE_ADJUSTMENT,
                  ((_A2020, 40.18),), _SCCO_EVIDENCE),
    BasisOverride("SCCO", date(2025, 8, 15), 1.0101, KIND_ADD_PRICE_ADJUSTMENT,
                  ((_A2020, 40.18),), _SCCO_EVIDENCE),
    BasisOverride("SCCO", date(2025, 11, 12), 1.0085, KIND_ADD_PRICE_ADJUSTMENT,
                  ((_A2020, 40.18),), _SCCO_EVIDENCE + ". FMP closes 2025-10-20..2025-11-11 "
                  "already carry this adjustment (16 sessions ~0.85% off after the fix, below "
                  "the 5% guard tolerance); accepted"),
)


def overrides_for(symbol: str, overrides: Optional[Tuple[BasisOverride, ...]] = None
                  ) -> Tuple[BasisOverride, ...]:
    """``symbol``'s entries (event-date order); ``overrides`` defaults to ``BASIS_OVERRIDES``."""
    src = BASIS_OVERRIDES if overrides is None else overrides
    s = str(symbol).upper()
    return tuple(sorted((o for o in src if o.symbol.upper() == s), key=lambda o: o.event_date))


def acknowledged_jumps(entries: Tuple[BasisOverride, ...]) -> dict:
    """``{jump_date: close_ratio}`` of the ``acknowledge_jump`` entries among ``entries``."""
    return {e.event_date: float(e.ratio) for e in entries if e.kind == KIND_ACKNOWLEDGE_JUMP}


def overrides_version_for(entries: Tuple[BasisOverride, ...],
                          version: Optional[str] = None) -> Optional[str]:
    """``"<OVERRIDES_VERSION>:<digest of the entries>"``, or None for no entries (the
    vendor calendar as-is, identity unchanged)."""
    if not entries:
        return None
    v = OVERRIDES_VERSION if version is None else version
    blob = repr(tuple(o.key() for o in entries)).encode("utf-8")
    return f"{v}:{hashlib.sha256(blob).hexdigest()[:12]}"
