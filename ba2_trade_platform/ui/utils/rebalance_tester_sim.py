"""The allocator's REBALANCE TESTER: the arithmetic. Pure -- no NiceGUI, no IO, no clock, no database.

"Would rebalancing back to the target weights every 1 / 3 / 6 / 12 months have beaten simply holding?" -- per label
(its members at their in-label weights) or for the whole account (each symbol at label target x in-label weight). A
read-only what-if: nothing here places, sizes or schedules an order.

THE SIMULATION (the rules of the owner's prototype, kept on purpose)
* TOTAL RETURN, daily: each member's daily return is ``(close_t + distributions_t) / close_{t-1} - 1`` -- the
  distributions are REINVESTED in the member that paid them. Holdings DRIFT with their returns between rebalances.
* HOLD never rebalances. A rebalancing variant trades back to its weights at the LAST SESSION OF A MONTH, every N
  months (N in ``FREQUENCIES``). For N > 1 every START OFFSET (0..N-1: which month-end of the cycle is the first) is
  run; the table shows the MEAN over offsets and the min..max of the annualised return, so a lucky calendar month is
  visible. The chart draws offset 0.
* TILT (default off): at a rebalance ``w_i ∝ target_i * clip(1 + k * r_i, TILT_FLOOR, TILT_CAP)``, normalised, where
  ``r_i`` is the member's return since the previous rebalance (since the start for the first). ``k`` is
  ``k_winners`` for a member with a positive period return and ``k_losers`` for a negative one. k > 0 follows the move
  (more to a winner / less to a loser), k < 0 leans against it.
* COST: ``cost_bps`` of the traded value (buys + sells) at every rebalance, taken out of the portfolio pro rata.
* TURNOVER per year: traded value (buys + sells) / portfolio value, summed over the rebalances, per year of window.

THE WINDOW
* Members with weight 0 are out (counted). A member with no price history at all is out and NAMED.
* The window starts at the lookback start (1, 3 or 5 years back), unless the members listed by then cover less than
  ``MIN_COVERAGE`` of the weight; then it starts at the earliest first-price date where they do. A member whose first
  price is after the window start is OUT and NAMED with that date. Shown: start, members in, share of weight covered.
* REFUSED (``RebalanceTestResult.refusal``, no numbers): fewer than ``MIN_MEMBERS`` members in, a window shorter than
  ``MIN_WINDOW_DAYS``, listed members never covering ``MIN_COVERAGE``, or a member in the window whose distribution
  history could not be fetched / carries an unknown amount inside the window (a total return without it would be
  silently low).

INPUTS have the shapes ``services.label_growth.fetch_label_growth_inputs`` returns: ``closes`` =
``{SYMBOL: {YYYY-MM-DD: close}}`` split-adjusted and NOT dividend-adjusted, ``distributions`` =
``{SYMBOL: DistributionHistory}`` on the same split basis. :func:`simulate_frame` is the core and takes a price frame
directly (total-return levels, one column per member).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .label_growth_sim import GAP_DAYS, DistributionHistory, horizon_start

#: Rebalance every N months.
FREQUENCIES: Tuple[int, ...] = (1, 3, 6, 12)
#: Lookback choices (years) and the one the dialog opens with.
LOOKBACK_YEARS: Tuple[int, ...] = (1, 3, 5)
DEFAULT_LOOKBACK_YEARS = 5
DEFAULT_COST_BPS = 10.0
DEFAULT_K = 0.0
#: The tilt factor ``1 + k * r`` is clipped to this band before normalising.
TILT_FLOOR = 0.25
TILT_CAP = 3.0
#: The window starts where the listed members cover at least this share of the weight.
MIN_COVERAGE = 0.60
MIN_MEMBERS = 2
#: ~6 months: a shorter window has at most five monthly rebalances and no 6- or 12-month one.
MIN_WINDOW_DAYS = 182
DAYS_PER_YEAR = 365.25

HOLD = 'hold'
VARIANT_NAMES = {HOLD: 'Hold (never rebalance)', 1: 'Every month', 3: 'Every 3 months', 6: 'Every 6 months',
                 12: 'Every 12 months'}
#: Chart colours, fixed per variant (never by rank). Validated (dataviz validate_palette, dark mode, on the dialog's
#: #1a1f2e surface): lightness band, chroma, CVD and normal-vision separation, contrast all pass. Hold is also DASHED,
#: and every line is in the legend and the table, so identity is never colour alone.
VARIANT_COLORS = {HOLD: '#3987e5', 1: '#d95926', 3: '#199e70', 6: '#c98500', 12: '#d55181'}

UNKNOWN_TEXT = 'n/a'


class RebalanceInputError(ValueError):
    """A parameter or weight the simulation cannot run on (shown to the user as is)."""


# ---------------------------------------------------------------------------
# parameters and results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RebalanceParams:
    lookback_years: int = DEFAULT_LOOKBACK_YEARS
    cost_bps: float = DEFAULT_COST_BPS
    #: Tilt coefficient for members whose return since the last rebalance is POSITIVE / NEGATIVE.
    k_winners: float = DEFAULT_K
    k_losers: float = DEFAULT_K


@dataclass
class WindowInfo:
    lookback_start: str
    #: First and last session simulated (None when refused before a window existed).
    start: Optional[str]
    end: Optional[str]
    years: float
    #: Members in the simulation and their normalised weights (sum 1).
    members: List[str]
    weights: Dict[str, float]
    #: Members with a positive weight (in or out) and the share of that weight the members in cover, in %.
    candidates: int
    coverage_pct: float
    #: ``[(symbol, first price date)]`` listed after the window start: out.
    late: List[Tuple[str, str]] = field(default_factory=list)
    no_history: List[str] = field(default_factory=list)
    zero_weight: List[str] = field(default_factory=list)
    #: ``{symbol: bars dropped for a close that is not finite or not positive}`` (members with a weight only).
    dropped_bars: Dict[str, int] = field(default_factory=dict)


@dataclass
class VariantResult:
    key: Any                    # HOLD or the months between rebalances
    name: str
    offsets: int
    total_return_pct: float     # mean over offsets
    annualised_pct: float       # mean over offsets
    annualised_min_pct: float
    annualised_max_pct: float
    max_drawdown_pct: float     # mean over offsets; <= 0
    turnover_pct_per_year: float
    rebalances: float           # mean count over offsets
    #: Start offsets at which this variant never rebalanced in the window (it then IS hold there), and whether
    #: offset 0 -- the one the chart draws -- is one of them.
    offsets_without_rebalance: int = 0
    charted_offset_without_rebalance: bool = False
    #: Versus hold (None for hold itself). Drawdown: positive = shallower than hold.
    d_total_pct: Optional[float] = None
    d_annualised_pct: Optional[float] = None
    d_drawdown_pct: Optional[float] = None


@dataclass
class RebalanceTestResult:
    params: RebalanceParams
    window: WindowInfo
    refusal: Optional[str] = None
    variants: List[VariantResult] = field(default_factory=list)
    #: The equity curves (start = 100) at offset 0, keyed like ``VariantResult.key``.
    curve_dates: List[str] = field(default_factory=list)
    curves: Dict[Any, List[float]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# validation and weights
# ---------------------------------------------------------------------------

def _finite(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def validate_params(params: RebalanceParams) -> None:
    if params.lookback_years not in LOOKBACK_YEARS:
        raise RebalanceInputError(f"The lookback must be one of {', '.join(map(str, LOOKBACK_YEARS))} years, "
                                  f"not {params.lookback_years!r}")
    if not _finite(params.cost_bps) or float(params.cost_bps) < 0:
        raise RebalanceInputError(f"The cost must be 0 or more basis points, not {params.cost_bps!r}")
    for name, k in (('winners', params.k_winners), ('losers', params.k_losers)):
        if not _finite(k):
            raise RebalanceInputError(f"The tilt coefficient for {name} must be a number, not {k!r}")


def _clean_weights(weights: Mapping[str, float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for symbol, w in weights.items():
        if not _finite(w) or float(w) < 0:
            raise RebalanceInputError(f"{symbol}: weight {w!r} is not usable (it must be 0 or more)")
        key = str(symbol).strip().upper()
        out[key] = out.get(key, 0.0) + float(w)
    return out


def account_weights(label_targets: Mapping[str, float],
                    label_shares: Mapping[str, Mapping[str, float]]) -> Tuple[Dict[str, float], List[str]]:
    """``({SYMBOL: weight}, skipped labels)`` for the whole account: each symbol at ``label target x its share of the
    label's total share`` (a symbol in two labels adds up). A label with a positive target but no positive member
    share cannot be represented and is returned in ``skipped``; a label at target 0 contributes nothing."""
    out: Dict[str, float] = {}
    skipped: List[str] = []
    for label, target in label_targets.items():
        if not _finite(target) or float(target) <= 0:
            continue
        shares = _clean_weights(label_shares[label] if label in label_shares else {})
        total = sum(shares.values())
        if total <= 0:
            skipped.append(label)
            continue
        for symbol, share in shares.items():
            if share > 0:
                out[symbol] = out.get(symbol, 0.0) + float(target) * share / total
    return out, skipped


def tilt_weights(target: np.ndarray, period_return: np.ndarray, k_winners: float, k_losers: float) -> np.ndarray:
    """The weights a rebalance trades to: ``target_i * clip(1 + k * r_i, TILT_FLOOR, TILT_CAP)``, normalised; ``k`` =
    ``k_winners`` where ``r_i > 0``, ``k_losers`` where ``r_i < 0`` (r = 0 keeps the target)."""
    k = np.where(period_return > 0, k_winners, np.where(period_return < 0, k_losers, 0.0))
    w = target * np.clip(1.0 + k * period_return, TILT_FLOOR, TILT_CAP)
    return w / w.sum()


# ---------------------------------------------------------------------------
# total-return levels
# ---------------------------------------------------------------------------

def _usable_bars(closes: Optional[Mapping[str, Any]], as_of_iso: str) -> Tuple[List[Tuple[str, float]], int]:
    """``([(date, close)] ascending, bars dropped for a close that is not finite or not positive)``; bars after
    ``as_of`` are left out (not counted as dropped). The caller REPORTS the drops."""
    bars, dropped = [], 0
    for day, close in (closes or {}).items():
        day = str(day)[:10]
        if day > as_of_iso:
            continue
        if not _finite(close) or float(close) <= 0:
            dropped += 1
            continue
        bars.append((day, float(close)))
    bars.sort()
    return bars, dropped


def total_return_levels(bars: Sequence[Tuple[str, float]],
                        events: Sequence[Tuple[str, Optional[float]]]) -> Tuple[pd.Series, List[str]]:
    """``(levels, unknown)``: a total-return index on the member's own bar dates (first = its first close) and the bar
    dates on which a distribution of UNKNOWN amount would have been reinvested (counted as 0 in ``levels``; the caller
    refuses when one falls inside the window). A distribution counts on the first bar on or after its ex-date, and
    only when there is a previous bar (one before the first bar is history the window never sees)."""
    dates = [d for d, _ in bars]
    closes = np.array([c for _, c in bars], dtype=float)
    paid = np.zeros(len(bars))
    unknown: List[str] = []
    # Sorted on the DATE ONLY: two rows can share an ex-date, and an amount of None does not compare with a float.
    for ex, amount in sorted(((str(ex)[:10], a) for ex, a in events), key=lambda pair: pair[0]):
        i = int(np.searchsorted(dates, ex, side='left'))
        if i == 0 or i >= len(dates):        # before the first bar / not yet in a close
            continue
        if not _finite(amount):
            unknown.append(dates[i])
            continue
        paid[i] += float(amount)
    rel = np.ones(len(bars))
    rel[1:] = (closes[1:] + paid[1:]) / closes[:-1]
    levels = closes[0] * np.cumprod(rel) if len(bars) else rel
    return pd.Series(levels, index=pd.DatetimeIndex(pd.to_datetime(dates))), unknown


# ---------------------------------------------------------------------------
# the core: one variant on a price frame
# ---------------------------------------------------------------------------

def month_end_mask(index: pd.DatetimeIndex) -> Tuple[np.ndarray, np.ndarray]:
    """``(is_month_end, month_number)`` per row: the last session of a calendar month in the frame (the frame's last
    row is never one: its month is not over) and the month count from the frame's first month (0, 1, ...)."""
    months = np.asarray(index.year * 12 + index.month)
    change = months[1:] != months[:-1]
    is_me = np.r_[change, False]
    month_no = np.cumsum(np.r_[False, change])
    return is_me, month_no


