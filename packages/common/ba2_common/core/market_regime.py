"""Market-regime classification from a benchmark's daily closes (SPY by default).

WHY THIS EXISTS: every TradeCondition in the codebase is PER-SYMBOL (rating, target,
position state, risk level). Nothing is market-wide. But strategy performance -- especially
premium selling -- is regime-dependent, so a system that cannot name the current regime
cannot adapt to it.

DESIGN IS EVIDENCE-DRIVEN, not intuition. Measured on cached SPY daily closes
2011-06-22 -> 2026-06-30 (3,777 sessions), asking which causal signal best separates the
FORWARD 21-session outcome, split into three independent sub-periods to reject
single-episode artifacts. Separation of forward realized volatility (pct points):

    signal              2011-2018   2019-2022   2023-2026   FULL
    close vs SMA200         7.1        10.3        10.8      10.4   <- most consistent
    drawdown from 252d high 9.9        11.9         5.8      12.1
    Weinstein stage         7.7        14.7        12.6      11.4   <- NOT monotone, see below
    realized-vol rank       4.9        10.4         6.3       7.8
    hand-combined score     5.5        11.9         6.7       8.8   <- WORSE than its parts

THREE CONCLUSIONS THAT SHAPED THIS MODULE:

1. THIS SIGNAL is a RISK regime, not a DIRECTION forecast. Careful about the general
   claim here: trend DOES predict direction -- time-series momentum (Moskowitz, Ooi &
   Pedersen 2012) is robust across 58 instruments and 25+ years, Sharpe ~1.28 vs 0.38
   buy-and-hold. What the literature says about THIS construction (a 200-day SMA cross on
   a single equity index) is narrower: its value is volatility and drawdown reduction, not
   return enhancement. Since 1951, SMA200 timing returns 7.11% at 10.1% vol (Sharpe 0.704)
   against buy-and-hold's 7.24% at 15.37% (Sharpe 0.471) -- LOWER return, much lower risk;
   over 1929-2019 it cut max drawdown from 83.4% to 29.6%.

   Our own 3,777-session sample cannot settle direction either way, and a check confirms
   why: the 568 "below SMA200" observations collapse to just EIGHT independent episodes
   (2015 x2, 2018, 2020, 2022 x2, 2025, 2026), of which two fell. Overlapping daily windows
   inflated n=8 into an apparent n=568. That sample even showed below-trend predicting
   HIGHER forward returns -- the opposite sign to time-series momentum -- which is a signal
   that the window is unrepresentative, not a finding.

   So: use this as a RISK/vol regime, which is what it is documented to deliver and what
   our data can actually support. Volatility clustering is separately one of the most
   replicated effects in finance, and volatility is the quantity that decides whether short
   premium wins (realized vs implied) and whether a position gets run over. If a DIRECTIONAL
   regime is wanted later, build it the way the literature does -- 12-month time-series
   momentum, applied across assets -- not as an SMA cross on one index.

2. Expose ORTHOGONAL PRIMITIVES, do not hand-combine. A hand-tuned 2-factor score scored
   WORSE than plain SMA200 in every sub-period. Trend and volatility are exposed as two
   independent axes so the GA can search their combination, rather than us baking in a
   blend the data does not support.

3. Weinstein stage is NOT a risk ladder. Its stage ORDER does not track forward risk:
   Stage 1 (basing) shows HIGHER forward vol than Stage 4 (declining) -- 23.2 vs 20.7 over
   the full sample. It is a good per-stock entry filter (its actual purpose, and how the
   screener already uses it); it is not a market risk regime. Use ``weinstein.py`` for the
   former and this module for the latter.

LIVE-COMPUTABLE BY CONSTRUCTION -- the requirement that drove signal choice. Every input is
the benchmark's own daily closes, which the live platform and the backtest both already read
through the same OHLCV provider. Nothing here needs VIX (not in the OHLCV cache, would need
a new fetch path plus as-of clamping and carries lookahead risk), no macro feed, and no
full-sample fit. ``classify_*`` take a plain oldest-first list, so the caller controls the
as-of cut and lookahead is structurally impossible.

Pure functions: no IO, no DB, no network.
"""
from typing import List, Optional

# Trend regime states.
RISK_ON = "risk_on"      # benchmark above its long SMA
RISK_OFF = "risk_off"    # benchmark below it -- ~2x forward realized vol, historically

# Volatility regime states, ordered calm -> stressed.
CALM = "calm"
NORMAL = "normal"
STRESSED = "stressed"

