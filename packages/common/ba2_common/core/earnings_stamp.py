"""The EARNINGS-EVENT STAMP CONTRACT -- one place, read by two sides.

Design 2026-08-31 (leaps-grid) S9, the TIMING SPLIT: the EXPERT owns the ranking, the
STRATEGY owns the timing. ``FMPEarningsEvent`` surfaces every event inside its fixed
look-ahead window and STAMPS what it saw onto the recommendation; ``O_ERN``'s searched
entry gene reads that stamp back as an entry condition, and its searched exit gene reads
the stamped EVENT DATE back off the entry order. One timing knob, owned by one side.

That only works if the writer and the two readers agree on the key path, so the key path
lives here rather than being spelled three times:

* ``ExpertRecommendation.data["FMPEarningsEvent"]`` -- the expert's payload. The nesting
  under the expert NAME is what makes the SAME path work live (``run_analysis`` stamps
  ``.data`` itself) and in backtest (``daily_engine`` copies ``raw_outputs`` wholesale
  into ``.data``). Hardcoding the producing expert's name in ba2_common follows the
  ``FMPRating`` precedent already in ``TradeConditions`` (``price_vs_target_*``): the
  package never imports ba2_experts, it just knows the key.
* ``TradingOrder.data["earnings_event_date"]`` -- the entry order's carry-forward of the
  event date, stamped at submit beside ``max_loss_per_contract`` (design 2026-08-29 S8.2's
  seam). The EXIT needs the date of the event the ENTRY was taken for, and by exit time the
  recommendation in hand is a DIFFERENT, later one; the order row is the only thing that
  travels with the position.

UNKNOWN IS NEVER A VALUE. Every reader here returns ``None`` rather than a plausible
number: an absent ``days_to_earnings`` read as ``0`` would satisfy ``<= X`` for every
symbol of every non-event expert (a straddle on everything), and an absent event date read
as "today" would fire ``days_after_event >= 0`` on sight. Both readers refuse bools,
strings, NaN and infinities through ``option_payoff._numeric`` / ``strptime``, so a
stringly-typed payload cannot be PARSED into firing.
"""
from datetime import date, datetime
from typing import Any, Dict, Optional

#: The ``ExpertRecommendation.data`` key the earnings-event expert nests its payload under.
EARNINGS_STAMP_NAMESPACE = "FMPEarningsEvent"

#: Key inside that payload: whole calendar days from the decision bar to the event.
DAYS_TO_EARNINGS_KEY = "days_to_earnings"

#: Key inside that payload: the announcement date, an ISO 'YYYY-MM-DD' string.
EVENT_DATE_KEY = "event_date"

#: Key on the ENTRY ORDER's ``data`` where the submit path carries the event date forward
#: for the exit side. Deliberately NOT the same spelling as the payload key: the order row
#: is a flat namespace shared with ``option_reserve``/``max_loss_per_contract``, so the
#: name has to say which event it is the date of.
ORDER_EVENT_DATE_KEY = "earnings_event_date"


def parse_stamp_day(value: Any) -> Optional[date]:
    """A ``date`` from an ISO 'YYYY-MM-DD' (or 'YYYY-MM-DDTHH:MM:SS') stamp, else None.

    Mirrors ``FMPEarningsEvent._parse_day`` -- the producing side -- so a value that
    round-trips there round-trips here. ``date``/``datetime`` are accepted as-is because a
    live path may hand the object rather than its serialisation; anything else that does
    not parse is UNKNOWN, never a guessed day.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value).split("T")[0], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def earnings_payload(expert_recommendation: Any) -> Optional[Dict[str, Any]]:
    """The expert's stamped payload off a recommendation, or None when there is none.

    None for: no recommendation, ``data`` absent/None, ``data`` not a dict (a legacy row
    could hold anything the JSON column accepted), or no ``FMPEarningsEvent`` key -- which
    is EVERY recommendation from every other expert. That last case is the common one and
    it is why the readers below are absence-first rather than exception-first.
    """
    data = getattr(expert_recommendation, "data", None)
    if not isinstance(data, dict):
        return None
    payload = data.get(EARNINGS_STAMP_NAMESPACE)
    return payload if isinstance(payload, dict) else None


def stamped_days_to_earnings(expert_recommendation: Any) -> Optional[float]:
    """Days-to-earnings AS THE EXPERT MEASURED IT at the decision bar, or None.

    NEVER a second calendar fetch. The whole point of the timing split is that the number
    the strategy gates on is the number the ranking was computed against -- point-in-time
    consistent with the rank, and free. ``DaysToEarningsCondition`` (which DOES fetch) is
    the unchained answer and stays exactly as it is; see that class's docstring.
    """
    payload = earnings_payload(expert_recommendation)
    if payload is None:
        return None
    # option_payoff._numeric is the module rule for "is this a usable quantity": it rejects
    # bool, str, NaN and infinity, so a stamp of "3" or True cannot be read as a number.
    from ba2_common.core.option_payoff import _numeric
    return _numeric(payload.get(DAYS_TO_EARNINGS_KEY))


def stamped_event_date(expert_recommendation: Any) -> Optional[date]:
    """The announcement date the expert stamped on this recommendation, or None."""
    payload = earnings_payload(expert_recommendation)
    if payload is None:
        return None
    return parse_stamp_day(payload.get(EVENT_DATE_KEY))


def order_event_date(order: Any) -> Optional[date]:
    """The event date carried on an ENTRY ORDER's ``data``, or None.

    The exit side's whole input. Absent for every order that was not opened off an
    earnings-event recommendation -- every equity order, and every option order from any
    other expert -- and absence disarms ``days_after_event`` in BOTH directions.
    """
    data = getattr(order, "data", None)
    if not isinstance(data, dict):
        return None
    return parse_stamp_day(data.get(ORDER_EVENT_DATE_KEY))