def simulate_variant(prices: pd.DataFrame, target: np.ndarray, months: Optional[int], offset: int = 0, *,
                     cost_bps: float = DEFAULT_COST_BPS, k_winners: float = DEFAULT_K,
                     k_losers: float = DEFAULT_K) -> Tuple[np.ndarray, float, int]:
    """``(equity per row (start 1.0), summed turnover, rebalances)`` of one variant.

    ``prices`` = total-return levels, one column per member, no NaN, rows ascending; ``target`` = weights (sum 1).
    ``months=None`` is HOLD. A rebalance happens at a month-end row whose month number m satisfies
    ``(m - offset) % months == months - 1``."""
    if months is not None and not 0 <= offset < months:
        raise RebalanceInputError(f"offset {offset} is outside 0..{months - 1}")
    px = prices.to_numpy(dtype=float)
    rets = np.vstack([np.zeros((1, px.shape[1])), px[1:] / px[:-1] - 1.0])
    is_me, month_no = month_end_mask(prices.index)
    cost = float(cost_bps) / 10_000.0
    val = np.asarray(target, dtype=float).copy()
    last = val.copy()
    equity = np.empty(len(px))
    turnover = 0.0
    count = 0
    for i in range(len(px)):
        val = val * (1.0 + rets[i])
        if months is not None and is_me[i] and (month_no[i] - offset) % months == months - 1:
            total = val.sum()
            new = total * tilt_weights(target, val / last - 1.0, k_winners, k_losers)
            traded = np.abs(new - val).sum()
            turnover += traded / total
            val = new * (1.0 - cost * traded / total)
            last = val.copy()
            count += 1
        equity[i] = val.sum()
    return equity, turnover, count


