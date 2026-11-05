"""ML forecast expert (design docs/plans/2026-10-25-ml-forecast-expert-design.md): reads one row of a precomputed
forecast store (TimesFM 3.0 deciles, Kronos sampled-path probabilities, ...) and maps it to a BUY/SELL/HOLD through
the pure ``ba2_common.core.ml_forecast.mapping``. The forecast is computed ONCE per (model key, symbol, session) by
``tools/ml_forecast/build_store.py``; every GA genome only re-maps it. Research-only: backtest-registered, not live
(the live model server is milestone 4).

Point in time: the row read is the latest session <= ``knowability.scan_cutoff_date(as_of)`` (daily clock: the
decision date D, by the D-close convention; intraday clock: the last FINISHED session). The ATR that scales the
uncertainty gate is Wilder ATR14 over the completed daily bars up to that same session.

Exits: the forecast take-profit becomes ``expected_profit_percent`` (the platform's target channel: S1's
``expert_target_price`` anchor is ``price * (1 + expected_profit_percent / 100)``); the forecast stop has no
order channel, so the RM stop (``risk_per_trade_pct`` / ``min_stop_loss_pct``) and the ruleset exits stand.
On the open-positions pass the expert answers the FLIP question (``exit_flip_prob``): SELL when the bearish
probability reaches it, BUY when the bullish one does, else HOLD; the ruleset's exit-on-opposite rule then closes a
held position whose side the flip opposes. A missing forecast row is a skip (``no_forecast``), never a default.

Ranking mode (``rank_top_n`` > 0, long-only): the entry pass ranks the EXPLICIT universe ``rank_universe`` (a sorted
symbol list, required and non-empty in this mode; its ``rank_universe_id`` is recorded with every decision) by the
bullish score at the cutoff session (``mapping.bullish_score``) among the symbols whose latest row is fresh. The
store directory is never listed, so another symbol's file (a locked holdout, a wider build) cannot change a rank; a
universe symbol with no store file aborts the run (an incomplete worker store). A symbol may BUY only inside the top
N, and still only through the usual gates; outside it the answer is ``HOLD ... rank k of n``. On the open-positions
pass a held long that is no longer inside the top N gets SELL (a rotation exit, fixed confidence
``ROTATION_EXIT_CONFIDENCE``). A held symbol whose forecast row went STALE is skipped (``no_forecast``), not rotated:
only the strategy's time exit closes it. Only rows with session <= the cutoff are read (``core.ml_forecast.ranking``).

Settings added with the ranking mode, ``rank_top_n`` (default 0) and ``rank_universe`` (default ""), are read through
their settings-definition default when absent: a persisted instance that predates them reproduces the pre-ranking
behaviour exactly (ranking off). That is the only default in this module.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, time, timedelta, timezone
from typing import Mapping

import numpy as np

from ba2_common.core.db import add_instance, update_instance
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.knowability import scan_cutoff_date
from ba2_common.core.market_conditions import atr14_wilder
from ba2_common.core.ml_forecast import mapping, ranking, store
from ba2_common.core.models import ExpertRecommendation
from ba2_common.core.types import (AnalysisUseCase, MarketAnalysisStatus, OrderRecommendation, Recommendation,
                                   RiskLevel, TimeHorizon)
from ba2_common.logger import get_expert_logger, logger

HORIZONS = (5, 10, 20)
#: Calendar days of daily bars read for ATR14 (about 40 sessions: the Wilder seed plus holiday slack).
ATR_LOOKBACK_DAYS = 60
#: Daily bars of backtest warmup covering ATR_LOOKBACK_DAYS (the handler converts at 1.45 days per bar).
BACKTEST_WARMUP_BARS = math.ceil(ATR_LOOKBACK_DAYS / 1.45)
SKIP_NO_FORECAST = "no_forecast"
#: Confidence of a rotation SELL (the held long dropped out of the top N). A rank is not a probability, so the figure
#: is fixed rather than derived from P(up); callers act on the signal.
ROTATION_EXIT_CONFIDENCE = 50.0
#: Store aliases built for the 2026-10 pilot (TimesFM 3.0 univariate in LOG space, ctx 512 / 2048). The GA's
#: ``model_key`` gene chooses among the aliases listed in the launcher's _EXPERT_OPT; this default is ctx 512.
DEFAULT_MODEL_KEY = "timesfm3-log-ctx512"


def _num(settings: Mapping, key: str, lo: float, hi: float) -> float:
    if key not in settings:
        raise ValueError(f"Missing setting {key!r}")
    v = settings[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ValueError(f"Setting {key!r} must be a finite number, got {v!r}")
    v = float(v)
    if not lo <= v <= hi:
        raise ValueError(f"Setting {key!r} must be in [{lo}, {hi}], got {v!r}")
    return v


def store_horizon(key: str) -> int:
    """The single horizon of store ``key`` (``horizon`` setting 0). A store with several horizons refuses."""
    hs = list(store.read_meta(key)["horizons"])
    if len(hs) != 1:
        raise ValueError(f"Setting 'horizon' 0 needs a single-horizon store; store {key} has {hs}")
    return int(hs[0])


def mapping_genes(settings: Mapping, key: str = None) -> mapping.MappingGenes:
    """The validated mapping genes of ``settings`` (no defaults: a missing key raises). ``horizon`` 0 means the
    store's own single horizon (the custom models each predict one horizon); it needs ``key``."""
    horizon = int(_num(settings, "horizon", 0, 1000))
    if horizon == 0:
        if key is None:
            raise ValueError("Setting 'horizon' 0 (the store's horizon) needs the resolved model key")
        horizon = store_horizon(key)
    elif horizon not in HORIZONS:
        raise ValueError(f"Setting 'horizon' must be one of {HORIZONS} or 0 (the store's horizon), got {horizon}")
    enable_sell = settings["enable_sell"]
    if isinstance(enable_sell, (int, float)) and not isinstance(enable_sell, bool) and enable_sell in (0, 1):
        enable_sell = bool(enable_sell)
    return mapping.MappingGenes(
        horizon=horizon, min_edge_pct=_num(settings, "min_edge_pct", 0.0, 100.0),
        min_prob=_num(settings, "min_prob", 50.000001, 100.0),
        max_uncertainty=_num(settings, "max_uncertainty", 1e-9, 1000.0),
        tp_quantile=_num(settings, "tp_quantile", 50.000001, 90.0),
        sl_quantile=_num(settings, "sl_quantile", 10.0, 49.999999),
        exit_flip_prob=_num(settings, "exit_flip_prob", 50.000001, 100.0), enable_sell=enable_sell)


