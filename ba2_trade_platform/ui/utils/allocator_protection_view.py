"""Pure view-model helpers for the allocator's per-symbol TP/SL control and exclusions.

No NiceGUI, no database, no broker: plain data in, plain data out, so every decision the page and
the dialog make is unit-testable without a browser (same split as ``portfolio_allocation_view``).
The page (``ui/pages/portfolio_allocation.py``) and the dialog
(``ui/pages/allocator_protection_dialog.py``) do the IO and hand the results here.
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...core.allocator_protection import (
    STATUS_NO_POSITION, STATUS_OFF, STATUS_PARTIAL, STATUS_PROTECTED, STATUS_REPLACING,
    STATUS_UNPROTECTED, ProtectionStatus, TpTarget,
)

#: Quasar colour name -> the hex the row paints with. Inline colours, not ``text-negative``:
#: Quasar's own red is close to unreadable on the page's dark cards (see the symbol card CSS).
STATUS_HEX = {"grey": "#94a3b8", "positive": "#4ade80", "warning": "#fbbf24",
              "negative": "#f87171", "info": "#60a5fa"}

#: The short chip text beside the shield icon. OFF has no chip: the icon alone is the control.
_SHORT_LABELS = {STATUS_PROTECTED: "TP/SL", STATUS_PARTIAL: "TP/SL size mismatch",
                 STATUS_UNPROTECTED: "UNPROTECTED", STATUS_REPLACING: "Re-placing",
                 STATUS_NO_POSITION: "TP/SL armed", STATUS_OFF: ""}


def row_fields(supported: bool, status: Optional[ProtectionStatus],
               note: Optional[str] = None) -> Dict[str, Any]:
    """The flat row fields the symbol table / card template reads for the TP/SL control. Pure.

    ``prot_on`` False (a broker that cannot do it) draws nothing at all; the strings are ``''``
    rather than ``None`` because Quasar prints a bare ``null`` for a missing one. ``note`` is the
    small line under the chip ("TP1 filled 2026-10-03: share 6% -> 3%").
    """
    if not supported or status is None:
        return {"prot_on": False, "prot_code": "", "prot_label": "", "prot_hex": STATUS_HEX["grey"],
                "prot_tip": "", "prot_alarm": False, "prot_note": ""}
    tip = (status.tooltip if status.code != STATUS_OFF
           else "Set TP/SL: place a take-profit and stop-loss for this position.")
    return {"prot_on": True, "prot_code": status.code, "prot_label": _SHORT_LABELS.get(status.code, ""),
            "prot_hex": STATUS_HEX.get(status.color, STATUS_HEX["grey"]), "prot_tip": tip,
            "prot_alarm": bool(status.alarm), "prot_note": note or ""}


def banner_lines(entries: Iterable[Tuple[Any, ProtectionStatus]]) -> List[str]:
    """The RED banner lines from ``[(protection, status)]``: a position that is unprotected, whose
    protective orders do not match its size, or whose protective order was lost or could not be
    verified. Pure."""
    lines: List[str] = []
    for p, status in sorted(entries, key=lambda e: e[0].symbol):
        if status.alarm:
            detail = (p.alert_message or status.tooltip or "").strip()
            lines.append(f"{p.symbol}: {status.label.upper()} -- {detail}")
    return lines


# --------------------------------------------------------------------- exclusion

EXCLUDED_BADGE = "Excluded: manual"


def exclusion_fields(exclusion: Any) -> Dict[str, Any]:
    """The flat row fields of the exclude toggle and its badge. Pure.

    ``exclusion`` is an ``AllocatorExclusion`` (or anything with ``since`` / ``note``) or None.
    An excluded row is greyed and says why; the toggle's tooltip is the action it performs.
    """
    if exclusion is None:
        return {"excluded": False, "excl_badge": "", "excl_tip": "Exclude from allocation",
                "excl_icon": "visibility_off"}
    since = getattr(exclusion, "since", None)
    note = getattr(exclusion, "note", None)
    tip = "Excluded from allocation"
    if since:
        tip += f" since {since:%Y-%m-%d}"
    if note:
        tip += f" ({note})"
    tip += ": no buys, no sells, outside the label maths. Click to include it again."
    return {"excluded": True, "excl_badge": EXCLUDED_BADGE, "excl_tip": tip, "excl_icon": "visibility"}


def effective_weight_text(weight_pct: Optional[float], effective_pct: Optional[float]) -> str:
    """The small 'eff. 25%' note under a share box when the label has an excluded symbol. Pure."""
    if effective_pct is None or weight_pct is None:
        return ""
    if abs(effective_pct - weight_pct) < 0.005:
        return ""
    return f"eff. {effective_pct:.2f}%"


def label_extras_text(excluded_value: float, excluded_count: int, freed_pct: float) -> str:
    """The label header's small extra line: '+$X excluded' and 'freed Y% from TP/SL fills'. Pure."""
    parts: List[str] = []
    if excluded_count > 0:
        parts.append(f"+${excluded_value:,.0f} excluded")
    if freed_pct > 0.005:
        parts.append(f"freed {freed_pct:.2f}% from TP/SL fills")
    return " | ".join(parts)


def weight_note(change: Any) -> str:
    """'TP1 filled 2026-10-03: share 6% -> 3%' from an ``AllocatorWeightChange``. Pure."""
    if change is None:
        return ""
    return (f"{change.detail.split(':')[0] if change.detail else 'TP/SL fill'} "
            f"{change.created_at:%Y-%m-%d}: share {change.before_pct:g}% -> {change.after_pct:g}%")


# --------------------------------------------------------------------- dialog input parsing

def _num(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_target_rows(rows: Sequence[Dict[str, Any]]) -> Tuple[List[TpTarget], List[str]]:
    """Dialog rows ``[{"price": raw, "pct": raw}]`` (percent 0-100) -> ``TpTarget`` list plus
    parse errors. A blank or unparsable cell is an ERROR, never a default. Pure."""
    targets: List[TpTarget] = []
    errors: List[str] = []
    for number, row in enumerate(rows, start=1):
        price = _num(row.get("price"))
        pct = _num(row.get("pct"))
        if price is None:
            errors.append(f"Take-profit {number}: enter a price.")
        if pct is None:
            errors.append(f"Take-profit {number}: enter the share of the position (%).")
        if price is not None and pct is not None:
            targets.append(TpTarget(price=price, fraction=pct / 100.0))
    return targets, errors


def even_percentages(count: int) -> List[float]:
    """``count`` percentages summing to exactly 100.00 (the remainder goes to the last)."""
    if count < 1:
        return []
    base = round(100.0 / count, 2)
    out = [base] * count
    out[-1] = round(100.0 - base * (count - 1), 2)
    return out


def fractions_to_percentages(fractions: Sequence[float]) -> List[float]:
    """Fractions -> percentages for the dialog's boxes, two decimals. When the fractions total
    exactly 1 the last box absorbs the rounding so the boxes read 100.00 (1/3 x 3 = 33.33 +
    33.33 + 33.34), not 99.99 -- which would silently carve a 0.01% runner. Pure."""
    pcts = [round(f * 100.0, 2) for f in fractions]
    if pcts and abs(sum(fractions) - 1.0) < 1e-9:
        pcts[-1] = round(100.0 - sum(pcts[:-1]), 2)
    return pcts
