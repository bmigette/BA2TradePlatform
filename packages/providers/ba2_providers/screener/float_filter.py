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
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.config import get_app_setting
from ba2_common.logger import logger

FLOAT_ALL_URL = "https://financialmodelingprep.com/api/v4/shares_float/all"
_FLOAT_TABLE_KEY = "screener:float_table:v4_all"
# The vendor refreshes daily; share the table across every screen/instance in the process.
_FLOAT_TABLE_TTL_S = 6 * 3600.0


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

    Raises (never returns an empty table silently): a float bound the operator configured must
    not quietly turn into a no-op because the table could not be fetched.
    """
    from ba2_providers.fmp_common import fmp_http_get, fmp_live_cached

    api_key = get_app_setting("FMP_API_KEY")
    if not api_key:
        raise RuntimeError("float filter configured but FMP_API_KEY is not set")

    def _fetch() -> Dict[str, float]:
        resp = fmp_http_get(FLOAT_ALL_URL, params={"apikey": api_key, "page": 0},
                            endpoint="shares-float-all", timeout=120)
        table = parse_float_table(resp.json())
        if not table:
            raise RuntimeError("shares_float/all returned no usable rows")
        logger.info(f"Float table loaded: {len(table)} symbols with a known float")
        return table

    return fmp_live_cached(_FLOAT_TABLE_KEY, _fetch, ttl_seconds=_FLOAT_TABLE_TTL_S)


def filter_by_float(
    candidates: List[Dict[str, Any]], float_min: Optional[float], float_max: Optional[float]
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Live entry point: no-op when both bounds are off, else load the table and filter."""
    fmin = float(float_min or 0)
    fmax = float(float_max or 0)
    if fmin <= 0 and fmax <= 0:
        return candidates, {"dropped_float": 0, "float_unknown": 0}
    return apply_float_filter(candidates, load_float_table(), fmin, fmax)
