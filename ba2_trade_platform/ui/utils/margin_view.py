"""Pure formatting for the margin figures shown on the live trades page."""
import math
from typing import Optional


def capital_requirement(value: float, *, effective_factor: float) -> float:
    """Dollars of the account's own balance this position consumes: value / the
    EFFECTIVE margin factor (1.0 with margin off; min(margin_factor, broker multiplier)
    with it on), so the cell agrees with the sizing that produced the position rather
    than with the raw setting. A non-positive or NaN factor is a defect upstream and is
    refused rather than divided by."""
    if not math.isfinite(effective_factor) or effective_factor <= 0:
        raise ValueError(f"effective margin factor must be a positive finite number, got {effective_factor!r}")
    return float(value) / float(effective_factor)


def value_capreq_text(value: Optional[float], capreq: Optional[float]) -> str:
    """'$value / $capreq'; the value alone when the requirement is unknown; '' when neither."""
    if value is None:
        return ''
    if capreq is None:
        return f"${value:,.2f}"
    return f"${value:,.2f} / ${capreq:,.2f}"
