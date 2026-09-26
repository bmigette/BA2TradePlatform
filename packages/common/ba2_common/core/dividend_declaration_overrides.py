"""Operator-maintained declaration dates for dividend-calendar rows FMP leaves undated.

WHY. The option basis guard's carry separation (``option_basis_guard`` /
``ba2_common.core.option_carry``) reads the FMP dividend calendar AS OF each session: a row
declared after the session is not added back, and a row with NO declaration date refuses the
carry path by name (it cannot be known whether the market priced it). FMP leaves some rows
undated. QIAGEN's 2026-01-08 capital repayment (the synthetic share repurchase) is one: its
2025-12 expiries price the $2.29 repayment (parity ~0.944 of the spot), so every run reaching
2025-12-31 was refused.

WHAT AN ENTRY DOES. It supplies the declaration date of ONE row -- (symbol, ex-date) -- and only
where FMP's row has none. It never replaces a date FMP provides: a row whose FMP date differs
from the entry REFUSES (``DeclarationOverrideConflict``), as does an entry whose row is absent
or whose amount no longer matches (the entry was recorded against other data: re-check it).

VERSIONED like ``split_basis_overrides``: bump ``DECLARATION_OVERRIDES_VERSION`` on any edit.
Every entry names its evidence (source URLs).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Sequence, Tuple

__all__ = ["DECLARATION_OVERRIDES_VERSION", "DeclarationOverride", "DECLARATION_OVERRIDES",
           "DeclarationOverrideConflict", "AMOUNT_TOLERANCE", "apply_declaration_overrides"]

DECLARATION_OVERRIDES_VERSION = "2026-09-26.1"

#: The row's FMP ``adjDividend`` may differ from the recorded one by at most this fraction.
AMOUNT_TOLERANCE = 0.001


class DeclarationOverrideConflict(ValueError):
    """An entry contradicts the cached FMP calendar: re-check it, never guess."""


@dataclass(frozen=True)
class DeclarationOverride:
    symbol: str
    ex_date: date
    declared: date
    #: FMP ``adjDividend`` of the row the entry was recorded against (the anchor).
    amount: float
    evidence: str

    def __post_init__(self):
        if not (self.declared <= self.ex_date):
            raise ValueError(f"{self.symbol} {self.ex_date}: declared after the ex-date")
        if not (self.amount > 0):
            raise ValueError(f"{self.symbol} {self.ex_date}: amount must be > 0")


DECLARATION_OVERRIDES: Tuple[DeclarationOverride, ...] = (
    # QIAGEN synthetic share repurchase: a capital repayment of up to ~$500M plus a reverse
    # split, enabled by the June 2025 AGM; the per-share amount ($2.29, FMP row ex 2026-01-08
    # with an empty declarationDate) was fixed in the 2025-12-18 announcement, completion
    # 2026-01-07.
    #   SEC 6-K exhibit 99.1:
    #   https://www.sec.gov/Archives/edgar/data/1015820/000101582025000077/exhibit991announcementssbb.htm
    #   Press release:
    #   https://www.nasdaq.com/press-release/qiagen-announces-details-completion-synthetic-share-repurchase-approximately-500
    DeclarationOverride(
        "QGEN", date(2026, 1, 8), date(2025, 12, 18), 2.29,
        "QIAGEN announced the details of its synthetic share repurchase (capital repayment "
        "~$500M + reverse split, completion 2026-01-07) on 2025-12-18 -- SEC 6-K "
        "https://www.sec.gov/Archives/edgar/data/1015820/000101582025000077/"
        "exhibit991announcementssbb.htm; press release https://www.nasdaq.com/press-release/"
        "qiagen-announces-details-completion-synthetic-share-repurchase-approximately-500. The "
        "AGM (June 2025) only enabled it; the amount was fixed on 2025-12-18."),
)


def _validate(entries: Sequence[DeclarationOverride]) -> None:
    seen = set()
    for e in entries:
        k = (e.symbol.upper(), e.ex_date)
        if k in seen:
            raise ValueError(f"duplicate declaration override {e.symbol} {e.ex_date}")
        seen.add(k)


_validate(DECLARATION_OVERRIDES)


def apply_declaration_overrides(
        symbol: str, rows: List[Tuple[date, Optional[float], Optional[date]]],
        overrides: Optional[Sequence[DeclarationOverride]] = None,
) -> List[Tuple[date, Optional[float], Optional[date]]]:
    """``rows`` [(ex-date, adjDividend or None, declarationDate or None)] of ``symbol``'s FMP
    calendar, with the entries' declaration dates filled in where FMP has none. REFUSES
    (``DeclarationOverrideConflict``) when an entry's row is absent, its amount no longer
    matches, or FMP states a different declaration date. ``overrides`` defaults to
    ``DECLARATION_OVERRIDES`` (tests pass their own)."""
    src = DECLARATION_OVERRIDES if overrides is None else tuple(overrides)
    _validate(src)
    mine = {e.ex_date: e for e in src if e.symbol.upper() == str(symbol).upper()}
    if not mine:
        return list(rows)
    out, used = [], set()
    for ex, amount, declared in rows:
        e = mine.get(ex)
        if e is not None:
            used.add(ex)
            tag = (f"{symbol}: declaration override ex {ex.isoformat()} (declared "
                   f"{e.declared.isoformat()}, overrides {DECLARATION_OVERRIDES_VERSION})")
            if amount is None or abs(amount / e.amount - 1.0) > AMOUNT_TOLERANCE:
                raise DeclarationOverrideConflict(
                    f"{tag}: FMP's amount is {amount!r}, the entry was recorded against "
                    f"{e.amount:g}; re-check it")
            if declared is not None and declared != e.declared:
                raise DeclarationOverrideConflict(
                    f"{tag}: FMP now states {declared.isoformat()}; an override never replaces "
                    f"a vendor date -- re-check and remove or correct the entry")
            declared = e.declared
        out.append((ex, amount, declared))
    missing = sorted(set(mine) - used)
    if missing:
        raise DeclarationOverrideConflict(
            f"{symbol}: declaration override(s) for ex {[d.isoformat() for d in missing]} name "
            f"no row of the cached calendar (overrides {DECLARATION_OVERRIDES_VERSION}); "
            f"re-check them")
    return out