# 200 sessions ~= the widely-watched 200-day moving average. Chosen on measured consistency
# (never below 7.1 points of forward-vol separation in any sub-period), not on convention.
_TREND_SMA_PERIOD = 200
# 20 sessions ~= 1 month of realized vol, ranked within ~2 years of its own history. The
# rank (not the level) is what makes this comparable across eras -- a 15% VIX-equivalent
# meant something different in 2017 than in 2022.
_VOL_WINDOW = 20
_VOL_RANK_LOOKBACK = 504
_CALM_PCTL = 30.0
_STRESSED_PCTL = 70.0


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def classify_trend_regime(closes: List[float], sma_period: int = _TREND_SMA_PERIOD) -> dict:
    """Trend regime from the benchmark's position vs its long SMA.

    The single most CONSISTENT of the signals measured -- separation never fell below 7.1
    points of forward realized vol in any sub-period, where the drawdown-based signal
    collapsed to 5.8 in 2023-2026.

    ``closes``: benchmark daily closes, OLDEST FIRST, cut at the caller's as-of date.
    Returns ``{regime, sma, price, distance_pct, reason}``; ``regime`` is None when there is
    not enough history (fail loud rather than guessing a regime).
    """
    out = {"regime": None, "sma": None, "price": None, "distance_pct": None, "reason": ""}
    vals = [float(c) for c in closes if c is not None]
    if len(vals) < sma_period:
        out["reason"] = f"insufficient history ({len(vals)} bars, need {sma_period})"
        return out
    sma = _sma(vals, sma_period)
    if not sma or sma <= 0:
        out["reason"] = "could not compute SMA"
        return out
    price = vals[-1]
    out.update({
        "regime": RISK_ON if price > sma else RISK_OFF,
        "sma": round(sma, 4), "price": price,
        "distance_pct": round((price / sma - 1.0) * 100.0, 3),
    })
    return out


def _realized_vol(closes: List[float], window: int) -> Optional[float]:
    """Annualised realized volatility (%) of the last ``window`` log returns."""
    if len(closes) < window + 1:
        return None
    rets = []
    for a, b in zip(closes[-(window + 1):-1], closes[-window:]):
        if a is None or b is None or a <= 0 or b <= 0:
            return None
        rets.append((b / a) - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5) * 100.0


def classify_volatility_regime(closes: List[float], window: int = _VOL_WINDOW,
                               rank_lookback: int = _VOL_RANK_LOOKBACK,
                               calm_pctl: float = _CALM_PCTL,
                               stressed_pctl: float = _STRESSED_PCTL) -> dict:
    """Volatility regime from the PERCENTILE RANK of trailing realized vol.

    Deliberately independent of :func:`classify_trend_regime` -- see the module docstring on
    why the two axes are not pre-combined. Weaker alone than the trend signal, but it fires
    on vol shocks that leave price above its SMA (Feb 2018 being the canonical case), which
    is exactly the event that ruins a short-premium book.

    Ranked rather than absolute so it is comparable across eras. ``closes``: benchmark daily
    closes, OLDEST FIRST, cut at the caller's as-of date.
    """
    out = {"regime": None, "realized_vol": None, "rank_pct": None, "reason": ""}
    vals = [float(c) for c in closes if c is not None]
    need = window + rank_lookback
    if len(vals) < need:
        out["reason"] = f"insufficient history ({len(vals)} bars, need {need})"
        return out

    # Trailing series of realized vol, one value per session in the lookback.
    series = []
    for end in range(len(vals) - rank_lookback, len(vals) + 1):
        rv = _realized_vol(vals[:end], window)
        if rv is not None:
            series.append(rv)
    if len(series) < 2:
        out["reason"] = "could not compute realized-vol series"
        return out

    current = series[-1]
    # DEGENERATE SERIES: with no dispersion every value ties, so the "<=" rank collapses to
    # 100% and a perfectly steady market would be reported as STRESSED -- the opposite of the
    # truth. A rank is only meaningful against a distribution that actually varies, so treat
    # a flat one as NORMAL (no evidence of stress) and say why.
    if max(series) - min(series) <= 0:
        out.update({"regime": NORMAL, "realized_vol": round(current, 4), "rank_pct": None,
                    "reason": "realized-vol series has no dispersion; rank undefined"})
        return out
    rank = sum(1 for v in series if v <= current) / len(series) * 100.0
    if rank < calm_pctl:
        regime = CALM
    elif rank > stressed_pctl:
        regime = STRESSED
    else:
        regime = NORMAL
    out.update({"regime": regime, "realized_vol": round(current, 4),
                "rank_pct": round(rank, 2)})
    return out


def is_risk_on(closes: List[float], **kwargs) -> bool:
    """True when the benchmark is in the RISK_ON trend regime. Unknown regime -> False
    (fail closed: an unclassifiable market is not treated as safe)."""
    return classify_trend_regime(closes, **kwargs).get("regime") == RISK_ON


def is_stressed(closes: List[float], **kwargs) -> bool:
    """True when trailing realized vol sits in its STRESSED percentile band. Unknown -> False."""
    return classify_volatility_regime(closes, **kwargs).get("regime") == STRESSED
