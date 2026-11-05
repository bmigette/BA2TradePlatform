"""Pure forecast-row -> signal mapping (design section 2). No I/O, no clock.

Entry (``map_forecast``), in gate order:
  1. uncertainty: (q90 - q10) * decision_price / atr  <=  max_uncertainty            ("max_uncertainty")
  2. direction: sign of the median return q50 (zero median -> HOLD)
  3. shorting: a bearish forecast with ``enable_sell`` off -> HOLD                       ("sell disabled")
  4. edge: |q50| in percent  >=  min_edge_pct                                          ("min_edge")
  5. probability in percent  >=  min_prob                                              ("min_prob")
  6. (not a gate) TP / SL prices: see below

Probability used:
  * decile models: ``p_up_from_deciles`` (below); the bearish probability is ``1 - p_up``.
  * path models (row carries a P(TP before SL) grid): the cell nearest to the TP/SL distances implied by the chosen
    quantiles, in the direction traded (long: TP = q[tp_quantile], SL = q[sl_quantile]; short mirrored). A distance
    outside the grid range or more than half a grid step from its cell gives HOLD with gate=grid (no silent
    snapping). A wrong-side exit has no cell: the decile probability is used and that exit is returned as None.

TP/SL prices come from the quantiles (long: TP = price * (1 + q[tp_quantile]), SL = price * (1 + q[sl_quantile]);
short: mirrored through ``100 - level``). A forecast TP/SL that is on the wrong side of the entry (a very
confident forecast whose q10 is already positive) or closer than MIN_EXIT_DISTANCE_ATR x ATR is returned as
``None`` with a note in ``reason``; the signal is still emitted. It is NEVER replaced by a made-up level: the
CALLER must then apply the ruleset / ATR stop safeguard, so a trade never opens with no protective exit.

Held positions (``map_held_position``): a flip to the opposite rating once the opposite-side probability
(decile based) reaches ``exit_flip_prob``. It ignores ``enable_sell`` (closing a long is not opening a short).

Confidence is the probability used, in percent, clamped to 1-100. On HOLD it is informational only: the probability
of the leading side (the gate-failing side where one was chosen), NOT a confidence in holding. Callers must act on
``signal``, never threshold a HOLD's confidence.

``expected_profit_percent`` is the profit in the TRADE direction: +|q50| for a BUY and +|q50| for a SELL (a short
profits when the price falls), so it is positive whenever a signal is emitted.

Class-probability rows (``row.has_classes``: down / mid / up, each with a probability and a return value) map as:
  1. direction: sign of the expected return E = sum(p_c x r_c) (zero -> HOLD); bearish with ``enable_sell`` off -> HOLD
  2. edge: |E| in percent  >=  min_edge_pct
  3. probability: P(up) / (P(up) + P(down)) for a long (for the triple barrier: P(take-profit before stop | a barrier
     is hit)), its complement for a short  >=  min_prob
  4. exits: TP at the up class's return, SL at the down class's (mirrored for a short), through the same
     wrong-side / minimum-distance rules as the decile exits.
``max_uncertainty``, ``tp_quantile`` and ``sl_quantile`` do not apply to class rows (the classes carry their own
levels). Held positions flip once the opposite side's conditional probability reaches ``exit_flip_prob``.
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
#: Forecast TP/SL prices closer to the entry than this many ATR are degenerate and dropped (set to None).
MIN_EXIT_DISTANCE_ATR = 0.25


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
    * Known discontinuity: with q10 exactly 0 (or just below) P(up) is ~0.90, but with q10 just above 0 it jumps to
      0.95 (and symmetrically at q90 = 0 for 0.10 -> 0.05). The jump is 0.05 and is accepted: the mass beyond q10 is
      not observed, so the tail clamp is a convention rather than an estimate.
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


def _snap(grid: Sequence[int], value_pct: float) -> Optional[int]:
    """Nearest grid cell, or None when the value is outside [grid[0], grid[-1]] or farther from the nearest cell
    than half of that cell's smallest adjacent step."""
    if value_pct < grid[0] or value_pct > grid[-1]:
        return None
    g = min(grid, key=lambda c: (abs(c - value_pct), c))
    i = grid.index(g)
    steps = [abs(grid[k] - g) for k in (i - 1, i + 1) if 0 <= k < len(grid)]
    return g if abs(g - value_pct) <= min(steps) / 2.0 + 1e-9 else None


def confidence_pct(p: float) -> float:
    """A probability as a confidence on the platform's 1-100 scale (clamped); the expert's own HOLDs use it too."""
    return min(100.0, max(1.0, p * 100.0))


