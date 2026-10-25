"""Pure forecast-row -> signal mapping (design section 2). No I/O, no clock.

Entry (``map_forecast``), in gate order:
  1. uncertainty: (q90 - q10) * decision_price / atr  <=  max_uncertainty            ("max_uncertainty")
  2. direction: sign of the median return q50 (zero median -> HOLD)
  3. shorting: a bearish forecast with ``enable_sell`` off -> HOLD                       ("sell disabled")
  4. edge: |q50| in percent  >=  min_edge_pct                                          ("min_edge")
  5. probability in percent  >=  min_prob                                              ("min_prob")
  6. TP / SL must lie on the correct side of the entry price                           ("stop"/"target")

Probability used:
  * decile models: ``p_up_from_deciles`` (below); the bearish probability is ``1 - p_up``.
  * path models (row carries a P(TP before SL) grid): the cell nearest to the TP/SL distances implied by the chosen
    quantiles, in the direction traded (long: TP = q[tp_quantile], SL = q[sl_quantile]; short mirrored).

TP/SL prices come from the quantiles (long: TP = price * (1 + q[tp_quantile]), SL = price * (1 + q[sl_quantile]);
short: mirrored through ``100 - level``). A stop quantile that is not on the loss side of the entry (a very
confident forecast whose q10 is already positive) is NOT replaced by a made-up distance: the result is HOLD.

Held positions (``map_held_position``): a flip to the opposite rating once the opposite-side probability
(decile based) reaches ``exit_flip_prob``. It ignores ``enable_sell`` (closing a long is not opening a short).

Confidence is the probability used, in percent, clamped to 1-100 (also reported on HOLD, informationally).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from ..types import OrderRecommendation
from .forecast_row import DECILE_LEVELS, SL_GRID_PCT, TP_GRID_PCT, ForecastRow

#: Clamp of P(up) when 0% lies outside the q10..q90 range: the tails are unobserved, so the probability is never
#: reported beyond 5% / 95%.
P_TAIL_CLAMP = 0.05


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class MappingGenes:
    horizon: int
    min_edge_pct: float
    min_prob: float          # percent, 50 < x <= 100
    max_uncertainty: float   # ATR multiples of the q10-q90 width
    tp_quantile: float       # 50 < x <= 90
    sl_quantile: float       # 10 <= x < 50
    exit_flip_prob: float    # percent, 50 < x <= 100
    enable_sell: bool

    def __post_init__(self):
        if isinstance(self.horizon, bool) or not isinstance(self.horizon, int) or self.horizon <= 0:
            raise ValueError(f"horizon must be a positive int, got {self.horizon!r}")
        for n in ("min_edge_pct", "min_prob", "max_uncertainty", "tp_quantile", "sl_quantile", "exit_flip_prob"):
            if not _is_num(getattr(self, n)):
                raise ValueError(f"{n} must be a finite number, got {getattr(self, n)!r}")
        if self.min_edge_pct < 0:
            raise ValueError("min_edge_pct must be >= 0")
        if not 50 < self.min_prob <= 100:
            raise ValueError("min_prob must be in (50, 100]")
        if self.max_uncertainty <= 0:
            raise ValueError("max_uncertainty must be > 0")
        if not 50 < self.tp_quantile <= 90:
            raise ValueError("tp_quantile must be in (50, 90]")
        if not 10 <= self.sl_quantile < 50:
            raise ValueError("sl_quantile must be in [10, 50)")
        if not 50 < self.exit_flip_prob <= 100:
            raise ValueError("exit_flip_prob must be in (50, 100]")
        if not isinstance(self.enable_sell, bool):
            raise TypeError(f"enable_sell must be bool, got {self.enable_sell!r}")


@dataclass(frozen=True)
class MappingResult:
    signal: OrderRecommendation
    confidence: float                       # 1-100
    expected_profit_percent: Optional[float]
    target_price: Optional[float]
    tp_price: Optional[float]
    sl_price: Optional[float]
    reason: str


def quantile_return(deciles: Sequence[float], level: float) -> float:
    """Return at percentile ``level`` (10..90), linear between the stored deciles."""
    if not 10 <= level <= 90:
        raise ValueError(f"quantile level must be within 10..90, got {level}")
    pos = (level - 10) / 10.0
    lo = int(math.floor(pos))
    if lo >= len(DECILE_LEVELS) - 1:
        return float(deciles[-1])
    frac = pos - lo
    return float(deciles[lo] + frac * (deciles[lo + 1] - deciles[lo]))


def p_up_from_deciles(deciles: Sequence[float]) -> float:
    """P(return > 0) from the 9 deciles: 1 - F(0) with F the piecewise-linear CDF through (q_i, level_i).

    * 0% inside the range: linear interpolation between the neighbouring deciles. When 0% is hit exactly by one or
      more deciles (a flat segment), F(0) is the midpoint of the first and last such level.
    * 0% below q10 (all deciles positive): F(0) = 0.05, i.e. P(up) = 0.95. 0% above q90: F(0) = 0.95, P(up) = 0.05.
      The tails are unobserved, so the probability never exceeds [0.05, 0.95].
    """
    r = [float(x) for x in deciles]
    levels = [l / 100.0 for l in DECILE_LEVELS]
    if r[0] > 0:
        return 1.0 - P_TAIL_CLAMP
    if r[-1] < 0:
        return P_TAIL_CLAMP
    zeros = [l for l, x in zip(levels, r) if x == 0.0]
    if zeros:
        return 1.0 - (zeros[0] + zeros[-1]) / 2.0
    for i in range(len(r) - 1):
        if r[i] < 0 < r[i + 1]:
            f = levels[i] + (0.0 - r[i]) / (r[i + 1] - r[i]) * (levels[i + 1] - levels[i])
            return 1.0 - f
    raise AssertionError(f"unreachable: deciles not monotone? {r}")   # ForecastRow guarantees monotone


def _nearest(grid: Sequence[int], value_pct: float) -> int:
    return min(grid, key=lambda g: (abs(g - value_pct), g))


def _conf(p: float) -> float:
    return min(100.0, max(1.0, p * 100.0))


def _hold(p_lead: float, reason: str) -> MappingResult:
    return MappingResult(OrderRecommendation.HOLD, _conf(p_lead), None, None, None, None, reason)


def _check_pos(name: str, x) -> float:
    if not _is_num(x) or x <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {x!r}")
    return float(x)


def _dec(row: ForecastRow, horizon: int):
    if horizon not in row.deciles:
        raise ValueError(f"{row.symbol} {row.session}: horizon {horizon} not in row (has {row.horizons})")
    return row.deciles[horizon]


def map_forecast(row: ForecastRow, genes: MappingGenes, decision_price: float, atr: float) -> MappingResult:
    price = _check_pos("decision_price", decision_price)
    atr = _check_pos("atr", atr)
    h = genes.horizon
    d = _dec(row, h)
    p_up = p_up_from_deciles(d)
    q50 = d[DECILE_LEVELS.index(50)]
    lead = max(p_up, 1.0 - p_up)

    unc = (d[-1] - d[0]) * price / atr
    if unc > genes.max_uncertainty:
        return _hold(lead, f"HOLD gate=max_uncertainty: q10-q90 width {unc:.2f} ATR > {genes.max_uncertainty:g}")
    if q50 == 0:
        return _hold(lead, "HOLD gate=direction: median return is zero")
    long_ = q50 > 0
    if not long_ and not genes.enable_sell:
        return _hold(1.0 - p_up, "HOLD gate=sell: bearish forecast but sell disabled (enable_sell=0)")
    edge = abs(q50) * 100.0
    if edge < genes.min_edge_pct:
        return _hold(lead, f"HOLD gate=min_edge: |q50| {edge:.2f}% < {genes.min_edge_pct:g}%")

    tp_lvl, sl_lvl = (genes.tp_quantile, genes.sl_quantile) if long_ else (100 - genes.tp_quantile, 100 - genes.sl_quantile)
    tp_ret, sl_ret = quantile_return(d, tp_lvl), quantile_return(d, sl_lvl)
    side = "long" if long_ else "short"

    cell = ""
    if row.has_paths:
        tp_cell = _nearest(TP_GRID_PCT, abs(tp_ret) * 100.0)
        sl_cell = _nearest(SL_GRID_PCT, abs(sl_ret) * 100.0)
        p = row.path_prob(side, h, tp_cell, sl_cell)
        cell = f" P(TP<SL)[{side} tp_cell={tp_cell} sl_cell={sl_cell}]"
    else:
        p = p_up if long_ else 1.0 - p_up
    if p * 100.0 < genes.min_prob - 1e-9:
        return _hold(p, f"HOLD gate=min_prob: p {p * 100:.1f}%{cell} < {genes.min_prob:g}%")

    if long_:
        ok_tp, ok_sl = tp_ret > 0, sl_ret < 0
    else:
        ok_tp, ok_sl = tp_ret < 0, sl_ret > 0
    if not ok_tp:
        return _hold(p, f"HOLD gate=target: take-profit quantile return {tp_ret:+.4f} is not in the profit direction")
    if not ok_sl:
        return _hold(p, f"HOLD gate=stop: stop quantile return {sl_ret:+.4f} is not on the loss side of the entry")

    tp_price, sl_price = price * (1.0 + tp_ret), price * (1.0 + sl_ret)
    sig = OrderRecommendation.BUY if long_ else OrderRecommendation.SELL
    reason = (f"{sig.value} h={h} q50={q50 * 100:+.2f}% p={p * 100:.1f}%{cell} width={unc:.2f}ATR "
              f"tp={tp_price:.4f} sl={sl_price:.4f}")
    return MappingResult(sig, _conf(p), edge, tp_price, tp_price, sl_price, reason)


def map_held_position(row: ForecastRow, genes: MappingGenes, side: str) -> MappingResult:
    """SELL (flip) for a held long / BUY for a held short once the opposite-side probability >= exit_flip_prob."""
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    p_up = p_up_from_deciles(_dec(row, genes.horizon))
    p_opp = (1.0 - p_up) if side == "long" else p_up
    if p_opp * 100.0 >= genes.exit_flip_prob - 1e-9:
        sig = OrderRecommendation.SELL if side == "long" else OrderRecommendation.BUY
        return MappingResult(sig, _conf(p_opp), None, None, None, None,
                             f"{sig.value} flip of held {side}: opposite-side p {p_opp * 100:.1f}% >= "
                             f"exit_flip_prob {genes.exit_flip_prob:g}%")
    return _hold(1.0 - p_opp, f"HOLD held {side}: opposite-side p {p_opp * 100:.1f}% < exit_flip_prob "
                              f"{genes.exit_flip_prob:g}%")