def _metrics(index: pd.DatetimeIndex, equity: np.ndarray) -> Tuple[float, float, float, float]:
    """``(total %, annualised %, max drawdown % (<= 0), years)``."""
    years = (index[-1] - index[0]).days / DAYS_PER_YEAR
    total = equity[-1] / equity[0] - 1.0
    car = ((1.0 + total) ** (1.0 / years) - 1.0) if total > -1.0 else -1.0
    dd = float((equity / np.maximum.accumulate(equity) - 1.0).min())
    return total * 100.0, car * 100.0, dd * 100.0, years


def simulate_frame(prices: pd.DataFrame, weights: Mapping[str, float],
                   params: RebalanceParams) -> Tuple[List[VariantResult], Dict[Any, List[float]]]:
    """Every variant on one price frame: ``(rows, curves at offset 0 with start = 100)``. ``weights`` must cover the
    frame's columns (normalised here)."""
    validate_params(params)
    cols = list(prices.columns)
    target = np.array([float(weights[c]) for c in cols], dtype=float)
    if target.sum() <= 0:
        raise RebalanceInputError("The members' weights add to 0")
    target = target / target.sum()
    kw = dict(cost_bps=params.cost_bps, k_winners=params.k_winners, k_losers=params.k_losers)
    curves: Dict[Any, List[float]] = {}
    rows: List[VariantResult] = []
    for key in (HOLD,) + FREQUENCIES:
        months = None if key == HOLD else key
        runs = []
        for offset in range(1 if months is None else months):
            equity, turnover, count = simulate_variant(prices, target, months, offset, **kw)
            total, car, dd, years = _metrics(prices.index, equity)
            runs.append((total, car, dd, turnover / years * 100.0, count))
            if offset == 0:
                curves[key] = [float(v) for v in equity / equity[0] * 100.0]
        rows.append(VariantResult(
            key=key, name=VARIANT_NAMES[key], offsets=len(runs),
            total_return_pct=float(np.mean([r[0] for r in runs])),
            annualised_pct=float(np.mean([r[1] for r in runs])),
            annualised_min_pct=min(r[1] for r in runs), annualised_max_pct=max(r[1] for r in runs),
            max_drawdown_pct=float(np.mean([r[2] for r in runs])),
            turnover_pct_per_year=float(np.mean([r[3] for r in runs])),
            rebalances=float(np.mean([r[4] for r in runs])),
            offsets_without_rebalance=sum(1 for r in runs if r[4] == 0),
            charted_offset_without_rebalance=months is not None and runs[0][4] == 0))
    hold = rows[0]
    for r in rows[1:]:
        r.d_total_pct = r.total_return_pct - hold.total_return_pct
        r.d_annualised_pct = r.annualised_pct - hold.annualised_pct
        r.d_drawdown_pct = r.max_drawdown_pct - hold.max_drawdown_pct
    return rows, curves