def _hold(p_lead: float, reason: str) -> MappingResult:
    return MappingResult(OrderRecommendation.HOLD, confidence_pct(p_lead), None, None, None, None, reason)


def _check_pos(name: str, x) -> float:
    if not _is_num(x) or x <= 0:
        raise ValueError(f"{name} must be a positive finite number, got {x!r}")
    return float(x)


def _dec(row: ForecastRow, horizon: int):
    if horizon not in row.deciles:
        raise ValueError(f"{row.symbol} {row.session}: horizon {horizon} not in row (has {row.horizons})")
    return row.deciles[horizon]


def _classes(row: ForecastRow, horizon: int):
    if horizon not in row.class_probs:
        raise ValueError(f"{row.symbol} {row.session}: horizon {horizon} not in row (has {row.horizons})")
    return row.class_probs[horizon]


def p_up_from_classes(cf) -> float:
    """P(up) / (P(up) + P(down)): the probability that the decisive outcome is the up class (0.5 when both are 0)."""
    (p_d, _), _, (p_u, _) = cf
    return p_u / (p_u + p_d) if p_u + p_d > 0 else 0.5


def p_up(row: ForecastRow, horizon: int) -> float:
    """The bullish probability the mapping uses, for any row type."""
    if row.has_classes:
        return p_up_from_classes(_classes(row, horizon))
    return p_up_from_deciles(_dec(row, horizon))


def bullish_score_deciles(d: Sequence[float]) -> tuple:
    return (p_up_from_deciles(d), float(d[DECILE_LEVELS.index(50)]))


def bullish_score_classes(cf) -> tuple:
    return (p_up_from_classes(cf), float(sum(p * r for p, r in cf)))


def bullish_score(row: ForecastRow, horizon: int) -> tuple:
    """The cross-sectional ranking key of a row, higher = more bullish, for any row type: ``(P(up), median/expected
    return)``. P(up) is the bullish probability the mapping uses; decile P(up) is clamped to [0.05, 0.95], so a strong
    forecast ties with every other strong one and the second element (q50 for deciles, E[r] = sum(p_c x r_c) for
    class rows) separates them: for decile stores the ranking is effectively by q50 among clamped symbols. Exact ties
    on both are left to the caller (the ranking breaks them by symbol name)."""
    if row.has_classes:
        return bullish_score_classes(_classes(row, horizon))
    return bullish_score_deciles(_dec(row, horizon))


def _map_classes(row: ForecastRow, genes: MappingGenes, price: float, atr: float) -> MappingResult:
    h = genes.horizon
    cf = _classes(row, h)
    pu = p_up_from_classes(cf)
    lead = max(pu, 1.0 - pu)
    exp = sum(p * r for p, r in cf)
    if exp == 0:
        return _hold(lead, "HOLD gate=direction: expected return is zero")
    long_ = exp > 0
    if not long_ and not genes.enable_sell:
        return _hold(1.0 - pu, "HOLD gate=sell: bearish forecast but sell disabled (enable_sell=0)")
    edge = abs(exp) * 100.0
    if edge < genes.min_edge_pct:
        return _hold(lead, f"HOLD gate=min_edge: |E[r]| {edge:.2f}% < {genes.min_edge_pct:g}%")
    p = pu if long_ else 1.0 - pu
    if p * 100.0 < genes.min_prob - 1e-9:
        return _hold(p, f"HOLD gate=min_prob: P(decisive {'up' if long_ else 'down'}) {p * 100:.1f}% < "
                        f"{genes.min_prob:g}%")
    (_, r_dn), _, (_, r_up) = cf
    tp_ret, sl_ret = (r_up, r_dn) if long_ else (r_dn, r_up)
    notes = []
    tp_price = _exit_price("take-profit", price, tp_ret, long_, True, atr, notes)
    sl_price = _exit_price("stop", price, sl_ret, long_, False, atr, notes)
    sig = OrderRecommendation.BUY if long_ else OrderRecommendation.SELL
    probs = "/".join(f"{x * 100:.0f}" for x, _ in cf)
    reason = (f"{sig.value} h={h} classes(d/m/u)={probs}% E[r]={exp * 100:+.2f}% p={p * 100:.1f}% "
              f"tp={'none' if tp_price is None else f'{tp_price:.4f}'} "
              f"sl={'none' if sl_price is None else f'{sl_price:.4f}'}")
    if notes:
        reason += "; " + "; ".join(notes)
    return MappingResult(sig, confidence_pct(p), edge, tp_price, tp_price, sl_price, reason)


