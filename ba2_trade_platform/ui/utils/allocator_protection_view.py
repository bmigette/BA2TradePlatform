"""Pure view-model helpers for the allocator's per-symbol TP/SL control and exclusions.

No NiceGUI, no database, no broker: plain data in, plain data out, so every decision the page and
the dialog make is unit-testable without a browser (same split as ``portfolio_allocation_view``).
The page (``ui/pages/portfolio_allocation.py``) and the dialog
(``ui/pages/allocator_protection_dialog.py``) do the IO and hand the results here.
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...core.allocator_protection import (
    STATUS_NO_POSITION, STATUS_OFF, STATUS_PARTIAL, STATUS_PROTECTED, STATUS_REPLACING,
    STATUS_UNPROTECTED, WARNING_CODES, ProtectionStatus, TpTarget,
)

#: Quasar colour name -> the hex the row paints with. Inline colours, not ``text-negative``:
#: Quasar's own red is close to unreadable on the page's dark cards (see the symbol card CSS).
STATUS_HEX = {"grey": "#94a3b8", "positive": "#4ade80", "warning": "#fbbf24",
              "negative": "#f87171", "info": "#60a5fa"}

#: The shield's colour NAME per status (icon only; the text lives in the tooltip). grey = no TP/SL set,
#: green = every whole share covered, amber = partly covered / size mismatch / re-placing, red = unprotected.
ICON_COLORS = {"grey": "#94a3b8", "green": "#4ade80", "amber": "#fbbf24", "red": "#f87171"}
_STATUS_ICON = {STATUS_OFF: "grey", STATUS_NO_POSITION: "grey", STATUS_PROTECTED: "green",
                STATUS_PARTIAL: "amber", STATUS_REPLACING: "amber", STATUS_UNPROTECTED: "red"}
#: The exclusion eye: orange when excluded, muted grey when included.
EXCLUDED_HEX = "#fb923c"
INCLUDED_HEX = "#94a3b8"


def protection_icon_color(code: str, failure_alert: bool = False) -> str:
    """The shield's colour name for a status code; a FAILURE alert makes any status red. Total: an unknown
    code is grey-with-a-tooltip rather than a crash. Pure."""
    if failure_alert:
        return "red"
    return _STATUS_ICON.get(code, "grey")


def protection_tooltip(status: ProtectionStatus, note: Optional[str] = None,
                       alert_message: Optional[str] = None) -> str:
    """The shield's whole tooltip: status, the shares-protected count, the status text, the latest fill
    note and the alert message (each once), then the click hint. Newline-separated. Pure."""
    if status.code == STATUS_OFF:
        lines = ["TP/SL: off", "Set TP/SL: place a take-profit and stop-loss for this position."]
        if status.tooltip and status.tooltip not in lines[1]:
            lines.append(status.tooltip)
    else:
        lines = [f"TP/SL: {status.label}"]
        if status.whole_shares and status.whole_shares > 0:
            lines.append(f"{status.covered_quantity:g} of {status.whole_shares} shares protected")
        lines.append(status.tooltip)
    for extra in (note, alert_message):
        if extra and extra not in " ".join(lines):
            lines.append(extra)
    lines.append("Click to set, change or switch off")
    return "\n".join(line for line in lines if line)


def row_fields(supported: bool, status: Optional[ProtectionStatus], note: Optional[str] = None,
               alert_code: Optional[str] = None, alert_message: Optional[str] = None) -> Dict[str, Any]:
    """The flat row fields the symbol table / card template reads for the TP/SL shield. Pure.

    ``prot_on`` False (a broker that cannot do it) draws nothing at all. There is NO text beside the icon:
    the colour (``prot_hex``) and the tooltip (``prot_tip``) carry everything. The strings are ``''``
    rather than ``None`` because Quasar prints a bare ``null`` for a missing one.
    """
    if not supported or status is None:
        return {"prot_on": False, "prot_code": "", "prot_color": "grey", "prot_hex": ICON_COLORS["grey"],
                "prot_tip": "", "prot_alarm": False}
    failure = bool(alert_code) and alert_code not in WARNING_CODES
    color = protection_icon_color(status.code, failure)
    return {"prot_on": True, "prot_code": status.code, "prot_color": color,
            "prot_hex": ICON_COLORS[color], "prot_tip": protection_tooltip(status, note, alert_message),
            "prot_alarm": bool(status.alarm) or failure}


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

def exclusion_fields(exclusion: Any) -> Dict[str, Any]:
    """The flat row fields of the exclude eye. Pure.

    ``exclusion`` is an ``AllocatorExclusion`` (or anything with ``since`` / ``note``) or None. NO text
    beside the icon: the eye is orange when excluded and muted grey when included, the tooltip says what
    clicking does, and the row itself is greyed by the table.
    """
    if exclusion is None:
        return {"excluded": False, "excl_tip": "Click to exclude from allocation",
                "excl_icon": "visibility", "excl_hex": INCLUDED_HEX}
    since = getattr(exclusion, "since", None)
    note = getattr(exclusion, "note", None)
    tip = "Excluded from allocation (manual)"
    if since:
        tip += f" since {since:%Y-%m-%d}"
    if note:
        tip += f" ({note})"
    tip += ". Click to include again."
    return {"excluded": True, "excl_tip": tip, "excl_icon": "visibility", "excl_hex": EXCLUDED_HEX}


def effective_weight_text(weight_pct: Optional[float], effective_pct: Optional[float]) -> str:
    """The small 'eff. 25%' note under a share box when the label has an excluded symbol. Pure."""
    if effective_pct is None or weight_pct is None:
        return ""
    if abs(effective_pct - weight_pct) < 0.005:
        return ""
    return f"eff. {effective_pct:.2f}%"


def label_extras_text(excluded_value: float, excluded_count: int, freed_pct: float) -> str:
    """The label header's small extra line: 'freed Y% from TP/SL fills' only. The excluded symbols are the
    orange segment of the count badge now (``label_badge_segments``). Pure."""
    if freed_pct > 0.005:
        return f"freed {freed_pct:.2f}% from TP/SL fills"
    return ""


#: The segment colours of the label header's count badge (Quasar colour names).
SEGMENT_COLORS = {"total": "grey-7", "profit": "green-8", "loss": "red-8", "excluded": "orange-8"}


def label_badge_segments(rows: Iterable[Any], excluded_value: float = 0.0) -> List[Dict[str, Any]]:
    """The segments of the label header's count badge, in order: total (grey, always), profitable (green),
    losing (red), excluded (orange). Segments with a zero count are omitted except the total. Pure.

    Every symbol of the label is in the total. An EXCLUDED symbol counts only in the orange segment; the
    others are profitable when their floating P&L plus dividends is above zero and losing when it is below
    (a flat, unpriced or unmeasurable P&L is in neither, never guessed). ``rows`` need ``excluded`` and
    ``pnl`` (``total_amount`` when there is dividend cash, else ``amount``).
    """
    total = profit = loss = excluded = 0
    for row in rows:
        total += 1
        if getattr(row, "excluded", False):
            excluded += 1
            continue
        pnl = getattr(row, "pnl", None)
        value = None
        if pnl is not None:
            value = pnl.total_amount if getattr(pnl, "total_amount", None) is not None else pnl.amount
        if value is None:
            continue
        if value > 0:
            profit += 1
        elif value < 0:
            loss += 1
    meaning = {"total": "symbols in this label", "profit": "profitable (floating P&L + dividends above 0)",
               "loss": "losing (floating P&L + dividends below 0)",
               "excluded": "excluded" + (f": ${excluded_value:,.0f}" if excluded_value else " from allocation")}
    out = []
    for key, count in (("total", total), ("profit", profit), ("loss", loss), ("excluded", excluded)):
        if count > 0 or key == "total":
            out.append({"key": key, "count": count, "color": SEGMENT_COLORS[key],
                        "tooltip": f"{count} {meaning[key]}"})
    return out


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