# ---------------------------------------------------------------------------
# the whole test: window, frame, variants
# ---------------------------------------------------------------------------

def choose_window_start(first: Mapping[str, str], weights: Mapping[str, float], total_weight: float,
                        lookback_start: str) -> Optional[str]:
    """The window start: ``lookback_start`` when the members priced by then cover ``MIN_COVERAGE`` of
    ``total_weight``, else the earliest later first-price date where they do; None when they never do."""
    def coverage(day: str) -> float:
        return sum(weights[s] for s, d in first.items() if d <= day) / total_weight

    if coverage(lookback_start) >= MIN_COVERAGE - 1e-12:
        return lookback_start
    for day in sorted({d for d in first.values() if d > lookback_start}):
        if coverage(day) >= MIN_COVERAGE - 1e-12:
            return day
    return None


def run_rebalance_test(weights: Mapping[str, float], closes: Mapping[str, Mapping[str, float]],
                       distributions: Mapping[str, DistributionHistory], params: RebalanceParams, *,
                       as_of: date) -> RebalanceTestResult:
    """The rebalance test of one scope. ``weights`` = ``{SYMBOL: weight}`` in any unit (normalised over the members
    in the window). Raises ``RebalanceInputError`` for an unusable parameter or weight; a test that cannot be run on
    the data is returned REFUSED (``refusal`` set, no variants) with the window facts that explain why."""
    validate_params(params)
    clean = _clean_weights(weights)
    as_of_iso = as_of.isoformat()
    lookback_start = horizon_start(f'{params.lookback_years}y', as_of).isoformat()
    upper_closes = {str(k).strip().upper(): v for k, v in (closes or {}).items()}
    upper_dist = {str(k).strip().upper(): v for k, v in (distributions or {}).items()}

    zero = sorted(s for s, w in clean.items() if w <= 0)
    positive = {s: w for s, w in clean.items() if w > 0}
    total_weight = sum(positive.values())
    usable = {s: _usable_bars(upper_closes.get(s), as_of_iso) for s in positive}
    bars_by = {s: bars for s, (bars, _dropped) in usable.items()}
    dropped_bars = {s: n for s, (_bars, n) in sorted(usable.items()) if n}
    no_history = sorted(s for s, b in bars_by.items() if not b)
    first = {s: b[0][0] for s, b in bars_by.items() if b}

    def window(start=None, end=None, years=0.0, members=(), late=()) -> WindowInfo:
        members = list(members)
        covered = sum(positive[s] for s in members)
        return WindowInfo(lookback_start=lookback_start, start=start, end=end, years=years, members=members,
                          weights={s: positive[s] / covered for s in members} if covered > 0 else {},
                          candidates=len(positive),
                          coverage_pct=(covered / total_weight * 100.0) if total_weight > 0 else 0.0,
                          late=list(late), no_history=no_history, zero_weight=zero, dropped_bars=dropped_bars)

    def refuse(message: str, info: WindowInfo) -> RebalanceTestResult:
        return RebalanceTestResult(params=params, window=info, refusal=message, notes=_membership_notes(info))

    if total_weight <= 0:
        return refuse("No member has a weight above 0: there is nothing to rebalance.", window())
    start = choose_window_start(first, positive, total_weight, lookback_start)
    if start is None:
        return refuse(f"The members with a price history never cover {MIN_COVERAGE:.0%} of the weight, so no window "
                      f"can be chosen.", window())
    members = sorted(s for s, d in first.items() if d <= start)
    late = sorted(((s, d) for s, d in first.items() if d > start), key=lambda x: (x[1], x[0]))

    levels: Dict[str, pd.Series] = {}
    unknown_by: Dict[str, List[str]] = {}
    fetch_errors: List[str] = []
    for s in members:
        payouts = upper_dist.get(s)
        if payouts is None or payouts.events is None:
            reason = 'no distribution history was fetched' if payouts is None else \
                ('the distribution history could not be fetched' + (f" ({payouts.error})" if payouts.error else ''))
            fetch_errors.append(f"{s}: {reason}")
            events: Sequence = ()
        else:
            events = payouts.events
        levels[s], unknown_by[s] = total_return_levels(bars_by[s], events)

    frame = pd.DataFrame(levels).sort_index().ffill() if members else pd.DataFrame()
    frame = frame[frame.index >= pd.Timestamp(start)] if members else frame
    if frame.empty:
        return refuse("No session is left in the window.", window(members=members, late=late))
    first_row, last_row = frame.index[0], frame.index[-1]
    years = (last_row - first_row).days / DAYS_PER_YEAR
    info = window(first_row.date().isoformat(), last_row.date().isoformat(), years, members, late)

    if len(members) < MIN_MEMBERS:
        return refuse(f"Only {len(members)} member(s) have prices for the whole window; a rebalance needs at least "
                      f"{MIN_MEMBERS}.", info)
    if (last_row - first_row).days < MIN_WINDOW_DAYS:
        return refuse(f"The window ({info.start} to {info.end}) is shorter than about 6 months "
                      f"({MIN_WINDOW_DAYS} days): too short to compare rebalancing frequencies.", info)
    if fetch_errors:
        return refuse("The distribution history of a member in the window is missing, so its total return (dividends "
                      "reinvested) would be understated: " + '; '.join(fetch_errors) + '. Try again.', info)
    unknown = [f"{s} ({', '.join(d for d in days if d > info.start)})" for s, days in unknown_by.items()
               if any(d > info.start for d in days)]
    if unknown:
        return refuse("A distribution inside the window has no split-adjusted amount, so the total return would be "
                      "understated: " + '; '.join(unknown) + '.', info)

    rows, curves = simulate_frame(frame, info.weights, params)
    notes = _window_notes(info, bars_by, first_row, last_row) + _rebalance_count_notes(rows)
    return RebalanceTestResult(params=params, window=info, variants=rows,
                               curve_dates=[d.date().isoformat() for d in frame.index], curves=curves, notes=notes)


