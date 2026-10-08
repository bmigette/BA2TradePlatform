"""Share-float post-filter for the LIVE FMP screener.

WHY THIS EXISTS (measured 2026-10-08, see the PR notes): the vendor's screener endpoint
(``/api/v3/stock-screener`` and ``/stable/company-screener``) has NO float parameter. The
``floatSharesUnder`` we used to send was silently ignored (the same 44 rows came back for
``floatSharesUnder=1``), and the response carries no float field, so ``screener_float_min`` /
``screener_float_max`` were no-ops. The float lives in a separate endpoint:

  ``GET /api/v4/shares_float/all`` -- ONE call returns every instrument (~88k rows, ~11.6 MB),
  fields ``symbol, date, freeFloat, floatShares, outstandingShares``. Refreshed daily by the
  vendor from SEC filings (``floatShares`` is the latest filing's figure).

The backtest's metric store builds ``float_shares`` from the SAME ``floatShares`` field (the
per-symbol ``api/v4/historical/shares_float`` series, effective-dated), so the rule below is the
rule the store gate applies (``screen_universe_for_day``): a bound is inclusive on the keeping
side, and a symbol whose float is UNKNOWN passes.

``apply_float_filter`` is PURE (no I/O) so the backtest simulation can import it.
"""
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.config import get_app_setting
from ba2_common.logger import logger

FLOAT_ALL_URL = "https://financialmodelingprep.com/api/v4/shares_float/all"
# The vendor refreshes daily; share the table across every screen/instance in the process.
_FLOAT_TABLE_TTL_S = 6 * 3600.0
#: One download is ~11.6 MB and took ~5 s when measured. Bounded retry: at most 2 attempts of
#: 45 s with a 5 s pause = ~95 s worst case (the generic helper default would be 4 x 120 s +
#: 50 s of backoff = ~8.5 min per screen, with the shared vendor gate backing off every other
#: call in the process meanwhile).
FLOAT_FETCH_TIMEOUT_S = 45
FLOAT_FETCH_DELAYS = (5,)
#: A failed fetch is remembered this long: later screens re-raise the stored error at once
#: instead of each re-downloading against a vendor that is already refusing us.
FLOAT_FAILURE_MEMO_S = 300.0

# Single-flight: ~10 instances start screening within half a second at 09:30; the cold load must
# happen ONCE while the others wait for its result (or its remembered failure).
_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"table": None, "loaded_at": 0.0, "error": None, "failed_at": 0.0}


def _now() -> float:
    return time.monotonic()


def reset_float_table_cache() -> None:
    """Forget the cached table and any remembered failure (tests / an explicit refresh)."""
    with _LOCK:
        _STATE.update(table=None, loaded_at=0.0, error=None, failed_at=0.0)


def apply_float_filter(
    candidates: List[Dict[str, Any]],
    float_table: Dict[str, float],
    float_min: float,
    float_max: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Keep candidates whose float is within [float_min, float_max] (0 = bound disabled).

    * ``float_shares`` is written onto every candidate that has a known float.
    * A candidate whose float is unknown (symbol absent from the table, or ``floatShares``
      missing/0 -- the vendor uses 0 for "no data") PASSES, exactly as the store gate does.
    * float < float_min is dropped; float > float_max is dropped (equal keeps).

    Returns ``(kept, stats)`` with ``dropped_float`` and ``float_unknown``.
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    unknown = 0
    for c in candidates:
        sym = (c.get("symbol") or "").upper()
        fl = float_table.get(sym)
        if fl is None or fl <= 0:
            unknown += 1
            kept.append(c)
            continue
        c["float_shares"] = fl
        if float_min > 0 and fl < float_min:
            dropped += 1
            continue
        if float_max > 0 and fl > float_max:
            dropped += 1
            continue
        kept.append(c)
    return kept, {"dropped_float": dropped, "float_unknown": unknown}


def parse_float_table(rows: Any) -> Dict[str, float]:
    """``/v4/shares_float/all`` payload -> ``{SYMBOL: floatShares}`` (zero/None rows omitted)."""
    if not isinstance(rows, list):
        raise RuntimeError(f"shares_float/all returned {type(rows).__name__}, expected a list")
    table: Dict[str, float] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = (r.get("symbol") or "").upper()
        fl = r.get("floatShares")
        if sym and fl:
            table[sym] = float(fl)
    return table


def load_float_table() -> Dict[str, float]:
    """The vendor's bulk float table, cached in-process for 6 h (ONE vendor call per refresh).

    Single-flight (one thread downloads, the others wait on the lock and read the result) and
    failure-memoising (``FLOAT_FAILURE_MEMO_S``). Raises ``ScreenerDataError`` -- never returns an
    empty table silently: a float bound the operator configured must not quietly turn into a
    no-op, and an empty screen must not be read as "the filters matched nothing".
    """
    from ba2_providers.StockScreener import ScreenerDataError
    from ba2_providers.fmp_common import fmp_http_get

    with _LOCK:
        now = _now()
        if _STATE["table"] is not None and now - _STATE["loaded_at"] < _FLOAT_TABLE_TTL_S:
            return _STATE["table"]
        if _STATE["error"] is not None and now - _STATE["failed_at"] < FLOAT_FAILURE_MEMO_S:
            raise ScreenerDataError(
                f"float table unavailable (failure remembered for another "
                f"{FLOAT_FAILURE_MEMO_S - (now - _STATE['failed_at']):.0f}s): {_STATE['error']}"
            )
        try:
            api_key = get_app_setting("FMP_API_KEY")
            if not api_key:
                raise RuntimeError("FMP_API_KEY is not set")
            resp = fmp_http_get(FLOAT_ALL_URL, params={"apikey": api_key, "page": 0},
                                endpoint="shares-float-all", timeout=FLOAT_FETCH_TIMEOUT_S,
                                delays=FLOAT_FETCH_DELAYS)
            table = parse_float_table(resp.json())
            if not table:
                raise RuntimeError("shares_float/all returned no usable rows")
        except Exception as e:  # noqa: BLE001 - typed and remembered below, never swallowed
            _STATE.update(error=f"{type(e).__name__}: {e}", failed_at=_now())
            logger.error(f"Float table fetch failed: {e}")
            raise ScreenerDataError(f"float table could not be fetched: {type(e).__name__}: {e}") from e
        _STATE.update(table=table, loaded_at=_now(), error=None)
        logger.info(f"Float table loaded: {len(table)} symbols with a known float")
        return table


def filter_by_float(
    candidates: List[Dict[str, Any]], float_min: Optional[float], float_max: Optional[float]
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Live entry point: no-op when both bounds are off, else load the table and filter."""
    fmin = float(float_min or 0)
    fmax = float(float_max or 0)
    if fmin <= 0 and fmax <= 0:
        return candidates, {"dropped_float": 0, "float_unknown": 0}
    return apply_float_filter(candidates, load_float_table(), fmin, fmax)
