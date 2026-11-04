"""Launch preparation of a screener-settings optimization: ONE function, two callers.

``ba2-test optimize --screener`` (``ba2test_launcher._cmd_optimize``) and the web API
(``app.api.strategies._merge_screener_opt``) both build the job's screener stamps HERE, so a job launched from either
carries byte-identical ``backtest.screener_opt`` / ``enabled_instruments`` / ``screener_universe_rule`` /
``excluded_instruments`` and the screener genes, and therefore the same checkpoint fingerprint
(``strategy_optimization_handler.checkpoint_fingerprint``).  The steps, in the order the CLI always took them:

  1. every selection key is stated (``live_sim.require_complete_settings``; the base is the floors-OFF table);
  2. classic jobs: the daily criteria panel (``live-daily-v2``) is resolved ONCE (newest on this machine, or the one
     named) and must cover the window (``live_sim.panel_problems``: criteria version, window, stale-listed symbols,
     pending exclusion entries ...);
  3. the STATIC UNIVERSE is the true superset of the declared gene ranges (``screener_gate.static_universe`` on the
     panel; ``universe_superset.static_universe`` on the weekly store for the bypass expert FactorRanker);
  4. HERMETIC cache check: every symbol needs cached bars for the execution interval and daily; uncached ones REFUSE
     the launch unless the caller asks to EXCLUDE them (recorded as ``excluded_instruments``);
  5. (separately, on the FINAL universe) the intraday coverage preflight.

A refusal is a :class:`ScreenerLaunchRefused`; the CLI turns it into ``sys.exit`` with the unchanged text, the API into
an HTTP 4xx carrying the same body.  Nothing here falls back to the old weekly gate or leaves a job unstamped.

This module has no argparse, no HTTP and no FastAPI import.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ba2_providers.screener import universe_superset as _us

Emit = Callable[[str], None]

#: The two callers.  They differ only in how an operator's remedy is NAMED (a flag vs a request field).
CLI = "cli"
API = "api"

_NAMES = {
    CLI: {"store": "--screener-store", "exclude": "--screener-exclude-uncached",
          "uncovered": "--screener-universe-allow-uncovered",
          "refused": "optimize: --screener REFUSED: ", "plain": "optimize: "},
    API: {"store": "screener_opt.store", "exclude": "screener_opt.exclude_uncached = true",
          "uncovered": "screener_opt.universe_allow_uncovered = true",
          "refused": "screener optimization REFUSED: ", "plain": "screener optimization: "},
}


class ScreenerLaunchRefused(Exception):
    """A launch the simulation cannot serve.  ``body`` is the message without the caller's prefix; ``problems`` is the
    list when the refusal is a list (panel / coverage problems, uncached symbols)."""

    def __init__(self, body: str, *, surface: str = CLI, plain: bool = False, problems: Sequence[str] = ()):
        self.body = body
        self.surface = surface
        self.plain = plain
        self.problems = list(problems)
        super().__init__(self.text)

    @property
    def text(self) -> str:
        """The full message as the surface prints it (the CLI's text is the historical one, byte for byte)."""
        n = _NAMES[self.surface]
        return n["plain" if self.plain else "refused"] + self.body


@dataclass(frozen=True)
class ScreenerLaunchRequest:
    store: str                              # the metric-store directory (weekly store; also defines the panel's universe)
    start: str
    end: str
    interval: str                           # the execution clock, e.g. "5min", "1d"
    history_source: str                     # the job's OHLCV history ("alpaca" / "fmp"): its cache, store and panel reads
    base_settings: Dict[str, Any]           # run-level non-optimized settings as stated by the caller (raw)
    scr_opt: Dict[str, Dict[str, Any]]      # the gene ranges, keyed ``screener_<name>`` (shape of ``SCREENER_OPT``)
    cadence_days: int
    bypass: bool                            # FactorRanker: weekly store gate, no panel
    surface: str = CLI
    cap_band: Optional[str] = None
    panel: Optional[str] = None             # an explicit panel directory (absolute, or relative to the cache root)
    exclude_uncached: bool = False
    expect_panel_fingerprint: Optional[str] = None   # a replayed (already stamped) job: the panel it ran on
    #: OPTION jobs: the fixed candidate list the screen is restricted to BEFORE its max_stocks cut (the symbols the
    #: option data / market-condition manifests exist for).  None = unrestricted (every other job).
    universe_restriction: Optional[Sequence[str]] = None
    #: a restriction symbol the panel cannot select (not in the panel, or on its reviewed exclusion list) REFUSES the
    #: launch; with this it is accepted and RECORDED (``screener_opt.universe_restriction.uncovered``).
    universe_allow_uncovered: bool = False


@dataclass
class PreparedScreenerLaunch:
    """Steps 1-4a: everything up to the cache check (the CLI's ``--print-universe`` dry run stops here)."""
    request: ScreenerLaunchRequest
    use_sim: bool
    base: Dict[str, Any]
    scr_opt: Dict[str, Dict[str, Any]]
    panel_dir: Optional[str]
    enabled: List[str]                      # the static universe BEFORE the uncached policy
    uncached: List[str]
    intervals: List[str]
    cache_dir: str
    screener_opt: Dict[str, Any]            # the stamped block
    criteria_version: Optional[str]


@dataclass
class ScreenerLaunch:
    screener_opt: Dict[str, Any]
    enabled_instruments: List[str]
    uncached_exclusions: List[str]          # recorded as ``excluded_instruments`` (empty = none)
    universe_rule: str
    genes: Dict[str, Dict[str, Any]]        # ``screener:<gene>`` -> range, merged into expert_params
    use_sim: bool
    base: Dict[str, Any]
    panel_dir: Optional[str]


def _print(msg: str) -> None:
    print(msg, flush=True)


def stored_restriction_symbols(screener_opt: Dict[str, Any]) -> Optional[List[str]]:
    """The restriction symbols of a STORED ``screener_opt`` (a replay), None when the job carried none. A stored block
    that has the key but no usable list (``{}``, ``{"symbols": []}``) is returned as ``[]`` so the launch REFUSES it:
    it must never be replayed as an unrestricted job."""
    stored = screener_opt.get("universe_restriction")
    if stored is None:
        return None
    if not isinstance(stored, dict):
        raise ScreenerLaunchRefused(f"stored universe_restriction is not an object: {stored!r}", plain=True)
    syms = stored.get("symbols")
    return list(syms) if isinstance(syms, (list, tuple)) else []


def declared_ranges_of(scr_opt: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``{gene (unprefixed): {min, max}}`` of the searched genes: the ranges the static universe is the superset of."""
    return {k[len("screener_"):] if k.startswith("screener_") else k: {"min": v["min"], "max": v["max"]}
            for k, v in scr_opt.items() if v and v.get("optimize") and v.get("type") != "choice"}


def prepare_screener_launch(req: ScreenerLaunchRequest, *, emit: Emit = _print) -> PreparedScreenerLaunch:
    """Steps 1-4a.  Raises :class:`ScreenerLaunchRefused`."""
    from ba2_common.config import CACHE_FOLDER
    from ba2_common.core.deploy_parity import SCREENER_OFF_VALUES
    from ba2_providers.screener import live_sim as ls
    from ba2_providers.screener import metric_store as msb

    names = _NAMES[req.surface]
    # EVERY selection key is stated: the base is the floors OFF (explicit zeros, as the enabled prod instances carry),
    # never left to live's defaults (price_min 20, volume_min 500,000, float_min 10M).
    base = {**SCREENER_OFF_VALUES, **msb.normalize_screener_settings(req.base_settings)}
    # Cap-band job: the market-cap gene RANGE for the band and a pinned market_cap_max (disjoint universes).
    scr_opt, base = _us.apply_cap_band(req.scr_opt, base, req.cap_band)
    use_sim = not req.bypass
    if req.bypass and "screener_sort_metric" in scr_opt:
        # The bypass expert's weekly STORE gate ranks on stored columns (an absent one silently falls back to market_cap) and
        # is not the live screener: it cannot honour a searched ordering, so the gene is not searched there (said, not silent).
        scr_opt = {k: v for k, v in scr_opt.items() if k != "screener_sort_metric"}
        emit("optimize: screener_sort_metric is NOT searched for a bypass expert (its weekly store gate has no live-faithful "
             "ordering); the base sort_metric applies")
    panel_dir: Optional[str] = None
    restriction_syms: Optional[List[str]] = None
    if req.universe_restriction is not None:
        from ba2_providers.screener.symbol_universe import parse_symbols
        try:
            restriction_syms = parse_symbols(list(req.universe_restriction), what="the universe restriction")
        except ValueError as e:
            raise ScreenerLaunchRefused(str(e), surface=req.surface, plain=True) from None
        if not restriction_syms:
            raise ScreenerLaunchRefused("the universe restriction lists no symbols", surface=req.surface, plain=True)
        if not use_sim:
            raise ScreenerLaunchRefused(
                "a universe restriction needs the daily-panel gate; the bypass expert FactorRanker's weekly store gate "
                "cannot apply it", surface=req.surface, plain=True)
    if use_sim:
        allk = {**base, **{(k[len("screener_"):] if k.startswith("screener_") else k): True
                           for k, v in scr_opt.items() if v and v.get("optimize")}}
        try:
            ls.require_complete_settings(allk, where="optimize --screener")
        except ls.SimulationRefusal as e:
            msg = str(e)
            if req.surface == API:      # the remedy is named by the request field, not the CLI flag
                msg = msg.replace("--screener-base-json", "screener_opt.base_settings")
            raise ScreenerLaunchRefused(msg, surface=req.surface) from None
        emit("optimize: screener base settings (every selection key explicit; genes override): "
             + json.dumps({k: base[k] for k in sorted(base)}))
        # the newest complete panel of THIS machine, resolved ONCE and stamped on the job (path relative to the cache
        # root + fingerprint): the job never switches panel afterwards, a worker resolves the same relative path.
        if req.panel:
            panel_dir = ls.resolve_panel_path(req.panel, CACHE_FOLDER) if not os.path.isabs(req.panel) \
                else os.path.abspath(req.panel)
        else:
            panel_dir = ls.latest_panel(CACHE_FOLDER)
        probs = (ls.panel_problems(panel_dir, req.start, req.end, expect_fp=req.expect_panel_fingerprint) if panel_dir
                 else [f"no daily criteria panel under {ls.panel_root(CACHE_FOLDER)}"])
        if probs:
            raise ScreenerLaunchRefused(
                "the daily screener-criteria panel cannot serve this job:\n  - " + "\n  - ".join(probs) +
                "\nBuild / refresh it (and the vendor share data it needs) with:\n"
                f"  ba2-test prewarm --screener-panel --screener-store {req.store} "
                f"--start {req.start} --end {req.end}\n"
                "or `ba2-test build-screener-metrics ... --daily-panel`. Remote workers receive it with "
                "`cache push` (directory screener/daily_panel, ~0.8 GB).", surface=req.surface, problems=probs)
        exm = (ls.read_manifest(panel_dir) or {}).get("excluded_unusable") or []
        if exm:
            emit(f"optimize: screener panel EXCLUDES {len(exm)} listed symbols with knowingly unusable data (reviewed list "
                 f"ba2_providers/screener/panel_exclusions.json): "
                 + "; ".join(f"{e['symbol']} ({e['reason'][:60]}..., {e['added']}, reviewed by {e['reviewed_by']})"
                              for e in exm))
    ackd = (ls.read_manifest(panel_dir) or {}).get("acknowledged_delisted") or [] if use_sim else []
    if ackd:
        emit(f"optimize: screener panel ACKNOWLEDGES {len(ackd)} known delistings (history kept through each last session, "
             f"never selected after; reviewed list ba2_providers/screener/panel_exclusions.json): "
             + "; ".join(f"{e['symbol']} (last session {e['last_session']}, {e['added']}, reviewed by {e['reviewed_by']})"
                         for e in ackd))
    screener_opt = {
        "store": req.store,
        "base_settings": base,
        **({"criteria_version": ls.CRITERIA_VERSION, "behaviour": ls.POST_FIX.name,
            "panel": ls.panel_rel(CACHE_FOLDER, panel_dir),
            "panel_fingerprint": ls.read_manifest(panel_dir)["panel_fingerprint"]} if use_sim else {}),
        # the ranges the static universe is the superset of: a genome outside them is refused
        "declared_ranges": declared_ranges_of(scr_opt),
        "cadence_days": int(req.cadence_days),
        # BYPASS experts (FactorRanker) ignore the classic entry-gate path: they read the store and the screener_*
        # settings off their OWN expert settings (`_build_daily_trial_config` pushes them each generation).
        "apply_to_expert_settings": bool(req.bypass),
    }
    if restriction_syms is not None:
        from app.services.backtest import screener_gate as _sgr
        _pan = _sgr.get_panel(panel_dir, history_source=req.history_source)
        _blocked = set(_sgr.ms.EXCLUDED_SYMBOLS) | {str(e["symbol"]).upper() for e in
                                                    (_pan.manifest.get("excluded_unusable") or [])}
        _selectable = {str(x).upper() for x in _pan.symbols} - _blocked
        uncovered = [x for x in restriction_syms if x not in _selectable]
        if uncovered and not req.universe_allow_uncovered:
            raise ScreenerLaunchRefused(
                f"{len(uncovered)} of the {len(restriction_syms)} restriction symbols cannot be selected by the panel "
                f"{panel_dir} (not in it, or on its reviewed exclusion list): {', '.join(uncovered)}. Rebuild the panel "
                f"for them, or pass {names['uncovered']} to accept it (recorded on the run).",
                surface=req.surface, problems=uncovered)
        if uncovered:
            emit(f"optimize: universe restriction: {len(uncovered)} symbols the panel cannot select are accepted and "
                 f"RECORDED: {', '.join(uncovered)}")
        restriction_stamp = {"symbols": restriction_syms,
                             "sha256": hashlib.sha256("\n".join(restriction_syms).encode()).hexdigest(),
                             "uncovered": uncovered}
        screener_opt["universe_restriction"] = restriction_stamp
        emit(f"optimize: screener universe restricted to {len(restriction_syms)} symbols "
             f"(sha256 {restriction_stamp['sha256'][:12]}), applied before the max_stocks cut")
    from app.services.backtest.history_source import load_job_store
    store_df = load_job_store(req.store, {"ohlcv_history_source": req.history_source})
    if store_df.empty:
        raise ScreenerLaunchRefused(f"{names['store']} {req.store!r} has no symbols", surface=req.surface, plain=True)
    # THE STATIC UNIVERSE = the true SUPERSET (universe_superset): every symbol that passes the LOOSEST values of the
    # FILTER genes on at least one scan visible during [start, end], NO max_stocks cut, no sort.  A gene whose range
    # or role is unknown REFUSES the launch (ScreenerUniverseError): the loosest value is never guessed.
    ranges = _us.gene_ranges_from_opt(scr_opt)
    try:
        if use_sim:
            from app.services.backtest import screener_gate as sg   # the SAME function as the gate and the prune
            enabled = sg.static_universe(sg.get_panel(panel_dir, history_source=req.history_source), req.start, req.end, base, ranges,
                                         intraday=_us.interval_is_intraday(req.interval), restrict_to=restriction_syms)
        else:
            enabled = _us.static_universe(store_df, req.start, req.end, base, ranges,
                                          intraday=_us.interval_is_intraday(req.interval))
    except _us.ScreenerUniverseError as e:
        raise ScreenerLaunchRefused(str(e), surface=req.surface) from None
    if not enabled:
        raise ScreenerLaunchRefused(
            f"{names['store']} {req.store!r} selected zero symbols for {req.start}..{req.end} under the loosest "
            f"filter settings", surface=req.surface, plain=True)
    emit(f"optimize: screener static universe = {len(enabled)} symbols "
         f"(rule {_us.RULE_ID}: loosest-filter superset over the visible scans of "
         f"{req.start}..{req.end}, no max_stocks cut)")
    # HERMETIC cache check (the backtest never fetches mid-run): bars for the execution interval AND daily.
    from ba2_common.core.ohlcv_history import history_cache_dirname
    cache_dir = os.path.join(CACHE_FOLDER, history_cache_dirname(req.history_source))
    ivs = sorted({req.interval, "1d"})

    def has_bars(sym: str) -> bool:
        for iv in ivs:
            if not any(os.path.exists(os.path.join(cache_dir, f"{c}_{iv}.parquet"))
                       for c in (sym, sym.replace("-", "_"), sym.replace("-", "."))):
                return False
        return True

    uncached = [s for s in enabled if not has_bars(s)]
    return PreparedScreenerLaunch(request=req, use_sim=use_sim, base=base, scr_opt=scr_opt, panel_dir=panel_dir,
                                  enabled=enabled, uncached=uncached, intervals=ivs, cache_dir=cache_dir,
                                  screener_opt=screener_opt,
                                  criteria_version=(ls.CRITERIA_VERSION if use_sim else None))


def apply_uncached_policy(prep: PreparedScreenerLaunch, *, emit: Emit = _print) -> ScreenerLaunch:
    """Step 4b: refuse the uncached symbols, or EXCLUDE them (recorded) when the caller asked."""
    req = prep.request
    names = _NAMES[req.surface]
    enabled, uncached, ivs = list(prep.enabled), list(prep.uncached), prep.intervals
    exclusions: List[str] = []
    if uncached:
        if not req.exclude_uncached:
            raise ScreenerLaunchRefused(
                f"{len(uncached)} of the {len(enabled)} static-universe symbols have no cached {'/'.join(ivs)} OHLCV "
                f"under {prep.cache_dir}: {', '.join(uncached)}. Fetch them (ba2-test fetch-cache --timeframes "
                f"{','.join(ivs)} --symbols ...), or pass {names['exclude']} to EXCLUDE them from the screen "
                f"(recorded on the run as excluded_instruments).", surface=req.surface, problems=uncached)
        exclusions = list(uncached)
        gone = set(uncached)
        enabled = [s for s in enabled if s not in gone]
        emit(f"optimize: --screener-exclude-uncached: EXCLUDING {len(uncached)} screened symbols "
             f"with no cached {'/'.join(ivs)} OHLCV from the screen: {', '.join(uncached)} "
             f"-> {len(enabled)} tradeable.")
    if not enabled:
        raise ScreenerLaunchRefused(
            f"0 of the screened universe has cached {'/'.join(ivs)} OHLCV -- fetch it first "
            f"(ba2-test fetch-cache --timeframes {req.interval} ...).", surface=req.surface, plain=True)
    return ScreenerLaunch(screener_opt=prep.screener_opt, enabled_instruments=enabled, uncached_exclusions=exclusions,
                          universe_rule=_us.RULE_ID, genes={f"screener:{k}": v for k, v in prep.scr_opt.items()},
                          use_sim=prep.use_sim, base=prep.base, panel_dir=prep.panel_dir)


def build_screener_launch(req: ScreenerLaunchRequest, *, emit: Emit = _print) -> ScreenerLaunch:
    """Steps 1-4 in one call (the API's entry; the CLI calls the two halves around its ``--print-universe`` dry run)."""
    return apply_uncached_policy(prepare_screener_launch(req, emit=emit), emit=emit)


# ---- step 5: the FINAL universe's intraday coverage ----------------------------------------------------------------

def coverage_report(symbols: Sequence[str], interval: str, start: str, end: str,
                    history_source: str) -> Optional[Dict[str, Any]]:
    """Per-symbol-day intraday coverage of these symbols in the cache of ``history_source`` (the job's own history),
    None on a daily clock."""
    from ba2_common.config import CACHE_FOLDER
    from ba2_common.core.ohlcv_history import history_cache_dirname
    from ba2_providers.screener import intraday_coverage as ic
    if not _us.interval_is_intraday(interval):
        return None
    return ic.scan(symbols, os.path.join(CACHE_FOLDER, history_cache_dirname(history_source)), interval, start, end)


def intraday_coverage_preflight(symbols: Sequence[str], interval: str, start: str, end: str, history_source: str, *,
                                command: str = "optimize", surface: str = CLI, emit: Emit = _print) -> Optional[Dict[str, Any]]:
    """LAUNCH REFUSAL (every intraday job): more than the stated share of symbol-days without an intraday bar.  The
    report is emitted either way.  The refusal's ``problems`` is the list; the CLI prints it as it always did."""
    from ba2_providers.screener import intraday_coverage as ic
    rep = coverage_report(symbols, interval, start, end, history_source)
    if rep is None:
        return None
    emit(f"{command}: {ic.format_report(rep)}")
    probs = ic.problems(rep)
    if probs:
        raise ScreenerLaunchRefused("REFUSED (intraday coverage):\n  - " + "\n  - ".join(probs), surface=surface,
                                    plain=True, problems=probs)
    return rep


# ---- identity -------------------------------------------------------------------------------------------------------

def universe_rule_name(name: str, *, simulated: bool = True) -> str:
    """``name`` with the superset-universe-rule token and, for a job that runs through the live-simulation gate
    (every classic screener job), the criteria token (idempotent).  The grid drivers
    (``tools/matrix_flags.with_universe_rule_name``) apply the same tokens; a test pins the two equal."""
    from ba2_providers.screener import live_sim as ls
    out = name if UNIVERSE_RULE_NAME_TOKEN in name else name + UNIVERSE_RULE_NAME_TOKEN
    if simulated and ls.CRITERIA_NAME_TOKEN not in out:
        out += ls.CRITERIA_NAME_TOKEN
    return out


UNIVERSE_RULE_NAME_TOKEN = "-sup1"


def check_warm_start_seeds(declared: Dict[str, Dict[str, Any]], source_results: Sequence[Dict[str, Any]],
                           population_size: int, *, where: str = "warm-start") -> None:
    """Refuse AT LAUNCH a warm-start source whose seeds (the last ``population_size`` evaluated individuals, as
    ``strategy_optimization_handler._build_warm_start_population`` takes them) hold a screener gene outside the
    DECLARED ranges.  The handler checks the same at job setup (job-fatal); this is the early, 4xx form."""
    tail = list(source_results)[-population_size:] if len(source_results) > population_size else list(source_results)
    for n, e in enumerate(tail):
        vals = {k[len("screener:"):]: v for k, v in (e.get("params") or {}).items() if str(k).startswith("screener:")}
        _us.check_genes_in_declared_ranges(vals, declared, where=f"{where} individual #{n}")