def _membership_notes(info: WindowInfo) -> List[str]:
    """Who is left out and why -- on a refusal as much as on a result."""
    notes: List[str] = []
    if info.late:
        notes.append(f"Listed after the window start, left out: "
                     f"{', '.join(f'{s} (first price {d})' for s, d in info.late)}.")
    if info.no_history:
        notes.append(f"No price history, left out: {', '.join(info.no_history)}.")
    if info.zero_weight:
        notes.append(f"Weight 0, left out: {', '.join(info.zero_weight)}.")
    for s, n in info.dropped_bars.items():
        notes.append(f"{s}: {n} bar(s) with no usable close (not a number, zero or negative) were ignored.")
    return notes


def _rebalance_count_notes(rows: Sequence[VariantResult]) -> List[str]:
    """Say when a frequency never rebalanced, so a figure equal to hold is explained rather than puzzling."""
    notes: List[str] = []
    for v in rows:
        if v.key == HOLD:
            continue
        if v.offsets_without_rebalance == v.offsets:
            notes.append(f"{v.name}: no rebalance date fell inside this window at any start month, so every figure "
                         f"equals hold.")
        elif v.charted_offset_without_rebalance:
            notes.append(f"{v.name}: the charted start (offset 0) has no rebalance date inside this window, so its "
                         f"line is hold's; {v.offsets_without_rebalance} of {v.offsets} start months never "
                         f"rebalance (they are in the table's mean).")
    return notes