def map_forecast(row: ForecastRow, genes: MappingGenes, decision_price: float, atr: float) -> MappingResult:
    price = _check_pos("decision_price", decision_price)
    atr = _check_pos("atr", atr)
    if row.has_classes:
        return _map_classes(row, genes, price, atr)
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
        # the exit distances the quantiles imply must sit on the grid: no silent snapping onto another TP/SL pair
        profit_ok = (tp_ret > 0) if long_ else (tp_ret < 0)
        loss_ok = (sl_ret < 0) if long_ else (sl_ret > 0)
        if profit_ok and loss_ok:
            tp_cell = _snap(TP_GRID_PCT, abs(tp_ret) * 100.0)
            sl_cell = _snap(SL_GRID_PCT, abs(sl_ret) * 100.0)
            if tp_cell is None or sl_cell is None:
                return _hold(lead, f"HOLD gate=grid: implied TP {abs(tp_ret) * 100:.2f}% / SL {abs(sl_ret) * 100:.2f}% "
                                   f"is outside the grid or more than half a grid step from a cell")
            p = row.path_prob(side, h, tp_cell, sl_cell)
            cell = f" P(TP<SL)[{side} tp_cell={tp_cell} sl_cell={sl_cell}]"
        else:
            # no grid cell exists for a wrong-side exit: decide on the decile probability, exits become None below
            which = "stop" if not loss_ok else "take-profit"
            side_word = "profit" if not loss_ok else "loss"
            p = p_up if long_ else 1.0 - p_up
            cell = (f" [path grid has no cell for a forecast {which} on the {side_word} side; decile probability "
                    f"used; ruleset/ATR safeguard applies]")
    else:
        p = p_up if long_ else 1.0 - p_up
    if p * 100.0 < genes.min_prob - 1e-9:
        return _hold(p, f"HOLD gate=min_prob: p {p * 100:.1f}%{cell} < {genes.min_prob:g}%")

    notes = []
    tp_price = _exit_price("take-profit", price, tp_ret, long_, True, atr, notes)
    sl_price = _exit_price("stop", price, sl_ret, long_, False, atr, notes)
    sig = OrderRecommendation.BUY if long_ else OrderRecommendation.SELL
    reason = (f"{sig.value} h={h} q50={q50 * 100:+.2f}% p={p * 100:.1f}%{cell} width={unc:.2f}ATR "
              f"tp={'none' if tp_price is None else f'{tp_price:.4f}'} "
              f"sl={'none' if sl_price is None else f'{sl_price:.4f}'}")
    if notes:
        reason += "; " + "; ".join(notes)
    return MappingResult(sig, confidence_pct(p), edge, tp_price, tp_price, sl_price, reason)


def _exit_price(name: str, price: float, ret: float, long_: bool, is_tp: bool, atr: float, notes: list) -> Optional[float]:
    """Forecast exit price, or None (with a note) when the quantile is on the wrong side of the entry or closer
    than MIN_EXIT_DISTANCE_ATR x ATR. Never replaced by an invented level: the caller applies the ruleset/ATR safeguard."""
    profit_side = (ret > 0) if long_ else (ret < 0)
    if profit_side != is_tp:
        notes.append(f"forecast {name} not on {'profit' if is_tp else 'loss'} side; ruleset/ATR safeguard applies")
        return None
    out = price * (1.0 + ret)
    if abs(out - price) < MIN_EXIT_DISTANCE_ATR * atr:
        notes.append(f"forecast {name} closer than the minimum {MIN_EXIT_DISTANCE_ATR:g} ATR; "
                     f"ruleset/ATR safeguard applies")
        return None
    return out


def map_held_position(row: ForecastRow, genes: MappingGenes, side: str) -> MappingResult:
    """SELL (flip) for a held long / BUY for a held short once the opposite-side probability >= exit_flip_prob."""
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    pu = p_up(row, genes.horizon)
    p_opp = (1.0 - pu) if side == "long" else pu
    if p_opp * 100.0 >= genes.exit_flip_prob - 1e-9:
        sig = OrderRecommendation.SELL if side == "long" else OrderRecommendation.BUY
        return MappingResult(sig, confidence_pct(p_opp), None, None, None, None,
                             f"{sig.value} flip of held {side}: opposite-side p {p_opp * 100:.1f}% >= "
                             f"exit_flip_prob {genes.exit_flip_prob:g}%")
    return _hold(1.0 - p_opp, f"HOLD held {side}: opposite-side p {p_opp * 100:.1f}% < exit_flip_prob "
                              f"{genes.exit_flip_prob:g}%")
