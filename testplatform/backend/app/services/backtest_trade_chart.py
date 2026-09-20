"""Read-only chart context for ONE saved backtest row (spec 2026-09-20, step 2).

Serves the option trade popup: the complete transaction the selected row belongs
to, each leg's recorded terms, and the underlying's cached daily bars around the
holding period.

READ-ONLY BY CONSTRUCTION, and that is the point of the module:

* **No provider is constructed and no network call is made.** Bars come from the
  on-disk OHLCV cache through ``ba2_common.core.native_cache`` -- the same layout
  ``MemoizedOHLCVProvider(cached_only=True)`` reads -- so opening a popup cannot
  fetch, cannot warm a cache and cannot start a run. A cold cache is a SUCCESSFUL
  response with a notice; the leg table and the payoff never needed bars.
* **The saved array is never written to**, and the response carries a digest of it
  so a client can discard a response for a revision it is no longer showing.
* **Missing stays missing.** A term that was not recorded is ``None`` -- not 0, not
  a defaulted multiplier -- and ``multiplierRecorded`` distinguishes a recorded 1
  from a presentation default. The published trade rows default the multiplier to 1
  for display (``_transform_trades_for_frontend``); this module reads the RAW array
  precisely so that default cannot be mistaken for evidence.

The row id is the saved-array index plus one, matching the frontend's ``id``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core import native_cache

logger = logging.getLogger(__name__)

#: Days of chart context on each side of the holding period. Opening the chart
#: never fetches padding -- a window that reaches past the cache is simply partial.
CONTEXT_DAYS = 20

INTERVAL = "1d"

#: engine_type -> OHLCV cache directory. Only engines whose dataset is KNOWN are
#: listed. An unlisted engine reports ``unavailable`` rather than guessing: reading
#: another provider's directory would silently chart a different instrument's
#: history, which is worse than an empty chart with a reason.
PROVIDER_BY_ENGINE = {"daily_expert": "FMPOHLCVProvider"}

#: Fields the recorder may have published on a leg. Absent => None, never a default.
_MONEY_FIELDS = (
    "entry_price", "exit_price", "pnl", "pnl_pct", "size", "strike", "multiplier",
)


class TradeChartRowNotFound(LookupError):
    """The requested ``trade_id`` is not a row of this backtest's saved array."""


def saved_trades(backtest: Any) -> List[Dict[str, Any]]:
    """The raw saved trade array, defensively copied. Never a transformed view."""
    trades = getattr(backtest, "trades", None)
    if not isinstance(trades, list):
        return []
    return [row for row in trades if isinstance(row, dict)]