def _window_notes(info: WindowInfo, bars_by: Mapping[str, List[Tuple[str, float]]], first_row: pd.Timestamp,
                  last_row: pd.Timestamp) -> List[str]:
    notes = _membership_notes(info)
    for s in info.members:
        dates = [d for d, _ in bars_by[s]]
        at_start = [d for d in dates if d <= first_row.date().isoformat()]
        if at_start and (first_row.date() - date.fromisoformat(at_start[-1])).days > GAP_DAYS:
            notes.append(f"{s}: data gap at the start: its last price before {first_row.date()} is from "
                         f"{at_start[-1]} and is used as the start price.")
        if (last_row.date() - date.fromisoformat(dates[-1])).days > GAP_DAYS:
            notes.append(f"{s}: last price is from {dates[-1]}; it is held flat after that date.")
    return notes


# ---------------------------------------------------------------------------
# display helpers (pure)
# ---------------------------------------------------------------------------

def fmt_pct(value: Optional[float], signed: bool = False) -> str:
    if value is None:
        return UNKNOWN_TEXT
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def fmt_pp(value: Optional[float]) -> str:
    """A difference of two percentages, in percentage points."""
    return '—' if value is None else f"{value:+.2f} pp"


def window_text(w: WindowInfo) -> str:
    """One sentence: the window, how many members are in, and the share of the weight they cover."""
    span = f"{w.start} to {w.end} ({w.years:.1f} years)" if w.start else 'no window'
    return (f"Window {span}; lookback start {w.lookback_start}. {len(w.members)} of {w.candidates} member(s) with a "
            f"weight are in, covering {w.coverage_pct:.1f}% of the weight (re-normalised to 100% over them).")