def _setting_or_definition_default(settings: Mapping, key: str):
    """``settings[key]``, or the settings-definition default when the key is absent. Used ONLY for the two ranking
    settings (see the module docstring): a persisted instance without them must keep its pre-ranking behaviour."""
    if key in settings:
        return settings[key]
    return MLForecast.get_settings_definitions()[key]["default"]


def rank_top_n(settings: Mapping) -> int:
    """The validated ``rank_top_n`` setting: an int >= 0, 0 = ranking off (also when the key is absent)."""
    v = _setting_or_definition_default(settings, "rank_top_n")
    v = _num({"rank_top_n": v}, "rank_top_n", 0, 100000)
    if v != int(v):
        raise ValueError(f"Setting 'rank_top_n' must be a whole number, got {settings['rank_top_n']!r}")
    return int(v)


def rank_universe_and_id(settings: Mapping) -> tuple:
    """``(symbols, rank_universe_id)`` of the ``rank_universe`` setting: sorted, de-duplicated, upper-cased symbols from
    a list or one comma-separated string (as the launcher writes it, so it travels as an ordinary string setting).
    Empty (ranking off) is ``((), "")``. The parse and the id are memoised (``ranking.parse_universe``)."""
    raw = _setting_or_definition_default(settings, "rank_universe")
    if isinstance(raw, str):
        if not any(x.strip() for x in raw.split(",")):
            return (), ""
    elif isinstance(raw, (list, tuple)) and all(isinstance(x, str) for x in raw):
        if not raw:
            return (), ""
    else:
        raise ValueError(f"Setting 'rank_universe' must be a list of symbols or a comma-separated string, got {raw!r}")
    return ranking.parse_universe(raw)


