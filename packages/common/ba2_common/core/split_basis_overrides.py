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
  * A REPLACEMENT is an ``exclude_calendar_event`` plus an ``add_price_adjustment`` on the
    SAME date: the vendor lists the event with the wrong ratio (FTI's Technip Energies spin as
    x1.344 where prices and chain carry x1.2216), so the measured ratio replaces it.
  * ``acknowledge_jump`` -- a REAL one-day move (an earnings gap, not a basis defect) that
    sits within ``MIXED_BASIS_WINDOW_DAYS`` of a calendar split and so classifies it
    ``mixed_basis``. ``event_date`` is the jump's bar date and ``ratio`` its close / previous
    close. It removes ONLY that jump from the mixed-basis scan: the split stays in the factor
    and keeps whatever verdict the ratio rule gives it. The jump must still be in the file at
    that date with that ratio (+-``ANCHOR_TOLERANCE``), else it acknowledges nothing (and
    ``resolve_symbol_split_basis`` refuses the symbol, naming it). Honoured by every
    ``check_split_basis`` caller (the warmup preflight and the live drift report included), so
    an acknowledged jump also stops the warmup's re-fetch loop.

THE PLATEAU CONVENTION (every ``add_price_adjustment`` ratio from OVERRIDES_VERSION 2026-09-26.1
on). With r(d) = put-call-parity spot of the nearest expiry / (FMP close x the factor of every
OTHER calendar row and entry of the symbol) on each evaluable session d, an add at event date c
gets

    ratio = median(r over the <= 60 evaluable sessions before c, after the previous event)
          / median(r over the <= 60 evaluable sessions from c on, before the next event)

rounded to 4 decimals (6 below 0.1). Adjacent plateaus, the same window on both sides, the same
rule for a lone spin-off, a stretch bracket and a replacement -- so a re-measurement of any entry
reproduces its number: ``python tools/measure_basis_plateaus.py SYM CUT[,CUT...]`` (the guard's
own parity spot, the symbol's adds at the cuts removed). EXCEPTION: a FIXED-RATIO corporate
action the calendar lacks (a reverse split) takes its NOMINAL ratio -- its measured plateau
carries borrow (FCEL: 0.033532 measured, 1/30 nominal). (HON, NVS and SCCO predate the
convention.) An exclusion's own ``ratio``
is the vendor row's, informational only. A STRETCH override (two adds bracketing a few weeks of
another basis, or an event whose effect FMP applies early) carries a second anchor INSIDE the
stretch, so a vendor fix of the stretch alone refuses instead of double counting.

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
    "validate_overrides",
    "acknowledged_jumps",
    "BASIS_OVERRIDES",
    "overrides_for",
    "overrides_version_for",
]