def table_rows(result: RebalanceTestResult) -> List[Dict[str, Any]]:
    out = []
    for v in result.variants:
        out.append({
            'variant': v.name,
            'total': fmt_pct(v.total_return_pct, signed=True),
            'annualised': fmt_pct(v.annualised_pct, signed=True),
            'range': ('—' if v.offsets == 1
                      else f"{v.annualised_min_pct:+.2f}% .. {v.annualised_max_pct:+.2f}% ({v.offsets} starts)"),
            'drawdown': fmt_pct(v.max_drawdown_pct),
            'turnover': f"{v.turnover_pct_per_year:.0f}%",
            'rebalances': _rebalances_text(v),
            'd_total': fmt_pp(v.d_total_pct),
            'd_annualised': fmt_pp(v.d_annualised_pct),
            'd_drawdown': fmt_pp(v.d_drawdown_pct),
        })
    return out


def _rebalances_text(v: VariantResult) -> str:
    if v.key == HOLD:
        return '0'
    if v.offsets_without_rebalance == v.offsets:
        return 'none in this window (= hold)'
    mean = f"{v.rebalances:.1f}".rstrip('0').rstrip('.')
    if v.offsets_without_rebalance:
        return f"{mean} (none at {v.offsets_without_rebalance} of {v.offsets} starts)"
    return mean


def chart_options(result: RebalanceTestResult) -> Dict[str, Any]:
    """ECharts options: the equity curves (start = 100) of hold and every frequency at offset 0, on ONE axis."""
    series = []
    for key in (HOLD,) + FREQUENCIES:
        if key not in result.curves:
            continue
        color = VARIANT_COLORS[key]
        series.append({'name': VARIANT_NAMES[key], 'type': 'line', 'showSymbol': False,
                       'data': [round(v, 3) for v in result.curves[key]],
                       'lineStyle': {'width': 2, 'color': color, 'type': 'dashed' if key == HOLD else 'solid'},
                       'itemStyle': {'color': color}})
    muted = '#a0aec0'
    return {
        'backgroundColor': 'transparent',
        'animation': False,
        'grid': {'left': 60, 'right': 20, 'top': 40, 'bottom': 40},
        'legend': {'data': [s['name'] for s in series], 'textStyle': {'color': '#e2e8f0'}, 'top': 5},
        'tooltip': {'trigger': 'axis', 'axisPointer': {'type': 'cross'}},
        'xAxis': {'type': 'category', 'data': list(result.curve_dates), 'boundaryGap': False,
                  'axisLabel': {'color': muted}, 'axisLine': {'lineStyle': {'color': '#4a5568'}}},
        'yAxis': {'type': 'value', 'scale': True, 'name': 'Value (start = 100)', 'nameTextStyle': {'color': muted},
                  'axisLabel': {'color': muted}, 'splitLine': {'lineStyle': {'color': 'rgba(255,255,255,0.08)'}}},
        'series': series,
    }