_alias_memo: dict = {}


def resolve_model_key(name: str) -> str:
    """A store key (16 hex chars) or a store alias -> the store key.

    The alias is the build's ``card_id`` (``timesfm3-ctx512``, ``kronos-small`` ...) plus ``+<covariate>`` per
    covariate, read from each built store's ``meta.json`` (``build.card_id``, ``covariate_set``): one source, the
    store itself. Zero or several stores answering an alias raise."""
    if isinstance(name, str) and len(name) == 16 and all(ch in "0123456789abcdef" for ch in name):
        return name
    if name in _alias_memo:
        return _alias_memo[name]
    root = store.root()
    hits = []
    for meta_p in sorted(root.glob("*/meta.json")):
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        alias = "+".join([meta["build"]["card_id"], *sorted(meta["covariate_set"])])
        if alias == name:
            hits.append(meta["model_key"])
    if len(hits) != 1:
        raise ValueError(f"model_key alias {name!r} matches {len(hits)} stores under {root} ({hits}); "
                         f"give the 16-hex key")
    _alias_memo[name] = hits[0]
    return hits[0]


class MLForecast(MarketExpertInterface):
    """Backtest-registered research expert over a precomputed forecast store (see the module docstring)."""

    BACKTEST_WARMUP_BARS = BACKTEST_WARMUP_BARS
    _skips_logged = None

    @classmethod
    def description(cls):
        return "Maps a precomputed ML price forecast (TimesFM 3.0 / Kronos deciles and path probabilities) to signals"

    @classmethod
    def get_settings_definitions(cls):
        return {
            "model_key": {"type": "str", "required": True, "default": DEFAULT_MODEL_KEY,
                          "description": "Forecast store: a 16-hex model key or a pilot alias (timesfm3-ctx512 ...)"},
            "horizon": {"type": "int", "required": True, "default": 10,
                        "description": "Forecast horizon traded, in sessions (5, 10 or 20; 0 = the store's single "
                                       "horizon, for the custom models)"},
            "min_edge_pct": {"type": "float", "required": True, "default": 1.0,
                             "description": "Minimum |median| forecast return in percent"},
            "min_prob": {"type": "float", "required": True, "default": 60.0,
                         "description": "Minimum P(right direction) (decile models) or P(TP before SL) (path models), %"},
            "max_uncertainty": {"type": "float", "required": True, "default": 4.0,
                                "description": "Maximum q10-q90 forecast width in ATR14 multiples"},
            "tp_quantile": {"type": "float", "required": True, "default": 70.0,
                            "description": "Take-profit decile (long; mirrored for a short), 55-90"},
            "sl_quantile": {"type": "float", "required": True, "default": 30.0,
                            "description": "Stop decile (long; mirrored for a short), 10-45 (informational: no SL channel)"},
            "exit_flip_prob": {"type": "float", "required": True, "default": 60.0,
                               "description": "Opposite-side probability (%) that flips a held position"},
            "enable_sell": {"type": "bool", "required": True, "default": False,
                            "description": "Emit SELL entries on bearish forecasts (short side)"},
            "rank_top_n": {"type": "int", "required": True, "default": 0,
                           "description": "Ranking mode (long-only): BUY only the N most bullish symbols of "
                                          "rank_universe at the decision date and SELL a held long that drops out of "
                                          "the top N; 0 = off. Decile stores clamp P(up) to [0.05, 0.95], so among "
                                          "strongly bullish symbols the ranking is effectively by the median "
                                          "forecast (q50)"},
            "rank_universe": {"type": "str", "required": True, "default": "",
                              "description": "Ranking mode: the comma-separated, sorted symbols that are ranked "
                                             "(required, non-empty when rank_top_n > 0; the launcher fills it with "
                                             "the job's own symbols). Never the store's directory contents"},
        }

    _SETTING_KEYS = ("model_key", "horizon", "min_edge_pct", "min_prob", "max_uncertainty", "tp_quantile",
                     "sl_quantile", "exit_flip_prob", "enable_sell", "rank_top_n", "rank_universe")

    def __init__(self, id):
        super().__init__(id)
        self._load_expert_instance(id)
        self.logger = get_expert_logger("MLForecast", id)

    # ------------------------------------------------------------------ helpers
    def _log_once(self, symbol, kind, message):
        logged = self._skips_logged
        if logged is None:
            logged = self._skips_logged = set()
        first = (symbol, kind) not in logged
        logged.add((symbol, kind))
        (logger.warning if first else logger.debug)(message)

    @staticmethod
    def _atr(provider, symbol, session, as_of) -> float:
        """Wilder ATR14 of the completed daily bars up to and including ``session``."""
        start = datetime.combine(session - timedelta(days=ATR_LOOKBACK_DAYS), time(0), tzinfo=timezone.utc)
        frame = provider.get_ohlcv_data(symbol, start_date=start, end_date=as_of, lookback_days=ATR_LOOKBACK_DAYS,
                                        interval="1d")
        if frame is None or len(frame) == 0:
            raise ValueError(f"Missing daily OHLCV for {symbol} (ATR14)")
        stamps = frame["Date"].values if frame["Date"].dtype.kind == "M" else \
            np.asarray(__import__("pandas").to_datetime(frame["Date"], utc=True).values)
        keep = stamps < (np.datetime64(session, "D") + np.timedelta64(1, "D"))
        order = np.argsort(stamps[keep], kind="stable")
        h, l, c = (frame[k].to_numpy(dtype=float)[keep][order] for k in ("High", "Low", "Close"))
        if len(c) < 16:
            raise ValueError(f"Insufficient daily bars for ATR14 of {symbol}: {len(c)} up to {session}")
        atr = float(atr14_wilder(h, l, c)[-1])
        if not (math.isfinite(atr) and atr > 0):
            raise ValueError(f"Invalid ATR14 for {symbol} on {session}: {atr!r}")
        return atr

    # ------------------------------------------------------------------ decision
    def _analyze(self, symbol, providers, settings, as_of, use_case):
        key = resolve_model_key(settings["model_key"])
        genes = mapping_genes(settings, key)
        top_n = rank_top_n(settings)
        if top_n and genes.enable_sell:
            raise ValueError("Setting 'rank_top_n' > 0 is long-only: 'enable_sell' must be off")
        ranks = n_ranked = None
        universe, universe_id = rank_universe_and_id(settings) if top_n else ((), "")
        if top_n and not universe:
            raise ValueError("Setting 'rank_universe' is required (a non-empty symbol list) when 'rank_top_n' > 0")
        if top_n and symbol.strip().upper() not in universe:
            raise ValueError(f"{symbol} is not in the 'rank_universe' of {len(universe)} symbols")
        cutoff = scan_cutoff_date(as_of)
        price = self._decision_price(providers, symbol, as_of)
        if price is None:
            raise ValueError(f"No decision price for {symbol} at {as_of}")
        if top_n:
            # BEFORE the row read: a universe symbol without a store file (an incomplete worker store) raises here
            # even when it is the symbol being analysed, instead of becoming a quiet no_forecast skip
            ranks, n_ranked = ranking.session_ranks(key, cutoff, genes.horizon, universe, universe_id=universe_id)
        try:
            row = store.read_row(key, symbol, cutoff, genes.horizon)
        except store.ForecastStoreMiss as exc:
            msg = f"MLForecast skip {symbol}: no forecast row in store {key} for {cutoff}: {exc}"
            self._log_once(symbol, SKIP_NO_FORECAST, msg)
            return Recommendation(signal=OrderRecommendation.HOLD, confidence=0.0, current_price=price,
                                  details=msg, expected_profit_percent=0.0, skip=True, skip_reason=SKIP_NO_FORECAST)
        raw = {"model_key": key, "forecast_session": row.session.isoformat(), "horizon": genes.horizon}
        if row.has_classes:
            raw["classes"] = [list(x) for x in row.class_probs[genes.horizon]]
        else:
            raw["deciles"] = list(row.deciles[genes.horizon])
        rank = None
        if top_n:
            if row.symbol not in ranks:
                raise store.ForecastStoreError(f"{key}/{row.symbol}: read_row found a fresh row for {cutoff} that the "
                                               f"session ranking does not contain")
            rank = ranks[row.symbol]
            raw.update({"rank": rank, "rank_n": n_ranked, "rank_top_n": top_n,
                        "rank_universe_id": universe_id})
        if use_case == AnalysisUseCase.OPEN_POSITIONS:
            if top_n and rank > top_n:
                # rotation exit: the held long is no longer among the N most bullish symbols
                return Recommendation(signal=OrderRecommendation.SELL, confidence=ROTATION_EXIT_CONFIDENCE,
                                      current_price=price, expected_profit_percent=0.0,
                                      details=f"{symbol} SELL dropped out of top {top_n}: rank {rank} of {n_ranked}",
                                      raw_outputs={**raw, "pass": "open_positions"})
            # side-agnostic flip: SELL if bearish p >= flip, BUY if bullish p >= flip (only the side the held
            # position opposes acts, through the ruleset's exit-on-opposite rule)
            down = mapping.map_held_position(row, genes, "long")
            res = down if down.signal != OrderRecommendation.HOLD else mapping.map_held_position(row, genes, "short")
            if res.signal == OrderRecommendation.HOLD:
                p_up = mapping.p_up(row, genes.horizon)
                res = mapping.MappingResult(
                    OrderRecommendation.HOLD, mapping.confidence_pct(max(p_up, 1 - p_up)), None, None, None, None,
                    f"HOLD held: neither side reaches exit_flip_prob {genes.exit_flip_prob:g}% (p_up {p_up * 100:.1f}%)")
            return Recommendation(signal=res.signal, confidence=res.confidence, current_price=price,
                                  expected_profit_percent=0.0, details=f"{symbol} {res.reason}",
                                  raw_outputs={**raw, "pass": "open_positions"})
        if top_n and rank > top_n:
            p_up = mapping.p_up(row, genes.horizon)
            return Recommendation(signal=OrderRecommendation.HOLD,
                                  confidence=mapping.confidence_pct(max(p_up, 1.0 - p_up)), current_price=price, expected_profit_percent=0.0,
                                  details=f"{symbol} HOLD rank {rank} of {n_ranked} (outside top {top_n})",
                                  raw_outputs={**raw, "pass": "entry"})
        atr = self._atr(providers.ohlcv(), symbol, cutoff, as_of)
        res = mapping.map_forecast(row, genes, price, atr)
        exp = 0.0
        if res.signal != OrderRecommendation.HOLD:
            # the take-profit distance is the platform's target channel; the q50 edge when the TP was dropped
            exp = abs(res.tp_price / price - 1.0) * 100.0 if res.tp_price is not None else res.expected_profit_percent
        return Recommendation(signal=res.signal, confidence=res.confidence, current_price=price,
                              expected_profit_percent=exp, target_price=res.tp_price,
                              details=f"{symbol} {res.reason} (atr14 {atr:.4f}, price {price:.4f}"
                                      + (f", rank {rank} of {n_ranked})" if top_n else ")"),
                              raw_outputs={**raw, "atr14": atr, "tp_price": res.tp_price, "sl_price": res.sl_price,
                                           "pass": "entry"})

    def analyze_as_of(self, as_of, context):
        symbol = context.extra["symbol"] if "symbol" in context.extra else self._gather_symbol
        try:
            return self._analyze(symbol, context.providers, context.settings, as_of, context.subtype)
        except (ValueError, KeyError, store.ForecastStoreError) as exc:
            # Missing ATR history, a corrupt store or a bad setting must abort the run, not become a quiet HOLD.
            from ba2_providers.fmp_common import FMPHistoryCacheMiss
            raise FMPHistoryCacheMiss(f"MLForecast cannot evaluate {symbol}: {exc}") from exc

    def run_analysis(self, symbol, market_analysis):
        market_analysis.status = MarketAnalysisStatus.FAILED
        market_analysis.state = {"error": "MLForecast is research-only: the live model server (milestone 4) is not built"}
        update_instance(market_analysis)

    def render_market_analysis(self, market_analysis):
        return json.dumps(market_analysis.state, indent=2)
