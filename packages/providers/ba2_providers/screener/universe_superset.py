"""The STATIC universe of a screener-optimization job: the true superset of what any genome can select.

THE BUG THIS REPLACES (2026-10-07, ``universe_mismatch_2026-10-07``). A screener job freezes a static
symbol list (``backtest.enabled_instruments``) at launch. It was built with the LOOSEST value of every
screener gene INCLUDING ``max_stocks`` at its ceiling (50) and the market-cap sort, so each weekly scan
contributed only its 50 LARGEST names that pass the loosest filters. The comment "tighter individuals
select a subset" is false once a top-N cut is applied after the filters: a genome with tighter filters
(rvol >= 1.5, drop >= 13 ...) selects names whose cap rank is 60-300 in the band, which the loosest
top-50 never contained. Those selections were then silently dropped by the intersection with
``enabled_instruments`` (``strategy_optimization_handler._build_daily_trial_config`` and the engine's
``_bar_universes``): the genome's own picks were untradable and nothing said so.

THE RULE (``RULE_ID``). The static universe is every symbol that passes the loosest values of the FILTER
genes on at least one scan visible during the run window, with NO ``max_stocks`` cut and no sort. The
per-decision gate (``metric_store.screen_universe_for_day``) then applies the genome's own filters, sort
and cut. Genes that only affect ORDERING or the CUT SIZE (``max_stocks``, ``sort_metric``) cannot change
the superset, by construction (they are stripped).

"Loosest" is derived from the job's DECLARED gene ranges, per gene role (a table below). A screener gene
this module has no role for, or one whose range is missing or malformed, raises ``ScreenerUniverseError``:
the launch refuses rather than guesses.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from ba2_providers.screener import metric_store as _ms

#: Stamped on ``backtest.screener_universe_rule`` of a job built under this rule. Part of the job's
#: identity (checkpoint fingerprint, driver job names): see ``strategy_optimization_handler``.
RULE_ID = "superset-v1"

#: The screener gene ranges, canonical here so the launcher AND the drivers (which cannot import the
#: launcher) read ONE definition. The launcher keeps its private names as aliases of these objects.
SCREENER_OPT = {
    "screener_market_cap_min": {"min": 2e9, "max": 1e10, "step": 1e9, "type": "float", "optimize": True},
    # Floor lowered 1.0->0.0 (2026-07-19): see the launcher's history for the rationale.
    "screener_relative_volume_min": {"min": 0.0, "max": 3.0, "step": 0.1, "type": "float", "optimize": True},
    "screener_price_drop_pct": {"min": 0.0, "max": 25.0, "step": 1.0, "type": "float", "optimize": True},
    # Lookback window Y (trading days) for the price-drop gate: selects the precomputed
    # price_drop_pct_<Y> column in a multi-window store.
    "screener_price_drop_days": {"min": 2, "max": 30, "step": 1, "type": "int", "optimize": True},
    "screener_max_stocks": {"min": 10, "max": 50, "step": 10, "type": "int", "optimize": True},
    "screener_weinstein_stage2_only": {"min": 0, "max": 1, "step": 1, "type": "int", "optimize": True},
}

#: Per-cap-band jobs: the market-cap gene RANGE and a fixed ``market_cap_max`` change per band.
SCREENER_CAP_BANDS = {
    "small": {"min": 5e7,  "max": 2e9,  "step": 1e8,  "cap_max": 2e9},
    "mid":   {"min": 2e9,  "max": 1e10, "step": 1e9,  "cap_max": 1e10},
    "large": {"min": 1e10, "max": 2e11, "step": 1e10, "cap_max": None},
}


def interval_is_intraday(interval: Any) -> bool:
    """m / h / min suffix = an intraday clock (same split as the engine's ``price_source._is_intraday``).
    A missing interval is read as intraday, the conservative side (an extra visible scan, never a
    missing one)."""
    if interval is None:
        return True
    iv = str(interval).lower()
    return iv.endswith("m") or iv.endswith("h") or iv.endswith("min")


class ScreenerUniverseError(ValueError):
    """The static universe cannot be derived (unknown gene, missing range). The launch must REFUSE."""


# --- gene roles: gene name (unprefixed ``screener_`` form) -> the store setting key it drives -------
#: LOWER-bound filters: row passes when value >= setting. Loosest = the smallest value in the range;
#: a value <= 0 means "not enforced" in the store gate, so a range reaching 0 is "filter off".
_LOWER = {
    "screener_market_cap_min": "market_cap_min", "screener_relative_volume_min": "relative_volume_min",
    "screener_price_drop_pct": "price_drop_pct", "screener_price_min": "price_min",
    "screener_volume_min": "volume_min", "screener_float_min": "float_min",
    "screener_dollar_volume_min": "dollar_volume_min",
}
#: UPPER-bound filters: row passes when value <= setting. Loosest = the largest value, or off (a range
#: reaching 0 includes "not enforced").
_UPPER = {
    "screener_market_cap_max": "market_cap_max", "screener_price_max": "price_max",
    "screener_volume_max": "volume_max", "screener_float_max": "float_max",
}
#: On/off filters: loosest = off when the range includes 0, else on.
_FLAG = {"screener_weinstein_stage2_only": "weinstein_stage2_only"}
#: Selects WHICH precomputed column a threshold reads (price_drop_pct_<Y>); not a filter by itself.
_SELECTOR = {"screener_price_drop_days": "price_drop_days"}
#: ORDERING / CUT genes: they decide WHICH of the passing names a decision keeps, never which pass.
_ORDERING = {"screener_max_stocks": "max_stocks", "screener_sort_metric": "sort_metric"}
#: Setting keys the superset never carries (also stripped from ``base``).
ORDERING_KEYS = frozenset(_ORDERING.values())


def _num(gene: str, spec: Dict[str, Any], field: str) -> float:
    if not isinstance(spec, dict) or spec.get(field) is None:
        raise ScreenerUniverseError(
            f"screener gene {gene!r}: range has no {field!r} ({spec!r}); the static universe cannot be "
            f"derived from an unknown range -- refusing to guess")
    try:
        return float(spec[field])
    except (TypeError, ValueError):
        raise ScreenerUniverseError(f"screener gene {gene!r}: {field}={spec[field]!r} is not a number") from None


def gene_ranges_from_opt(scr_opt: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``{gene: spec}`` for the genes the GA actually searches (``optimize`` true), from an options
    dict shaped like ``SCREENER_OPT``. A non-optimized entry is not in the param space and has no effect."""
    return {g: s for g, s in scr_opt.items() if s and s.get("optimize")}


def gene_ranges_from_parameter_ranges(parameter_ranges: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``{gene: spec}`` from a STORED optimization's ``parameter_ranges`` (keys ``screener:<gene>``)."""
    return {k[len("screener:"):]: v for k, v in (parameter_ranges or {}).items() if k.startswith("screener:")}


def loosest_filter_variants(base: Dict[str, Any], ranges: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The list of FILTER-ONLY settings dicts whose union of selections is the superset.

    Normally ONE dict. More than one only when a drop threshold is enforced at its loosest value AND the
    window-length gene ``price_drop_days`` is searched: the threshold then reads a different precomputed
    column per Y, so the superset is the union over every Y in the gene's range.
    ``base`` is the run-level non-optimized screener settings (e.g. the cap band's ``market_cap_max``);
    a searched gene overrides it exactly as ``eff = {**base, **screener_overrides}`` does at run time.
    Raises ``ScreenerUniverseError`` for an unknown gene or a malformed range."""
    out: Dict[str, Any] = {k: v for k, v in _ms.normalize_screener_settings(base).items()
                           if k not in ORDERING_KEYS}
    y_range: Optional[range] = None
    for gene, spec in ranges.items():
        if gene in _ORDERING:
            continue                      # cannot change which names pass
        if gene in _LOWER:
            lo, hi = _num(gene, spec, "min"), _num(gene, spec, "max")
            if lo > hi:
                raise ScreenerUniverseError(f"screener gene {gene!r}: min {lo} > max {hi}")
            out[_LOWER[gene]] = 0.0 if lo <= 0 else lo
        elif gene in _UPPER:
            lo, hi = _num(gene, spec, "min"), _num(gene, spec, "max")
            if lo > hi:
                raise ScreenerUniverseError(f"screener gene {gene!r}: min {lo} > max {hi}")
            if lo <= 0:
                out.pop(_UPPER[gene], None)       # the range includes "not enforced"
            else:
                out[_UPPER[gene]] = hi
        elif gene in _FLAG:
            lo, hi = _num(gene, spec, "min"), _num(gene, spec, "max")
            if lo > hi:
                raise ScreenerUniverseError(f"screener gene {gene!r}: min {lo} > max {hi}")
            out[_FLAG[gene]] = 0 if lo <= 0 else 1
        elif gene in _SELECTOR:
            lo, hi = _num(gene, spec, "min"), _num(gene, spec, "max")
            if lo > hi:
                raise ScreenerUniverseError(f"screener gene {gene!r}: min {lo} > max {hi}")
            y_range = range(int(lo), int(hi) + 1)
        else:
            raise ScreenerUniverseError(
                f"screener gene {gene!r} has no declared role in ba2_providers.screener.universe_superset "
                f"(lower bound / upper bound / flag / window selector / ordering): the loosest value of "
                f"an unclassified gene cannot be derived. Add it to the role table, with a test, before "
                f"launching a job that searches it.")
    drop = out.get("price_drop_pct")
    if y_range is not None and drop is not None and float(drop) > 0:
        return [{**out, "price_drop_days": y} for y in y_range]
    if y_range is not None:
        out.pop("price_drop_days", None)      # threshold off: the window selector selects nothing
    return [out]


def static_universe(store_df: Any, start_day: str, end_day: str, base: Dict[str, Any],
                    ranges: Dict[str, Dict[str, Any]], *, intraday: bool,
                    excluded_symbols: "Optional[Iterable[str]]" = None, warmup_days: int = 0) -> List[str]:
    """The sorted static universe: union, over every visible scan of the window, of the names passing
    the loosest filters. NO ``max_stocks`` cut, no sort (see the module docstring)."""
    out: set = set()
    for settings in loosest_filter_variants(base, ranges):
        out.update(_ms.screened_symbol_union_visible(
            store_df, start_day, end_day, settings, excluded_symbols,
            intraday=intraday, warmup_days=warmup_days))
    return sorted(out)


def gate_selections(store_df: Any, start_day: str, end_day: str, settings: Dict[str, Any],
                    excluded_symbols: "Optional[Iterable[str]]" = None, *, intraday: bool,
                    warmup_days: int = 0) -> Dict[str, List[str]]:
    """``{scan_date: [symbols]}``: what the per-decision gate returns on each scan visible in the window
    for ONE genome's effective settings (its own filters, sort and cut). Used by the re-run warning and
    the evidence scripts; the engine itself calls ``screen_universe_for_day`` per decision."""
    win = _ms.visible_scan_window(store_df, start_day, end_day, intraday=intraday, warmup_days=warmup_days)
    if win is None:
        return {}
    out: Dict[str, List[str]] = {}
    for day in _ms.scan_dates(store_df):
        if win[0] <= day <= win[1]:
            out[day] = _ms.screen_universe_for_day(store_df, day, settings, excluded_symbols)
    return out


def count_outside(selections: Dict[str, List[str]], universe: Iterable[str]) -> Dict[str, Any]:
    """``{"gate_selected": N, "outside_static_universe": M, "first_examples": [...]}`` for per-scan
    gate selections against a static universe (the same keys the engine records at run time)."""
    uni = set(universe)
    total = outside = 0
    examples: List[Dict[str, str]] = []
    for day, syms in selections.items():
        for s in syms:
            total += 1
            if s not in uni:
                outside += 1
                if len(examples) < 10:
                    examples.append({"symbol": s, "scan_date": day})
    return {"gate_selected": total, "outside_static_universe": outside, "first_examples": examples}


def apply_cap_band(scr_opt: Dict[str, Dict[str, Any]], base: Dict[str, Any], cap_band: Optional[str]):
    """``(scr_opt', base')`` for a cap-band job: the market-cap gene RANGE becomes the band's and
    ``market_cap_max`` is pinned in the base settings (when the band has a ceiling). No band: unchanged
    (the same objects). The ONE definition the launcher and the driver previews share."""
    if not cap_band:
        return scr_opt, base
    b = SCREENER_CAP_BANDS[cap_band]
    opt = dict(scr_opt)
    opt["screener_market_cap_min"] = {"min": b["min"], "max": b["max"], "step": b["step"],
                                      "type": "float", "optimize": True}
    base = dict(base)
    if b.get("cap_max") is not None:
        base["market_cap_max"] = b["cap_max"]
    return opt, base


def cached_ohlcv_missing(symbols: Iterable[str], intervals: Iterable[str], cache_dir: str) -> List[str]:
    """Symbols lacking a native cache file ``<cache_dir>/<SYM>_<interval>.parquet`` for ANY of the
    intervals (with the provider's symbol sanitisation: '-' -> '_' / '.')."""
    import os
    ivs = sorted(set(intervals))
    out = []
    for sym in symbols:
        for iv in ivs:
            if not any(os.path.exists(os.path.join(cache_dir, f"{c}_{iv}.parquet"))
                       for c in (sym, sym.replace("-", "_"), sym.replace("-", "."))):
                out.append(sym)
                break
    return out


def preview_static_universe(store_path: str, cap_band: Optional[str], start_day: str, end_day: str,
                            interval: str, cache_dir: Optional[str] = None) -> Dict[str, Any]:
    """What a ``--screener`` job of this band/window/interval will get as its static universe, without
    launching it: ``{"size", "uncached": [...]}``. For the drivers' dry-run output. Raises
    ``ScreenerUniverseError`` like the launch does."""
    if cache_dir is None:
        import os
        from ba2_common.config import CACHE_FOLDER
        cache_dir = os.path.join(CACHE_FOLDER, "FMPOHLCVProvider")
    opt, base = apply_cap_band(SCREENER_OPT, {}, cap_band)
    uni = static_universe(_ms.load_store(store_path), start_day, end_day, base, gene_ranges_from_opt(opt),
                          intraday=interval_is_intraday(interval))
    return {"size": len(uni), "uncached": cached_ohlcv_missing(uni, {interval, "1d"}, cache_dir)}