OVERRIDES_VERSION = "2026-09-26.4"

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
    # ---- stage-2 universe (2026-09-25/26). Ratios follow the PLATEAU CONVENTION of the module
    # docstring; "r" = parity spot of the nearest expiry / (FMP close x factor without the entry),
    # "par/close" = the same with no factor. Part-2 anchors are the bytes of the 2026-09-25
    # forced full re-fetch; a stretch override carries a second anchor INSIDE its stretch.
    # Measurements: tests/test_split_basis_overrides_stage2.py. ---------------------------------
    BasisOverride(
        "TRP", date(2024, 10, 2), 1.098, KIND_ADD_PRICE_ADJUSTMENT, ((_A2020, 48.0),),
        "South Bow spin-off, ex 2024-10-02 (10-01 at 1.1013, 10-02 unevaluable, 10-03 at "
        "1.0023); in FMP prices, not in the calendar. r median 1.1007 over the 60 sessions "
        "before (p10-p90 1.0875-1.1028, the low tail being ex-dividend weeks) / 1.0024 over "
        "the 60 after = 1.0980."),
    BasisOverride(
        "MMM", date(2024, 4, 1), 1.1961, KIND_ADD_PRICE_ADJUSTMENT, ((_A2020, 150.49),),
        "Solventum spin-off, ex 2024-04-01 (03-28 at 1.1968, 04-02 at 1.0024); in FMP prices, "
        "not in the calendar. r 1.1971 (p10-p90 1.1961-1.1979) before / 1.0009 after = 1.1961."),
    BasisOverride(
        "SKM", date(2021, 12, 27), 1.6594, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 22.5), (date(2021, 12, 15), 26.9)),
        "SK Telecom ADR: the as-traded ADR left FMP's basis on 2021-11-30 (the calendar row "
        "0.607, which STAYS) and returned to it on 2021-12-27, when the chain re-struck "
        "(strikes 20.74/25.74/... first listed that day). r (0.607 row in the factor): 1.6494 "
        "over the 60 sessions before 12-27 (1.6515 before 11-30, 1.6517 over 11-30..12-23) / "
        "0.9940 after = 1.6594 (tools/measure_basis_plateaus.py)."),
    BasisOverride(
        "MTCH", date(2020, 7, 1), 3.502, KIND_EXCLUDE_CALENDAR_EVENT, ((_A2020, 84.19),),
        "The 2020-07-01 row 1751/500 is IAC's separation ratio; FMP's MTCH closes before it are "
        "the OLD Match Group's own price, never adjusted by it. Parity (MTCH root) / (FMP x "
        "3.502) = 0.2848 over 60 sessions before (p10-p90 0.2828-0.2857: 0.9974 without the "
        "row), 1.0000 after. Not a root change: IAC's chain / FMP is 2.65-3.17, not constant."),
    BasisOverride(
        "AMC", date(2022, 8, 22), 2.0, KIND_EXCLUDE_CALENDAR_EVENT, ((_A2020, 74.6),),
        "The 2022-08-22 x2 row is the APE preferred-unit dividend: OCC left strikes alone and "
        "FMP's closes never took it. par/close 0.0998 over 2020-01..2022-08-19 (628 sessions) "
        "and 0.0986 over 2022-08-23..2023-08-23 = the 2023-08-24 1:10 row alone (kept)."),
    BasisOverride(
        "AMC", date(2023, 8, 14), 0.644487, KIND_ACKNOWLEDGE_JUMP, ((_A2020, 74.6),),
        "Real move: -36% the day the court cleared the APE conversion (10 days before the "
        "1:10 reverse split), in a full re-fetch."),
    BasisOverride(
        "AMC", date(2023, 9, 6), 0.631965, KIND_ACKNOWLEDGE_JUMP, ((_A2020, 74.6),),
        "Real move: -37% on the at-the-market offering announcement (13 days after the 1:10); "
        "par/close 1.0016 over 2023-08-25..2025-12-31, so the file is on one basis."),
    BasisOverride(
        "ARWR", date(2011, 11, 10), 1.401869, KIND_ACKNOWLEDGE_JUMP, ((_A2020, 63.12),),
        "Real +40% move 7 days before the 2011-11-17 1:10 reverse split, in a full re-fetch; "
        "outside 2020-2025, guard clean (0 sessions > 5%)."),
    BasisOverride(
        "ENLT", date(2023, 2, 10), 0.682692, KIND_ACKNOWLEDGE_JUMP, ((date(2023, 2, 10), 17.75),),
        "The US IPO day: FMP's 27 bars before it (2023-01-03..02-09) are thin pre-listing "
        "prints (volume 0-350, flat 26.0 for two weeks), 11 days after the 2023-01-30 1:10 "
        "row. Not a basis change: the chain starts 2023-08-09 and is guard clean."),
    BasisOverride(
        "FCEL", date(2019, 6, 5), 0.666667, KIND_ACKNOWLEDGE_JUMP, ((_A2020, 60.3),),
        "Real -33% move 27 days after the 2019-05-09 1:12 reverse split, in a full re-fetch."),
    BasisOverride(
        "FCEL", date(2024, 11, 11), 1 / 30, KIND_ADD_PRICE_ADJUSTMENT, ((_A2020, 60.3),),
        "1:30 reverse split effective 2024-11-11 (chain strike ladder 0.5-7 on 11-08, 1-12 from "
        "11-12; no ThetaData row on 11-11); in FMP prices, missing from the calendar. A fixed-"
        "ratio action: the NOMINAL 1/30 (the plateau convention measures 0.033532 = r 0.0334 "
        "before / 0.9949 after, the difference being borrow). Residual median 1.0000 over "
        "2020-01..2024-11-08 (878 sessions), 0.9973 after."),
    BasisOverride(
        "GSK", date(2022, 7, 22), 0.8, KIND_EXCLUDE_CALENDAR_EVENT, ((_A2020, 46.92),),
        "The 4:5 share consolidation (with the Haleon demerger, 2022-07-18) is in the calendar "
        "but not in FMP's ADR closes: par/close 1.0000 over 2020-01..2022-07-18 (631 "
        "sessions) and 1.0007 after, i.e. no step; with the row the pre-event ratio was 1.25."),
    BasisOverride(
        "QGEN", date(2025, 1, 29), 0.9722, KIND_EXCLUDE_CALENDAR_EVENT,
        ((_A2020, 34.18), (date(2024, 6, 3), 43.0)),
        "Synthetic share repurchase listed as x35/36 = 0.9722, but FMP's closes step the other "
        "way at it; replaced by the next entry. (The 2024-01-30 x0.97 row IS in the prices: "
        "par/close 0.9957 over 2023-07..2024-01-29 vs 1.0263 over 2024-01-31..07-31, a step of "
        "1.0307 = 1/0.97, so it stays.)"),
    BasisOverride(
        "QGEN", date(2025, 1, 29), 1.019, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 34.18), (date(2024, 6, 3), 43.0)),
        "Replacement of the x0.9722 row by the measured step: r (the 2024-01-30 x0.97 row in "
        "the factor) 1.0253 (p10-p90 1.0152-1.0316) over the 60 sessions before / 1.0062 over "
        "the 60 from it = 1.0190. Residual medians 1.0061 (2020-01..2025-01-28) and 1.0031 "
        "(2025-01-29..2025-12-31)."),
    BasisOverride(
        "QGEN", date(2025, 12, 15), 1.0564, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 34.18), (date(2025, 12, 16), 47.38)),
        "Synthetic share repurchase (capital repayment + 0.95 consolidation, ex 2026-01-08): "
        "FMP's closes 2025-12-15..2026-01-07 sit on another basis -- FMP's close jumps x1.0577 "
        "on 12-15 while the chain does not move (even the 4-day 12-19 expiry, before the event, "
        "reads 0.9492 of it). NOT carry: re-measured 2026-09-26 (an earlier entry called it "
        "the repayment's carry). r 1.0023 over the 60 sessions before 12-15 / 0.9487 over the "
        "12-session stretch = 1.0564; closed by the 2026-01-08 replacement below."),
    BasisOverride(
        "QGEN", date(2026, 1, 8), 0.95, KIND_EXCLUDE_CALENDAR_EVENT,
        ((_A2020, 34.18), (date(2025, 12, 16), 47.38)),
        "The 0.95 consolidation row: FMP's closes carry it only over the 2025-12-15 stretch "
        "(see that entry), not over the whole history; replaced by the next entry."),
    BasisOverride(
        "QGEN", date(2026, 1, 8), 0.9465, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 34.18), (date(2025, 12, 16), 47.38)),
        "End of the stretch at the repurchase's ex-date: r 0.9487 over the stretch / 1.0023 "
        "over the 27 sessions from 2026-01-08 = 0.9465 (x1.0564 before it: net 0.9999)."),
    BasisOverride(
        "LBRDA", date(2025, 6, 16), 0.9374, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 125.19), (date(2025, 6, 24), 89.03)),
        "GCI Liberty spin (calendar row 2025-07-15 x1.067, kept): FMP's closes carry it only "
        "from 2025-06-16 (ex-distribution prices a month early). r (the row in the factor) "
        "0.9403 over the 60 sessions before 06-16 / 1.0031 over the 60 from it = 0.9374."),
    BasisOverride(
        "FTI", date(2021, 2, 16), 1.344, KIND_EXCLUDE_CALENDAR_EVENT, ((_A2020, 17.46),),
        "Technip Energies spin: the calendar says x1.344, prices and chain carry x1.2213; "
        "replaced by the next entry."),
    BasisOverride(
        "FTI", date(2021, 2, 16), 1.2213, KIND_ADD_PRICE_ADJUSTMENT, ((_A2020, 17.46),),
        "Replacement of the x1.344 row: r 1.2221 (p10-p90 1.2184-1.2279) over the 60 sessions "
        "before / 1.0006 over the 60 after = 1.2213."),
    BasisOverride(
        "PHG", date(2022, 5, 13), 1.0764, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 44.87), (date(2022, 9, 7), 17.43)),
        "FMP's closes 2022-05-13..2023-05-10 sit on another basis than the calendar implies "
        "(FMP's close jumps exactly x1.0455 = the 2023-05-11 row on 2022-05-13, the 2022 "
        "stock-dividend ex-date); rows 2020-06-30 x1.02 and 2024-05-09 x1.034 are right and "
        "stay. r 1.0423 over the 60 sessions before / 0.9683 over the 60 from it = 1.0764."),
    BasisOverride(
        "PHG", date(2023, 5, 11), 1.045455, KIND_EXCLUDE_CALENDAR_EVENT,
        ((_A2020, 44.87), (date(2022, 9, 7), 17.43)),
        "The x1.0455 row is not what the prices carry at 2023-05-11; replaced by the next "
        "entry."),
    BasisOverride(
        "PHG", date(2023, 5, 11), 0.966, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 44.87), (date(2022, 9, 7), 17.43)),
        "Replacement: r 0.9687 over the 60 sessions before / 1.0029 over the 60 from it = "
        "0.9660."),
    BasisOverride(
        "FLEX", date(2023, 12, 4), 0.727, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 12.78), (date(2023, 12, 11), 18.77)),
        "Nextracker distribution (ex 2024-01-03): FMP's closes 2023-12-04..2024-01-02 are "
        "ex-distribution prices (-29% on 12-04) while the chain trades regular way. r 1.0022 "
        "over the 60 sessions before / 1.3785 (p10-p90 1.3752-1.3813) over the 20-session "
        "stretch = 0.7270."),
    BasisOverride(
        "FLEX", date(2024, 1, 3), 1.3748, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 12.78), (date(2023, 12, 11), 18.77)),
        "End of the ex-distribution stretch: r 1.3785 over the stretch / 1.0027 over the 60 "
        "sessions from 2024-01-03 = 1.3748."),
    BasisOverride(
        "CMCSA", date(2025, 12, 8), 0.9364, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 45.37), (date(2025, 12, 15), 26.43)),
        "Versant spin-off: FMP's closes 2025-12-08..2026-01-02 are ex-distribution prices "
        "(-8.6% on 12-08) while the chain trades regular way. r 1.0006 over the 60 sessions "
        "before / 1.0685 (p10-p90 1.0678-1.0693) over the 18-session stretch = 0.9364."),
    BasisOverride(
        "CMCSA", date(2026, 1, 5), 1.068, KIND_ADD_PRICE_ADJUSTMENT,
        ((_A2020, 45.37), (date(2025, 12, 15), 26.43)),
        "End of the ex-distribution stretch: r 1.0685 over the stretch / 1.0005 over the 59 "
        "sessions from 2026-01-05 (01-05 unevaluable, 01-06 at 0.9903) = 1.0680."),
    BasisOverride(
        "MARA", date(2017, 11, 22), 1.535211, KIND_ACKNOWLEDGE_JUMP, ((_A2020, 1.05),),
        "Real Nov-2017 crypto spike (+54% / +173% / -41%) 23-29 days after the 2017-10-30 "
        "1:4 reverse split, in a full re-fetch; outside 2020-2025."),
    BasisOverride("MARA", date(2017, 11, 24), 2.729358, KIND_ACKNOWLEDGE_JUMP,
                  ((_A2020, 1.05),), "See the 2017-11-22 entry."),
    BasisOverride("MARA", date(2017, 11, 28), 0.591398, KIND_ACKNOWLEDGE_JUMP,
                  ((_A2020, 1.05),), "See the 2017-11-22 entry."),
    BasisOverride(
        "MARA", date(2019, 4, 10), 1.424354, KIND_ACKNOWLEDGE_JUMP, ((_A2020, 1.05),),
        "Real +42% move 2 days after the 2019-04-08 1:4 reverse split, in a full re-fetch."),
    BasisOverride(
        "SMCI", date(2024, 10, 30), 0.673249, KIND_ACKNOWLEDGE_JUMP, ((_A2020, 2.36),),
        "Real -33% move (auditor resignation) 29 days after the 2024-10-01 10:1 split, in a "
        "full re-fetch; guard clean in 2020-2025."),
)


def validate_overrides(entries: Tuple[BasisOverride, ...]) -> None:
    """Refuse (``ValueError``) two entries of one symbol with the same event date AND kind: the
    factor would silently apply both (or the second acknowledgement would shadow the first). A
    REPLACEMENT -- an exclusion and an add on one date -- is two kinds and stays legal."""
    seen = {}
    for e in entries:
        k = (e.symbol.upper(), e.event_date, e.kind)
        if k in seen:
            raise ValueError(
                f"split-basis overrides: duplicate {e.kind} for {e.symbol} on "
                f"{e.event_date.isoformat()} (x{seen[k]:g} and x{e.ratio:g}); keep one entry")
        seen[k] = float(e.ratio)


validate_overrides(BASIS_OVERRIDES)


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