def result_digest(backtest: Any) -> str:
    """Stable digest of the saved trade array.

    The saved-row id is an array index, so it is only meaningful for the revision
    it was read from. The client echoes this back / discards on mismatch.
    """
    payload = json.dumps(
        {"id": getattr(backtest, "id", None), "trades": saved_trades(backtest)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def row_for_trade_id(backtest: Any, trade_id: int) -> Dict[str, Any]:
    """The saved row for a 1-based ``trade_id``. Raises ``TradeChartRowNotFound``."""
    trades = saved_trades(backtest)
    if not isinstance(trade_id, int) or trade_id < 1 or trade_id > len(trades):
        raise TradeChartRowNotFound(
            f"trade {trade_id} is not a row of backtest {getattr(backtest, 'id', None)}"
        )
    return trades[trade_id - 1]


def _transaction_rows(backtest: Any, row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every saved row of the selected row's transaction, in saved order.

    Membership is resolved by ``transaction_id`` against the whole saved array --
    never by symbol, sorting, paging or what the table happens to be showing. A row
    with no transaction id stands alone rather than being guessed into a structure.
    """
    transaction_id = row.get("transaction_id")
    if transaction_id is None:
        return [row]
    return [candidate for candidate in saved_trades(backtest)
            if candidate.get("transaction_id") == transaction_id]


def _number(value: Any) -> Optional[float]:
    """A recorded number, or None. A bool is not a price."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_event(value: Any) -> Optional[datetime]:
    """Parse a saved timestamp to an aware UTC datetime, or None.

    A date-only value (or one whose clock reads 00:00:00) is treated as a SESSION
    DATE rather than an instant -- the difference decides whether the day's close
    may be quoted as the entry reference at all.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_time_of_day(event: Optional[datetime]) -> bool:
    return bool(event and (event.hour or event.minute or event.second))


def _session_date(event: Optional[datetime]) -> Optional[date]:
    return event.date() if event else None


def _position_status(row: Dict[str, Any]) -> str:
    reason = (_text(row.get("exit_reason")) or "").lower()
    if reason == "open_at_end":
        return "open_at_end"
    if _parse_event(row.get("exit_time")) is None:
        return "open_at_end"
    return "closed"


def _leg(row: Dict[str, Any], row_id: int) -> Dict[str, Any]:
    """One saved leg, normalised. Absent terms stay absent."""
    unavailable = [field for field in _MONEY_FIELDS if _number(row.get(field)) is None]
    for field in ("strike", "expiry", "option_type", "contract_symbol"):
        if _text(row.get(field)) is None:
            unavailable.append(field)

    direction = _text(row.get("direction"))
    if direction == "buy":
        direction = "long"
    elif direction == "sell":
        direction = "short"

    return {
        "id": row_id,
        "symbol": _text(row.get("symbol")),
        "underlyingSymbol": _text(row.get("underlying_symbol")),
        "contractSymbol": _text(row.get("contract_symbol")),
        "optionType": _text(row.get("option_type")),
        "strike": _number(row.get("strike")),
        "expiry": _text(row.get("expiry")),
        "multiplier": _number(row.get("multiplier")),
        # EVIDENCE, not shape: the raw row either carries a multiplier or it does not.
        # The frontend transform defaults this to 1 for display, which is exactly the
        # substitution this flag exists to keep out of a payoff calculation.
        "multiplierRecorded": "multiplier" in row and _number(row.get("multiplier")) is not None,
        "direction": direction,
        "size": _number(row.get("size")),
        "entryAt": _text(row.get("entry_time")),
        "exitAt": _text(row.get("exit_time")),
        "entryPrice": _number(row.get("entry_price")),
        "exitPrice": _number(row.get("exit_price")),
        "pnl": _number(row.get("pnl")),
        "pnlPercent": _number(row.get("pnl_pct")),
        "exitReason": _text(row.get("exit_reason")),
        "transactionId": (
            None if row.get("transaction_id") is None else str(row.get("transaction_id"))
        ),
        "rowBasis": "aggregate_round_trip",
        "positionStatus": _position_status(row),
        "unavailableFields": sorted(set(unavailable)),
    }


def _underlying_symbol(rows: List[Dict[str, Any]]) -> Optional[str]:
    """The UNDERLYING ticker, or None when it cannot be established.

    An option row without ``underlying_symbol`` has no verified underlying: the OCC
    contract string is not the underlying and is never used as a fallback (that
    would chart a contract as if it were an equity). Equity rows carry their own
    symbol as the underlying.
    """
    option_rows = [row for row in rows if _text(row.get("option_type"))]
    if option_rows:
        declared = {_text(row.get("underlying_symbol")) for row in option_rows}
        declared.discard(None)
        if len(declared) == 1:
            return declared.pop()
        return None

    symbols = {_text(row.get("symbol")) for row in rows}
    symbols.discard(None)
    return symbols.pop() if len(symbols) == 1 else None


def _bars_from_cache(
    provider: Optional[str], symbol: Optional[str], start: date, end: date
) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
    """Bars for ``[start, end]`` from the on-disk cache ONLY.

    Returns ``(bars, cache_status, notice_code)``. No provider is constructed and no
    fetch is attempted: ``native_cache`` resolves an existing parquet file and
    returns None when there is none, which is a reportable state, not an error.
    """
    if provider is None or symbol is None:
        return [], "unavailable", "source_unresolved"

    as_of = datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc)
    frame = native_cache.read_timeseries(provider, symbol, INTERVAL, as_of=as_of)
    if frame is None:
        return [], "missing", "cache_missing"
    if getattr(frame, "empty", True):
        return [], "partial", "cache_empty"

    bars: List[Dict[str, Any]] = []
    for _, record in frame.iterrows():
        session = _parse_event(record.get("effective_date", record.get("Date")))
        if session is None:
            continue
        if session.date() < start or session.date() > end:
            continue
        bars.append({
            "date": session.date().isoformat(),
            "open": _number(record.get("Open")),
            "high": _number(record.get("High")),
            "low": _number(record.get("Low")),
            "close": _number(record.get("Close")),
        })

    if not bars:
        return [], "partial", "cache_out_of_window"
    return bars, "complete", None


def _reference(
    bars: List[Dict[str, Any]], event: Optional[datetime], provider: Optional[str]
) -> Dict[str, Any]:
    """The underlying at one event, from cached bars, WITHOUT looking forward.

    The distinction the popup must not blur:

    * A timestamped event (09:30) does NOT get its own session's close -- a daily bar
      is known only at its session close, so the newest bar STRICTLY BEFORE that
      session is the last price actually known at the event. That is
      ``last_known_bar``, and it is an estimate with an age, not a fill-time quote.
    * A date-only event cannot be placed inside a session at all, so the session's
      own bar is used and labelled ``daily_reference`` -- never an exact fill price.

    Session dates are compared on the bar's own effective date with no timezone
    reinterpretation, and an event we cannot parse is ``unavailable`` rather than
    silently priced at the nearest bar.
    """
    blank = {
        "price": None, "eventAt": None, "observedAt": None, "availableAt": None,
        "quality": "unavailable", "source": provider, "reason": None,
    }
    if event is None:
        return {**blank, "reason": "event timestamp not recorded"}
    if not bars:
        return {**blank, "reason": "no cached bars to reference"}

    blank["eventAt"] = event.isoformat()

    if _has_time_of_day(event):
        session = _session_date(event)
        eligible = [bar for bar in bars if bar["date"] < session.isoformat()]
        if not eligible:
            return {**blank, "reason": "no cached bar before the event"}
        chosen = eligible[-1]
        observed = datetime.fromisoformat(chosen["date"]).replace(tzinfo=timezone.utc)
        return {
            "price": chosen["close"],
            "eventAt": event.isoformat(),
            "observedAt": observed.isoformat(),
            # A daily bar becomes knowable at its session close; that is the honest
            # availability time, not the bar's midnight stamp.
            "availableAt": (observed + timedelta(hours=21)).isoformat(),
            "quality": "last_known_bar",
            "source": provider,
            "reason": f"last completed session before the event ({chosen['date']})",
        }

    session = _session_date(event)
    eligible = [bar for bar in bars if bar["date"] <= session.isoformat()]
    if not eligible:
        return {**blank, "reason": "no cached bar at or before the event"}
    chosen = eligible[-1]
    return {
        "price": chosen["close"],
        "eventAt": event.isoformat(),
        "observedAt": datetime.fromisoformat(chosen["date"]).replace(
            tzinfo=timezone.utc
        ).isoformat(),
        "availableAt": None,
        "quality": "daily_reference",
        "source": provider,
        "reason": "date-level event: the session's own bar, not a fill-time price",
    }


def build_trade_chart_context(backtest: Any, trade_id: int) -> Dict[str, Any]:
    """Assemble the chart context for one saved row of one backtest."""
    row = row_for_trade_id(backtest, trade_id)
    rows = _transaction_rows(backtest, row)
    saved = saved_trades(backtest)
    id_of = {id(candidate): index + 1 for index, candidate in enumerate(saved)}

    legs = [_leg(candidate, id_of.get(id(candidate), 0)) for candidate in rows]

    engine_type = _text(getattr(backtest, "engine_type", None)) or ""
    provider = PROVIDER_BY_ENGINE.get(engine_type)
    underlying_symbol = _underlying_symbol(rows)

    notices: List[Dict[str, str]] = []
    if provider is None:
        notices.append({
            "code": "source_unresolved",
            "message": (
                f"No cached dataset is registered for engine '{engine_type or 'unknown'}', "
                "so no underlying history is shown."
            ),
        })
    if underlying_symbol is None:
        notices.append({
            "code": "underlying_unresolved",
            "message": (
                "The underlying ticker is not recorded on these rows, so no history is "
                "shown. An OCC contract string is not used as a substitute."
            ),
        })
    if len({leg["optionType"] for leg in legs if leg["optionType"]}) == 0:
        notices.append({
            "code": "no_option_terms",
            "message": "These rows carry no option terms; this is the equity view.",
        })

    events = [
        _parse_event(candidate.get("entry_time")) for candidate in rows
    ] + [_parse_event(candidate.get("exit_time")) for candidate in rows]
    known_events = [event for event in events if event is not None]
    start = (min(known_events).date() - timedelta(days=CONTEXT_DAYS)) if known_events else None
    end = (max(known_events).date() + timedelta(days=CONTEXT_DAYS)) if known_events else None

    bars: List[Dict[str, Any]] = []
    cache_status = "unavailable"
    if start is not None and end is not None:
        bars, cache_status, notice_code = _bars_from_cache(
            provider, underlying_symbol, start, end
        )
        if notice_code == "cache_missing":
            notices.append({
                "code": "cache_missing",
                "message": (
                    f"No cached daily bars for {underlying_symbol} "
                    f"({provider}/{INTERVAL}). The payoff and the leg table do not need "
                    "them. Warm the cache for this symbol and range to see the chart."
                ),
            })
        elif notice_code == "cache_out_of_window":
            notices.append({
                "code": "cache_out_of_window",
                "message": (
                    f"Cached bars for {underlying_symbol} do not cover "
                    f"{start.isoformat()}..{end.isoformat()}."
                ),
            })
        elif notice_code == "cache_empty":
            notices.append({
                "code": "cache_empty",
                "message": f"The cached series for {underlying_symbol} holds no bars.",
            })

    for leg, candidate in zip(legs, rows):
        leg["entryUnderlying"] = _reference(bars, _parse_event(candidate.get("entry_time")), provider)
        leg["exitUnderlying"] = _reference(bars, _parse_event(candidate.get("exit_time")), provider)

    if bars:
        notices.append({
            "code": "cache_not_run_snapshot",
            "message": (
                "Historical cache -- exact run snapshot unverified. Cached bars may have "
                "been corrected since the run."
            ),
        })

    return {
        "schemaVersion": 1,
        "backtestId": getattr(backtest, "id", 0),
        "resultDigest": result_digest(backtest),
        "selectedTradeId": trade_id,
        "transactionId": (
            None if row.get("transaction_id") is None else str(row.get("transaction_id"))
        ),
        "legs": legs,
        "underlying": {
            "symbol": underlying_symbol,
            "provider": provider,
            "interval": INTERVAL,
            "cacheStatus": cache_status,
            "provenance": "current_historical_cache" if bars else "unknown",
            "bars": bars,
        },
        "notices": notices,
    }
