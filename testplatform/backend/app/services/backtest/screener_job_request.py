"""A screener-settings optimization requested over HTTP (the web UI): the server builds the job's stamps itself.

The UI sends ``screener_opt = {param_ranges, cadence_days, base_settings[, store]}`` and a ``backtest`` block of its own
shape (``expert`` + ``universe``).  This module turns that into the config the optimization handler reads, through
``screener_launch`` -- the SAME functions ``ba2-test optimize --screener`` calls -- so the job carries the same stamps
(``screener_universe_rule``, ``enabled_instruments``, ``screener_opt.{criteria_version, behaviour, panel,
panel_fingerprint, declared_ranges}``, ``excluded_instruments``) and the same screener genes the CLI would have written
for the same request.

Two error classes, mapped by the API to HTTP 4xx:
  * :class:`ScreenerRequestError` -- the REQUEST is malformed or asks for something the web path does not do (400);
  * ``screener_launch.ScreenerLaunchRefused`` -- the simulation cannot serve it (panel / coverage / uncached ...; 409), with
    the CLI's own message and list.

A request that ALREADY carries stamps (a replayed launcher config) is VERIFIED, never trusted: see :func:`_verify_stamps`.

No FastAPI import here; ``app.api.strategies`` does the HTTP mapping.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from app.services.backtest import screener_launch as sl
from ba2_providers.screener import universe_superset as us

#: The universe build reads the whole daily panel (memory-mapped, memoised per process) and the intraday cache; two
#: concurrent launches would each hold their own working set for no gain.  Launches queue here.
_BUILD_LOCK = threading.Lock()

#: What ``strategy_optimization_handler._build_daily_trial_config`` indexes directly on ``backtest`` (KeyError otherwise).
HANDLER_BACKTEST_KEYS = ("experts", "enabled_instruments", "start_date", "end_date", "initial_capital",
                         "account_settings", "warmup_days", "seed")

_STAMP_KEYS = ("criteria_version", "behaviour", "panel", "panel_fingerprint", "declared_ranges")


class ScreenerRequestError(Exception):
    """The request cannot be turned into a screener job (HTTP 400)."""


@dataclass
class AppliedScreenerRequest:
    bypass: bool                # FactorRanker: weekly store gate, name token ``-sup1`` only
    launch: sl.ScreenerLaunch
    verified_stamps: List[str]  # which supplied stamps were verified (empty for a plain UI request)


def _num(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ScreenerRequestError(f"{what} must be a number, got {v!r}")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ScreenerRequestError(f"{what} must be a number, got {v!r}") from None


def _require(d: Dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] is None or d[key] == "":
        raise ScreenerRequestError(f"{where}.{key} is required (the server states no defaults for a job)")
    return d[key]


def _scr_opt_from_param_ranges(param_ranges: Any) -> Dict[str, Dict[str, Any]]:
    """``{screener_<name>: spec}`` -- the shape of ``SCREENER_OPT`` -- from the UI's ``param_ranges`` (``market_cap_min``,
    ``screener_market_cap_min`` and ``screener:market_cap_min`` are the same gene)."""
    if not isinstance(param_ranges, dict) or not param_ranges:
        raise ScreenerRequestError("screener_opt.param_ranges is required: a screener-settings optimization searches "
                                   "at least one screener gene (omit screener_opt for a static universe)")
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in param_ranges.items():
        if not isinstance(spec, dict):
            raise ScreenerRequestError(f"screener_opt.param_ranges[{name!r}] must be an object, got {spec!r}")
        bare = str(name)
        for pre in ("screener:", "screener_"):
            if bare.startswith(pre):
                bare = bare[len(pre):]
        key = f"screener_{bare}"
        if key in out:
            raise ScreenerRequestError(f"screener_opt.param_ranges names the gene {bare!r} twice")
        out[key] = dict(spec)
    if not any(v.get("optimize") for v in out.values()):
        raise ScreenerRequestError("screener_opt.param_ranges has no range with optimize=true: nothing to search "
                                   "(omit screener_opt for a static universe)")
    return out


def _normalise_experts(bt: Dict[str, Any]) -> None:
    """The UI states ``expert: {class, settings}``; the handler reads ``experts: [{class, settings}]``."""
    experts = bt.get("experts")
    if not experts:
        exp = bt.get("expert")
        if not isinstance(exp, dict) or not exp.get("class"):
            raise ScreenerRequestError("backtest.experts (or backtest.expert.class) is required")
        bt["experts"] = [{"class": exp["class"], "settings": dict(exp.get("settings") or {})}]


def _complete_handler_block(bt: Dict[str, Any], ga_seed: Any) -> None:
    """Fill ONLY what the CLI would have written from the same inputs, then REFUSE if the handler's required keys are
    still missing (a job that fails every trial is never enqueued).

    * ``seed`` = the GA seed (the CLI writes ``backtest.seed`` = ``--seed`` = the GA seed);
    * ``warmup_days`` = ``derive_warmup_days(experts)`` (the CLI's derivation);
    * ``account_settings`` from the UI's own ``initial_capital`` / ``commission`` / ``slippage`` / ``fill_model`` with the
      daily-backtest handler's mapping (``daily_backtest_handler._build_config``); ``fill_model`` has no default.
    """
    from app.services.backtest.backtest_account import resolve_short_borrow_rate_pa
    from app.services.backtest.daily_backtest_handler import derive_warmup_days
    if bt.get("seed") is None:
        if ga_seed is None:
            raise ScreenerRequestError("optimization_config.seed is required")
        bt["seed"] = int(ga_seed)
    if bt.get("warmup_days") is None:
        bt["warmup_days"] = derive_warmup_days(bt["experts"])
    if not bt.get("account_settings"):
        for k in ("initial_capital", "commission", "slippage", "fill_model"):
            if bt.get(k) is None:
                raise ScreenerRequestError(
                    f"backtest.{k} is required (the job's account settings are built from it; the server states no "
                    f"default trading parameter)")
        try:
            borrow = resolve_short_borrow_rate_pa({"short_borrow_rate_pa": bt.get("short_borrow_rate_pa")})
        except (TypeError, ValueError) as e:
            raise ScreenerRequestError(str(e)) from None
        bt["account_settings"] = {
            "starting_cash": float(bt["initial_capital"]),
            "equity_cap": bt.get("equity_cap"),
            "commission_per_trade": float(bt["commission"]),
            "slippage_bps": float(bt["slippage"]),
            "fill_model": str(bt["fill_model"]),
            "spread_bps": float(bt.get("spread_bps") or 0.0),
            "short_borrow_rate_pa": borrow,
            "hold_assigned_stock": False,
        }
    if bt.get("subtype") is None:
        bt["subtype"] = "daily_expert"
    missing = [k for k in HANDLER_BACKTEST_KEYS if bt.get(k) in (None, "", [], {})]
    if missing:
        raise ScreenerRequestError(f"backtest is missing {missing}: the optimization handler reads them for every "
                                   f"trial, so the job would fail on its first generation")


def _panel_arg(raw: Any, cache_folder: str) -> Optional[str]:
    """A panel named by the request must be a directory under the cache's panel root (never an arbitrary path)."""
    if raw in (None, ""):
        return None
    from ba2_providers.screener import live_sim as ls
    path = ls.resolve_panel_path(str(raw), cache_folder)
    root = os.path.abspath(ls.panel_root(cache_folder))
    p = os.path.abspath(path)
    if os.path.commonpath([root, p]) != root or p == root:
        raise ScreenerRequestError(f"screener_opt.panel {raw!r} is not a panel directory under {root}")
    return p


def _verify_stamps(so_in: Dict[str, Any], bt_in: Dict[str, Any], launch: sl.ScreenerLaunch) -> List[str]:
    """A request that carries stamps is VERIFIED against the server's own build, field by field (the server always
    writes ITS values; a disagreement refuses, so a forged or stale list is never stored):

    * ``backtest.screener_universe_rule`` equals the rule the server applies;
    * ``criteria_version`` / ``behaviour`` equal the simulation's;
    * ``panel`` resolves to a panel under this machine's cache root whose manifest fingerprint equals
      ``panel_fingerprint`` (``panel_problems(expect_fp=...)``, run inside the build) and which covers the window;
    * ``declared_ranges`` equal the ranges derived from ``param_ranges``;
    * ``enabled_instruments`` equals, as a set, the static universe the server rebuilt from that panel.
    """
    out: List[str] = []
    so = launch.screener_opt
    if bt_in.get("screener_universe_rule") is not None:
        if bt_in["screener_universe_rule"] != launch.universe_rule:
            raise sl.ScreenerLaunchRefused(
                f"stamped screener_universe_rule {bt_in['screener_universe_rule']!r} != {launch.universe_rule!r}",
                surface=sl.API)
        out.append("screener_universe_rule")
    for k in ("criteria_version", "behaviour", "panel_fingerprint"):
        if so_in.get(k) is not None:
            if so_in[k] != so.get(k):
                raise sl.ScreenerLaunchRefused(
                    f"stamped screener_opt.{k} {so_in[k]!r} != the server's {so.get(k)!r}", surface=sl.API)
            out.append(k)
    if so_in.get("panel") not in (None, ""):
        if str(so_in["panel"]).replace("\\", "/") != so["panel"]:
            raise sl.ScreenerLaunchRefused(
                f"stamped screener_opt.panel {so_in['panel']!r} != the panel the server resolved {so['panel']!r}",
                surface=sl.API)
        out.append("panel")
    if so_in.get("declared_ranges") is not None:
        a = json.dumps(so_in["declared_ranges"], sort_keys=True, default=str)
        b = json.dumps(so["declared_ranges"], sort_keys=True, default=str)
        if a != b:
            raise sl.ScreenerLaunchRefused(
                f"stamped screener_opt.declared_ranges {so_in['declared_ranges']} != the ranges derived from "
                f"param_ranges {so['declared_ranges']}", surface=sl.API)
        out.append("declared_ranges")
    if bt_in.get("enabled_instruments"):
        have, want = set(bt_in["enabled_instruments"]), set(launch.enabled_instruments)
        if have != want:
            raise sl.ScreenerLaunchRefused(
                f"stamped enabled_instruments differ from the static universe the server rebuilt "
                f"({len(have - want)} extra, {len(want - have)} missing; e.g. extra {sorted(have - want)[:8]}, "
                f"missing {sorted(want - have)[:8]})", surface=sl.API)
        out.append("enabled_instruments")
    return out


def apply_screener_request(cfg: Dict[str, Any], screener_opt: Dict[str, Any], *,
                           emit: Callable[[str], None], memo: Optional[Dict[str, sl.ScreenerLaunch]] = None
                           ) -> AppliedScreenerRequest:
    """Weave a screener-settings optimization into ``cfg`` (an ``optimization_config``) IN PLACE.

    ``memo`` lets a batch share one universe build between experts of the same kind (keyed by every input).
    Raises :class:`ScreenerRequestError` or ``screener_launch.ScreenerLaunchRefused``; ``cfg`` is untouched on error.
    """
    from ba2_common.config import CACHE_FOLDER, SCREENER_STORE_DIR
    from app.services.strategy_optimization_handler import _is_bypass_expert

    if not isinstance(screener_opt, dict):
        raise ScreenerRequestError("screener_opt must be an object")
    if screener_opt.get("gate_only"):
        raise ScreenerRequestError(
            "a gate-only screener (screener_opt.gate_only: the option grids' per-bar price cap) is not launched from the "
            "web: its per-strategy caps live in the launcher's option tables. Launch it with "
            "`ba2-test optimize --screener-gate-store ...`.")
    bt_in = cfg.get("backtest")
    if not isinstance(bt_in, dict) or not bt_in:
        raise ScreenerRequestError("optimization_config.backtest is required for a screener optimization")
    bt = dict(bt_in)
    _normalise_experts(bt)
    start, end = str(_require(bt, "start_date", "backtest")), str(_require(bt, "end_date", "backtest"))
    interval = str(_require(bt, "execution_interval", "backtest"))
    cadence = _num(_require(screener_opt, "cadence_days", "screener_opt"), "screener_opt.cadence_days")
    if "base_settings" not in screener_opt or not isinstance(screener_opt["base_settings"], dict):
        raise ScreenerRequestError("screener_opt.base_settings is required (an object; {} states none)")
    scr_opt = _scr_opt_from_param_ranges(screener_opt.get("param_ranges"))
    cap_band = screener_opt.get("cap_band")
    if cap_band is not None and cap_band not in us.SCREENER_CAP_BANDS:
        raise ScreenerRequestError(f"screener_opt.cap_band {cap_band!r} is not one of {sorted(us.SCREENER_CAP_BANDS)}")
    store = os.path.expanduser(str(screener_opt.get("store") or SCREENER_STORE_DIR))
    bypass = _is_bypass_expert(bt)
    # ``apply_to_expert_settings`` is NOT a request option: it is what the launcher writes for a BYPASS expert (FactorRanker
    # reads the store + screener_* off its own settings, ``_build_daily_trial_config``).  The server derives it from the
    # expert class; a request stating the opposite is refused rather than silently overridden.
    if screener_opt.get("apply_to_expert_settings") is not None and bool(screener_opt["apply_to_expert_settings"]) != bypass:
        raise ScreenerRequestError(
            f"screener_opt.apply_to_expert_settings={screener_opt['apply_to_expert_settings']!r} contradicts the expert "
            f"(bypass expert: {bypass}); the server derives it from the expert class, omit it")
    stamped_list = bool(bt_in.get("enabled_instruments"))
    req = sl.ScreenerLaunchRequest(
        store=store, start=start, end=end, interval=interval, base_settings=screener_opt["base_settings"],
        scr_opt=scr_opt, cadence_days=cadence, bypass=bypass, surface=sl.API, cap_band=cap_band,
        panel=_panel_arg(screener_opt.get("panel"), CACHE_FOLDER),
        # a replayed config that recorded excluded_instruments was launched with the exclusion policy on
        exclude_uncached=bool(screener_opt.get("exclude_uncached") or bt_in.get("excluded_instruments")),
        expect_panel_fingerprint=screener_opt.get("panel_fingerprint") or None)
    key = json.dumps([req.store, req.start, req.end, req.interval, req.base_settings, req.scr_opt, req.cadence_days,
                      req.bypass, req.cap_band, req.panel, req.exclude_uncached, req.expect_panel_fingerprint],
                     sort_keys=True, default=str)
    launch = (memo or {}).get(key)
    if launch is None:
        with _BUILD_LOCK:
            launch = sl.build_screener_launch(req, emit=emit)
            # the FINAL universe's intraday coverage (threshold + report), exactly as the CLI runs it
            sl.intraday_coverage_preflight(launch.enabled_instruments, interval, start, end, command="optimize",
                                           surface=sl.API, emit=emit)
        if memo is not None:
            memo[key] = launch
    verified = _verify_stamps(screener_opt, bt_in, launch) if (
        stamped_list or any(screener_opt.get(k) not in (None, "") for k in _STAMP_KEYS)
        or bt_in.get("screener_universe_rule") is not None) else []
    seed_source = cfg.get("seed")
    bt["enabled_instruments"] = list(launch.enabled_instruments)
    bt["screener_universe_rule"] = launch.universe_rule
    if launch.uncached_exclusions:
        bt["excluded_instruments"] = list(launch.uncached_exclusions)
    bt["screener_opt"] = copy.deepcopy(launch.screener_opt)
    _complete_handler_block(bt, seed_source)
    expert_params = dict(cfg.get("expert_params") or {})
    expert_params.update(copy.deepcopy(launch.genes))
    cfg["backtest"] = bt
    cfg["expert_params"] = expert_params
    return AppliedScreenerRequest(bypass=bypass, launch=launch, verified_stamps=verified)


def check_warm_start(cfg: Dict[str, Any], source_results: List[Dict[str, Any]]) -> None:
    """Launch-time refusal of a warm start whose seeds lie outside the job's declared screener ranges (the handler
    checks the same at job setup, job-fatal)."""
    declared = ((cfg.get("backtest") or {}).get("screener_opt") or {}).get("declared_ranges")
    if declared and source_results:
        sl.check_warm_start_seeds(declared, source_results, int(cfg["populationSize"]))
