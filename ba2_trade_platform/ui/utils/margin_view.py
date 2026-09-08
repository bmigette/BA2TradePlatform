"""Pure formatting for the margin figures shown on the live trades page."""
import math
from typing import Any, Callable, Dict, Iterable, Optional

from ba2_trade_platform.logger import logger


def capital_requirement(value: float, *, effective_factor: float) -> float:
    """Dollars of the account's own balance this position consumes: value / the
    EFFECTIVE margin factor (1.0 with margin off; min(margin_factor, broker multiplier)
    with it on), so the cell agrees with the sizing that produced the position rather
    than with the raw setting. A non-positive or NaN factor is a defect upstream and is
    refused rather than divided by."""
    if not math.isfinite(effective_factor) or effective_factor <= 0:
        raise ValueError(f"effective margin factor must be a positive finite number, got {effective_factor!r}")
    return float(value) / float(effective_factor)


def factors_by_account(account_ids: Iterable[int],
                       *,
                       resolve: Callable[[int], Any]) -> Dict[int, float]:
    """The EFFECTIVE margin factor per account, once per render. ``resolve(acc_id)``
    returns the account instance or None. An account whose factor cannot be read, or
    whose factor is defective (non-positive / NaN), is LEFT OUT and logged at ERROR, so
    its rows show the value alone rather than a capital requirement from a guess."""
    factors: Dict[int, float] = {}
    for acc_id in account_ids:
        acct = resolve(acc_id)
        if acct is None:
            # Plain control flow, not a raise caught one line down: "no instance for
            # this id" is its own diagnosis and deserves to read as one.
            logger.error(f"Effective margin factor unavailable for account {acc_id}: "
                         f"no account instance")
            continue
        try:
            factor = acct.effective_margin_factor()
            # Refuse a defective factor HERE, once, where the account is named --
            # rather than per row, where the failure would be a silent empty cell.
            capital_requirement(1.0, effective_factor=factor)
            factors[acc_id] = factor
        except Exception as e:
            logger.error(f"Effective margin factor unavailable for account {acc_id}: {e}",
                         exc_info=True)
    return factors


def value_capreq_text(value: Optional[float], capreq: Optional[float]) -> str:
    """'$value / $capreq'; '$value / unknown' when the requirement is unknown -- the
    column header promises two figures, so a labelled cell spells the word out (like
    the card's 'BP: unknown') instead of quietly showing one; '' when there is no
    value at all."""
    if value is None:
        return ''
    if capreq is None:
        return f"${value:,.2f} / unknown"
    return f"${value:,.2f} / ${capreq:,.2f}"
