"""FMPEarningsEvent — rank UPCOMING earnings events (design §9 of
``docs/superpowers/specs/2026-08-31-leaps-grid-design.md``).

What it is
----------
The expert half of the ``O_ERN`` chain. It does NOT time the trade: per design §9
the EXPERT owns the RANKING and the STRATEGY owns the TIMING. This expert surfaces
every symbol whose next earnings print falls inside a fixed look-ahead window
(``earnings_days_look``, a plain setting — NOT a gene), scores how attractive that
event looks, and stamps ``days_to_earnings`` + the raw feature values onto the
recommendation. ``O_ERN``'s searched entry gene (Task 9) reads ``days_to_earnings``
back as an entry condition, and its ``gate_confidence`` gene thresholds the
confidence this expert emits. One timing knob, owned by one side.

Features (design §9 "Genes — emission decided BY the measurements")
------------------------------------------------------------------
* ``hist_move``    — avg |earnings-day % move| over past events. Event dates from
                     ``past_earnings_quarterly`` × our OHLCV closes.
* ``surprise_vol`` — std of past EPS surprise % (reported vs estimated).
* ``vol_cheapness``— hist_move ÷ implied move. **Task 8**; its WEIGHT already exists
                     here, its FEATURE does not yet (see ``_composite`` — an absent
                     feature never demotes, the composite renormalizes over the
                     present ones).

WITHHELD, deliberately not implemented (design §9, recorded not silently dropped)
--------------------------------------------------------------------------------
``w_dispersion`` (high/low estimate spread) and ``w_revision`` (estimate/grade
momentum) are NOT settings on this expert and must not be added on a hunch. The
2026-08-31 cache measurement found only ~3 ``earnings_estimates_quarterly`` rows per
symbol inside the whole 3-year window, and the endpoint looks FORWARD-BIASED (rows
published after the events they'd be scoring), with 1-analyst degeneracy
(high == low) common across mid/small caps. Emitting either weight today produces a
gene that is dead (no data) or lookahead-contaminated (data that did not exist at
decision time) — both of which the GA would happily "optimize".

WHAT UNLOCKS THEM: a point-in-time replay proving the estimate rows a given event
would read were ALREADY PUBLISHED before that event's date (e.g. a dated
publication/`updatedFromDate`-style anchor on the estimates endpoint, or an
independent as-of snapshot of it). Until that replay exists and passes, the two
weights stay off this expert. The same caveat covers any price-target-derived
expected move for mid/small caps, which measured 0–3 targets/quarter — if ever
added it takes the ``min_price_targets_per_quarter``-style guard verbatim.

The earnings-day move convention (pinned; both slots tested)
------------------------------------------------------------
FMP stamps each calendar row with a ``time`` slot. Measured over 120 cached
``past_earnings_quarterly`` files (8,391 rows): ``bmo`` 4,935 / ``amc`` 2,505 /
``'--'`` 663 / missing 288. The slot decides which SESSION reacts to the print, so
the move is not computable without it:

* ``bmo`` (before market open on day D): the market reacts on D itself.
  move = Close(D) / Close(last session before D) - 1.
* ``amc`` (after market close on day D): the market reacts on D+1.
  move = Close(next session after D) / Close(D) - 1.
* ``'--'`` / missing: the reacting session is UNKNOWN. The event is NOT usable —
  it contributes no move and does NOT count toward ``min_hist_events``. Widening
  the window to Close(D-1)→Close(D+1) to "cover both" would systematically inflate
  hist_move for exactly the symbols with the worst data (two sessions of noise
  instead of one), i.e. bias the ranking toward the least-known names. Fail-closed
  beats biased; ~11% of rows carry an unknown slot and symbols average 11–12 events.

The same slot is the confirmation signal for the UPCOMING event: a scheduled row
whose slot is still ``'--'``/missing is a date FMP has not pinned down, and a
slipped date buys volatility for nothing (design §9 ``allow_unconfirmed_dates``).

Normalization (why the weights act on RANKING, not units)
---------------------------------------------------------
Each raw feature is squashed by ``x / (x + k)`` — monotone, bounded to (0, 1),
0 at 0, exactly 0.5 at the documented scale ``k``. The composite is the weighted
MEAN over the features that are PRESENT (``Σ w·n / Σ w``), so:

* every feature lands on the SAME 0–1 axis whatever its unit (percent, percent,
  ratio), which is what makes a weight a ranking knob rather than a unit converter;
* the composite is invariant to a common rescale of all weights (2×w changes
  nothing), so the GA searches weight RATIOS — the only thing that can reorder
  symbols;
* an ABSENT feature drops out of BOTH sums, so it never demotes a symbol
  (applicability-first; the ``_OFF_SCALE`` discipline from the selection stack).

Provider seam / point-in-time discipline
----------------------------------------
Every read goes through ``ProviderBundle`` + ``ba2_providers.cache.cached_get``
(``past_earnings_get`` / ``ohlcv_get``), i.e. the same ``fmp_history_disk_cached``
TTL-frozen + hermetic machinery every other expert uses: a backtest reads the
pre-warmed disk cache (a missing symbol disables that symbol, many abort the run),
live hits the API. No raw HTTP, no direct cache-file reads.

FEATURES ARE COMPUTED STRICTLY FROM EVENTS DATED BEFORE ``as_of``. The upcoming
event is found by asking the calendar for rows up to ``as_of + earnings_days_look``
(the same trick ``DaysToEarningsCondition`` uses — FMP's
``historical_earning_calendar`` also returns already-SCHEDULED future prints), and
that future row contributes ONLY its date and slot. Its ``eps`` is an actual in the
cache file; letting it reach ``surprise_vol`` would be a textbook lookahead leak,
so the past/future split happens once, explicitly, in ``_split_events``.

KNOWN, ACCEPTED LIMIT: the cache file is a present-day snapshot, so a backtest sees
the event date as it was FINALLY recorded, not as it was announced at ``as_of``.
That is inherent to backtesting an earnings CALENDAR and is the same compromise
``DaysToEarningsCondition`` already makes; ``allow_unconfirmed_dates`` is the knob
that lets the GA decide how much of that risk it will take. It touches the DATE
only — never a feature value.
"""

from datetime import datetime, timedelta, timezone
from statistics import stdev
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.backtest_context import BacktestContext, ProviderBundle
from ba2_common.core.db import add_instance, get_db, update_instance
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import AnalysisOutput, ExpertRecommendation, MarketAnalysis
from ba2_common.core.types import (
    MarketAnalysisStatus, OrderRecommendation, Recommendation, RiskLevel, TimeHorizon,
)
from ba2_common.logger import get_expert_logger
from ba2_experts.earnings_surprise import surprise_percent as _surprise_percent
from ba2_experts.expert_mixins import AnalysisStatusRenderMixin
from ba2_providers.cache.cached_get import ohlcv_get, past_earnings_get

#: FMP announcement slots that pin the reacting session. Anything else ('--', None,
#: an unexpected string) is an UNKNOWN slot -- see the module docstring.
_CONFIRMED_SLOTS = ("bmo", "amc")

#: Calendar rows to request. The provider returns rows <= end_date newest-first and
#: truncates to this count, so it must span the upcoming event PLUS enough history to
#: fill _MAX_HIST_EVENTS after the unknown-slot/no-price attrition. 16 quarters = 4y.
_CALENDAR_PERIODS = 16

#: How many of the most recent USABLE past events feed the features. 8 quarters = 2
#: years: long enough to average out one freak print, short enough that the company
#: is still recognisably the same business. Bounded on purpose -- an unbounded history
#: would let a 10-year-old micro-cap move dominate a large-cap's current profile.
_MAX_HIST_EVENTS = 8

#: Daily bars to pull. Must cover the oldest event of _MAX_HIST_EVENTS quarters ago
#: AND the session BEFORE it: 8 quarters ~= 730 calendar days, +170 of slack for
#: irregular reporting gaps and non-trading days.
_OHLCV_LOOKBACK_DAYS = 900

#: Saturating-normalization scales: n(x) = x / (x + k), so n(k) == 0.5 exactly.
#: k is the "typical" magnitude of each feature, chosen so the curve is most
#: DISCRIMINATING (steepest) around ordinary values instead of saturating early:
#:  * hist_move 5.0 %  -- a 5% average absolute earnings-day move is a normal
#:    single-name reaction; 1% scores 0.17, 5% 0.50, 20% 0.80.
#:  * surprise_vol 25.0 % -- EPS surprise percentages are wide and heavy-tailed
#:    (the denominator is the consensus estimate, which can be near zero), so the
#:    half-way point sits far higher than the move's.
#:  * vol_cheapness 1.0 -- a pure ratio (historical move / implied move); 1.0 means
#:    the option is priced exactly at the historical move, which is the natural
#:    midpoint. TASK 8 owns the feature; the scale is pinned here with the others.
_NORM_SCALE = {"hist_move": 5.0, "surprise_vol": 25.0, "vol_cheapness": 1.0}

#: Feature -> its weight setting. The ONLY place the two are tied together, so a new
#: feature cannot be added without a weight (or a weight without a feature) silently.
_FEATURE_WEIGHT_SETTING = {
    "hist_move": "w_hist_move",
    "surprise_vol": "w_surprise_vol",
    # TASK 8 SEAM: the weight is already a setting (and already read below); the
    # FEATURE is produced by Task 8's implied-move leg. Until then 'vol_cheapness'
    # is simply never present in the features dict and _composite renormalizes
    # without it -- inert, never demoting. Task 8 adds the feature, nothing here.
    "vol_cheapness": "w_vol_cheapness",
}


def normalize_feature(name: str, raw: float) -> float:
    """Saturating squash of one raw feature onto (0, 1): ``x / (x + k)``.

    Monotone increasing, 0 at 0, 0.5 at the feature's documented scale ``k``.
    Negative inputs are clamped to 0 (all three features are magnitudes: an average
    ABSOLUTE move, a standard deviation, and a non-negative ratio)."""
    x = max(0.0, float(raw))
    k = _NORM_SCALE[name]
    return x / (x + k)


def composite_confidence(features: Dict[str, float],
                         weights: Dict[str, float]) -> Optional[float]:
    """Weighted mean of the normalized PRESENT features, mapped to confidence 1-100.

    ``features`` is {feature name: raw value} and carries ONLY features that could
    actually be computed. ``weights`` is {feature name: weight}. Returns None when
    nothing weighted is present (no features, or every present feature's weight is
    <= 0) -- the caller turns that into a refusal rather than inventing a score,
    because a "0 out of 0" composite is not a ranking, it is a coin flip.

    Negative weights are clamped to 0: the settings are described as importances,
    and a negative one would invert a feature's meaning behind the operator's back.
    """
    num = 0.0
    den = 0.0
    for name, raw in features.items():
        w = max(0.0, float(weights[name]))
        if w <= 0.0:
            continue
        num += w * normalize_feature(name, raw)
        den += w
    if den <= 0.0:
        return None
    composite = num / den            # in (0, 1)
    return 1.0 + 99.0 * composite    # platform convention: confidence is 1-100


def _parse_day(value) -> Optional[datetime]:
    """A naive UTC-midnight datetime from a 'YYYY-MM-DD' (or ISO) string, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).split("T")[0], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _slot_confirmed(row: Dict[str, Any]) -> bool:
    """True when FMP pinned the announcement slot ('bmo'/'amc'); False for '--'/missing."""
    slot = row.get("time")
    return isinstance(slot, str) and slot.strip().lower() in _CONFIRMED_SLOTS


def earnings_day_move_pct(bars: List[Tuple[str, float]], event_day: str,
                          slot: Optional[str]) -> Optional[float]:
    """Absolute % close-to-close move around one earnings print, or None if not computable.

    ``bars`` is [(YYYY-MM-DD, close)] ascending. See the module docstring for the
    convention; an unknown slot yields None BY DESIGN (not a guessed default), and so
    does a missing bar on either side of the reacting session.
    """
    day = _parse_day(event_day)
    if day is None or not bars:
        return None
    key = day.strftime("%Y-%m-%d")
    s = (slot or "").strip().lower()
    if s == "bmo":
        # Reacting session = the event day itself (or the next session if the event
        # day was not a trading day). 'pre' is the session before it.
        post_i = next((i for i, (d, _) in enumerate(bars) if d >= key), None)
        if post_i is None or post_i == 0:
            return None
        pre_i = post_i - 1
    elif s == "amc":
        # Reacting session = the one AFTER the event day; 'pre' is the event day close.
        pre_i = None
        for i, (d, _) in enumerate(bars):
            if d <= key:
                pre_i = i
            else:
                break
        if pre_i is None or pre_i + 1 >= len(bars):
            return None
        post_i = pre_i + 1
    else:
        return None  # unknown slot -> not usable (module docstring)
    pre_close = bars[pre_i][1]
    post_close = bars[post_i][1]
    if not pre_close or pre_close <= 0 or post_close is None or post_close <= 0:
        return None
    return abs(post_close / pre_close - 1.0) * 100.0


def _split_events(rows: List[Dict[str, Any]], as_of_day: str,
                  look_days: int) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """(next upcoming event within the window, past events newest-first).

    THE POINT-IN-TIME SPLIT. 'Past' is STRICTLY BEFORE ``as_of_day``; everything on or
    after it is future and may contribute its DATE and SLOT only, never an eps figure
    (the cache file holds the eventual actuals for future-dated rows). The upcoming
    event is the EARLIEST row in [as_of_day, as_of_day + look_days] -- the event day
    itself counts as 0 days away, matching DaysToEarningsCondition's inclusive bound.
    """
    horizon = (_parse_day(as_of_day) + timedelta(days=look_days)).strftime("%Y-%m-%d")
    past: List[Dict[str, Any]] = []
    upcoming: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = row.get("report_date") or row.get("fiscal_date_ending")
        d = _parse_day(day)
        if d is None:
            continue
        key = d.strftime("%Y-%m-%d")
        if key < as_of_day:
            past.append(row)
        elif key <= horizon:
            upcoming.append(row)
    past.sort(key=lambda r: str(r.get("report_date") or r.get("fiscal_date_ending")), reverse=True)
    upcoming.sort(key=lambda r: str(r.get("report_date") or r.get("fiscal_date_ending")))
    return (upcoming[0] if upcoming else None), past


def compute_features(past_events: List[Dict[str, Any]],
                     bars: List[Tuple[str, float]]) -> Dict[str, Any]:
    """Raw features + the usable-event count from the PAST events only.

    Returns {'features': {name: raw}, 'usable_events': int, 'moves': [...],
    'surprises': [...]}. ``usable_events`` counts events whose earnings-day move was
    computable (a confirmed slot AND bars on both sides) -- that is the population the
    ``min_hist_events`` floor is about, since an event we cannot price tells us nothing
    about how this name trades its prints.

    ``surprise_vol`` needs at least TWO surprises to have a standard deviation at all;
    with fewer the feature is ABSENT (dropped from the dict), not 0.0 -- a zero would
    read as "this name never surprises", the strongest possible statement, from no data.
    """
    moves: List[float] = []
    surprises: List[float] = []
    for row in past_events[:_MAX_HIST_EVENTS]:
        day = row.get("report_date") or row.get("fiscal_date_ending")
        move = earnings_day_move_pct(bars, day, row.get("time"))
        if move is None:
            continue
        moves.append(move)
        sp = row.get("surprise_percent")
        if sp is None:
            sp = _surprise_percent(row.get("reported_eps"), row.get("estimated_eps"))
        if sp is not None:
            surprises.append(float(sp))

    features: Dict[str, float] = {}
    if moves:
        features["hist_move"] = sum(moves) / len(moves)
    if len(surprises) >= 2:
        features["surprise_vol"] = stdev(surprises)
    return {"features": features, "usable_events": len(moves),
            "moves": moves, "surprises": surprises}


class FMPEarningsEvent(AnalysisStatusRenderMixin, MarketExpertInterface):
    """Ranks the UPCOMING earnings event for one symbol (design §9)."""

    RENDER_PENDING_MESSAGE = 'Earnings-event analysis for {symbol} is queued'
    RENDER_RUNNING_MESSAGE = 'Scoring the next earnings event for {symbol}...'

    @classmethod
    def description(cls) -> str:
        return "Rank upcoming earnings events (historical move, surprise volatility)"

    def __init__(self, id: int):
        super().__init__(id)
        self._load_expert_instance(id)
        self.logger = get_expert_logger("FMPEarningsEvent", id)

    @classmethod
    def get_settings_definitions(cls) -> Dict[str, Any]:
        return {
            "earnings_days_look": {
                "type": "int", "required": True, "default": 10,
                "description": "How many calendar days ahead to look for the next earnings "
                               "event. A plain SETTING, not a gene: per design §9 the expert "
                               "surfaces every event inside this fixed window and the strategy's "
                               "own entry gene ('days_to_earnings <= X') owns the timing. "
                               "Widening this here does not make the strategy enter earlier.",
            },
            "min_hist_events": {
                "type": "int", "required": True, "default": 4,
                "description": "Minimum PAST earnings events with a computable earnings-day move "
                               "before this expert will score a symbol. Below it the features are "
                               "a coin flip, so the symbol gets NO recommendation rather than a "
                               "padded rank. Names with no earnings coverage fail here naturally.",
            },
            "min_analysts": {
                "type": "int", "required": True, "default": 3,
                "description": "Minimum analysts behind the forward EPS estimate. 0 disables the "
                               "gate. With the gate on, a symbol whose analyst count cannot be "
                               "read is REFUSED (fail-closed), because an unread count is not "
                               "evidence of coverage.",
            },
            "allow_unconfirmed_dates": {
                "type": "bool", "required": True, "default": False,
                "description": "Whether to score an upcoming event whose announcement slot FMP "
                               "has not confirmed yet ('--'/missing rather than bmo/amc). Those "
                               "dates slip, and a slipped date buys volatility for nothing. Off "
                               "by default; the grid may search it on.",
            },
            "w_hist_move": {
                "type": "float", "required": True, "default": 1.0,
                "description": "Weight on the average ABSOLUTE earnings-day move over past "
                               "events -- how violently this name usually reacts to a print.",
            },
            "w_surprise_vol": {
                "type": "float", "required": True, "default": 1.0,
                "description": "Weight on the standard deviation of past EPS surprises -- how "
                               "unpredictable this name's results are versus consensus.",
            },
            "w_vol_cheapness": {
                "type": "float", "required": True, "default": 1.0,
                "description": "Weight on historical move divided by the option-implied move -- "
                               "what you GET versus what you PAY. The implied leg is Task 8 of "
                               "the options-grid2 plan; until it ships the feature is absent for "
                               "every symbol and this weight is inert (an absent feature is "
                               "renormalized out, it never demotes a symbol).",
            },
            # DELIBERATELY ABSENT: w_dispersion, w_revision. See the module docstring
            # -- design §9 withheld them on measured coverage (~3 in-window estimate
            # rows/symbol, forward-biased endpoint, 1-analyst degeneracy) and they are
            # unlocked only by a point-in-time replay proving the estimate rows predate
            # the events they would score.
        }

    _SETTING_KEYS = ("earnings_days_look", "min_hist_events", "min_analysts",
                     "allow_unconfirmed_dates", "w_hist_move", "w_surprise_vol",
                     "w_vol_cheapness")

    # ------------------------------------------------------------------
    # Backtest contract: _gather (provider I/O) + _process (pure).
    # ------------------------------------------------------------------
    def _gather(self, providers: ProviderBundle, as_of: Optional[datetime]) -> Dict[str, Any]:
        symbol = self._gather_symbol
        now = as_of or datetime.now(timezone.utc)
        look_days = int(self._gather_earnings_days_look)

        # ONE calendar read covering [oldest history .. now + look_days]: the provider
        # filters to rows <= end_date and returns the newest _CALENDAR_PERIODS of them,
        # so asking to the HORIZON is what makes the already-scheduled upcoming print
        # visible at all (the same trick DaysToEarningsCondition uses). The past/future
        # split is done in _process via _split_events -- see the module docstring.
        horizon = now + timedelta(days=look_days)
        earnings = past_earnings_get(
            providers.fundamentals_details(), symbol, as_of=horizon,
            frequency="quarterly", lookback_periods=_CALENDAR_PERIODS, format_type="dict")
        rows = earnings.get("earnings") or [] if isinstance(earnings, dict) else []

        # Daily closes up to `now` ONLY -- never to the horizon. The moves are historical;
        # a bar dated after the decision date has no business being in this window.
        df = ohlcv_get(providers.ohlcv(), symbol, as_of=now, lookback=_OHLCV_LOOKBACK_DAYS)
        bars: List[Tuple[str, float]] = []
        if df is not None and not getattr(df, "empty", True) and "Close" in df:
            for day, close in zip(df["Date"], df["Close"]):
                if close is None or close != close:      # NaN-safe
                    continue
                bars.append((str(day)[:10], float(close)))
            bars.sort(key=lambda t: t[0])

        # Analyst count: only fetched when the gate is armed (min_analysts > 0), mirroring
        # FMPEarningsDrift's opt-in estimator fetch -- no extra disk-cache pressure for a
        # configuration that would ignore the answer.
        analyst_count = None
        if int(self._gather_min_analysts) > 0:
            estimates = providers.fundamentals_details().get_earnings_estimates(
                symbol, frequency="quarterly", as_of_date=now,
                lookback_periods=1, format_type="dict")
            rows_e = estimates.get("estimates") or [] if isinstance(estimates, dict) else []
            if rows_e:
                count = rows_e[0].get("number_of_analysts")
                # 0 is FMP's "no count published", not a real zero-analyst estimate.
                analyst_count = int(count) if count else None

        current_price = (self._get_current_price(symbol) if as_of is None
                         else providers.price_at_date(symbol, as_of))
        return {"symbol": symbol, "earnings_rows": rows, "bars": bars,
                "analyst_count": analyst_count, "current_price": current_price}

    def _process(self, data_bundle: Dict[str, Any], settings: Dict[str, Any],
                 as_of: Optional[datetime] = None) -> Recommendation:
        """PURE ranking. Every refusal is a first-class SKIP with a reason, never a
        padded score: this expert exists to RANK events, and a symbol it cannot rank
        must leave the ranking rather than enter it at an invented value."""
        now = as_of or datetime.now(timezone.utc)
        as_of_day = now.strftime("%Y-%m-%d")
        symbol = data_bundle["symbol"]
        current_price = data_bundle["current_price"]
        look_days = int(settings["earnings_days_look"])
        min_hist_events = int(settings["min_hist_events"])
        min_analysts = int(settings["min_analysts"])
        allow_unconfirmed = bool(settings["allow_unconfirmed_dates"])

        def _skip(reason: str) -> Recommendation:
            self._log_skip(symbol, reason)
            return Recommendation(
                signal=OrderRecommendation.HOLD, confidence=0.0,
                current_price=current_price if current_price is not None else 0.0,
                details=f"No earnings-event ranking for {symbol}: {reason}",
                expected_profit_percent=0.0, skip=True, skip_reason=reason)

        upcoming, past_events = _split_events(
            data_bundle["earnings_rows"], as_of_day, look_days)
        if upcoming is None:
            return _skip(f"no earnings event within {look_days} days")

        event_day = str(upcoming.get("report_date") or upcoming.get("fiscal_date_ending"))
        confirmed = _slot_confirmed(upcoming)
        if not confirmed and not allow_unconfirmed:
            # THE SLIPPED-DATE TRAP: an unpinned slot is an unpinned DATE.
            return _skip(f"earnings date {event_day} is unconfirmed "
                         f"(slot {upcoming.get('time')!r}) and allow_unconfirmed_dates is off")

        # Fail-closed analyst gate. An absent count is not a small count and not a large
        # one -- with the gate armed it is a refusal, logged, so a coverage hole can never
        # masquerade as a qualifying symbol.
        analyst_count = data_bundle["analyst_count"]
        if min_analysts > 0:
            if analyst_count is None:
                return _skip(f"analyst count unavailable and min_analysts={min_analysts} "
                             f"(fail-closed)")
            if analyst_count < min_analysts:
                return _skip(f"only {analyst_count} analysts < min_analysts={min_analysts}")

        computed = compute_features(past_events, data_bundle["bars"])
        if computed["usable_events"] < min_hist_events:
            return _skip(f"only {computed['usable_events']} usable past events "
                         f"< min_hist_events={min_hist_events}")

        features = computed["features"]
        weights = {name: float(settings[key])
                   for name, key in _FEATURE_WEIGHT_SETTING.items()}
        confidence = composite_confidence(features, weights)
        if confidence is None:
            return _skip("no weighted feature available (every present feature's weight is 0)")

        days_to_earnings = (_parse_day(event_day) - _parse_day(as_of_day)).days
        payload = {
            "days_to_earnings": days_to_earnings,
            "event_date": event_day,
            "event_time": upcoming.get("time"),
            "event_time_confirmed": confirmed,
            "usable_events": computed["usable_events"],
            # Raw (un-normalized) feature values, so a reader/condition sees the real
            # units. Absent features are absent here too -- never stamped as 0.
            "hist_move": features.get("hist_move"),
            "surprise_vol": features.get("surprise_vol"),
            "vol_cheapness": features.get("vol_cheapness"),   # Task 8
        }
        details = (
            f"Upcoming earnings for {symbol} on {event_day} "
            f"({days_to_earnings}d away, slot {upcoming.get('time')!r}, "
            f"{'confirmed' if confirmed else 'UNCONFIRMED'})\n"
            f"Usable past events: {computed['usable_events']}\n"
            f"Avg |earnings-day move|: "
            f"{('%.2f%%' % features['hist_move']) if 'hist_move' in features else 'n/a'}\n"
            f"EPS surprise volatility: "
            f"{('%.2f%%' % features['surprise_vol']) if 'surprise_vol' in features else 'n/a'}\n"
            f"Confidence: {confidence:.1f}%\n")
        return Recommendation(
            signal=OrderRecommendation.BUY, confidence=round(confidence, 1),
            current_price=current_price, details=details, expected_profit_percent=0.0,
            raw_outputs={
                "name": "Earnings Event Analysis", "type": "earnings_event_analysis",
                "text": details,
                # Nested under the expert name so the SAME key path works live (where
                # run_analysis stamps ExpertRecommendation.data itself) and in backtest
                # (where the engine copies raw_outputs wholesale into .data). Task 9's
                # days_to_earnings condition reads exactly this path.
                "FMPEarningsEvent": payload,
            })

    def _log_skip(self, symbol: str, reason: str) -> None:
        """Every refusal is logged: a silently-dropped symbol is indistinguishable from
        a symbol that was never considered."""
        logger = getattr(self, "logger", None)
        if logger is not None:
            logger.info(f"FMPEarningsEvent skipping {symbol}: {reason}")

    def analyze_as_of(self, as_of: Optional[datetime], context: BacktestContext) -> Recommendation:
        """BacktestInterface entry: the SAME _gather+_process the live path runs.

        _gather reads _gather_symbol/_gather_earnings_days_look/_gather_min_analysts,
        which the live orchestrator sets; sourced from the context here too (same
        pattern as FMPEarningsDrift/FMPRating)."""
        self._gather_symbol = context.extra.get("symbol", getattr(self, "_gather_symbol", None))
        self._gather_earnings_days_look = int(context.settings["earnings_days_look"])
        self._gather_min_analysts = int(context.settings["min_analysts"])
        bundle = self._gather(context.providers, as_of)
        return self._process(bundle, context.settings, as_of)

    # ------------------------------------------------------------------
    def run_analysis(self, symbol: str, market_analysis: MarketAnalysis) -> None:
        """Live orchestrator: resolve settings -> _gather(as_of=None) -> _process ->
        persist. Runs the EXACT same _gather/_process the backtest drives."""
        self.logger.info(f"Starting earnings-event analysis for {symbol} "
                         f"(Analysis ID: {market_analysis.id})")
        try:
            market_analysis.status = MarketAnalysisStatus.RUNNING
            update_instance(market_analysis)

            settings = self._resolve_settings(self._SETTING_KEYS)
            self._gather_symbol = symbol
            self._gather_earnings_days_look = int(settings["earnings_days_look"])
            self._gather_min_analysts = int(settings["min_analysts"])
            bundle = self._gather(self._live_providers(), as_of=None)
            if not bundle["current_price"]:
                raise ValueError(f"Unable to get current price for {symbol}")
            rec = self._process(bundle, settings, as_of=None)

            recommendation_id = None
            if not rec.skip:
                recommendation_id = add_instance(ExpertRecommendation(
                    instance_id=self.id,
                    symbol=symbol,
                    recommended_action=rec.signal,
                    expected_profit_percent=rec.expected_profit_percent,
                    price_at_date=rec.current_price,
                    details=rec.details,
                    confidence=round(rec.confidence, 1),
                    risk_level=RiskLevel.MEDIUM,
                    time_horizon=TimeHorizon.SHORT_TERM,
                    market_analysis_id=market_analysis.id,
                    # Same key path the backtest engine produces from raw_outputs.
                    data={"FMPEarningsEvent": rec.raw_outputs["FMPEarningsEvent"]},
                    created_at=datetime.now(timezone.utc),
                ))

                session = get_db()
                try:
                    session.add(AnalysisOutput(
                        market_analysis_id=market_analysis.id,
                        name=rec.raw_outputs["name"],
                        type=rec.raw_outputs["type"],
                        text=rec.raw_outputs["text"],
                    ))
                    session.commit()
                finally:
                    session.close()

            market_analysis.state = {
                "earnings_event": {
                    "skipped": rec.skip,
                    "skip_reason": rec.skip_reason,
                    "recommendation": None if rec.skip else {
                        "signal": rec.signal.value,
                        "confidence": rec.confidence,
                    },
                    "event": None if rec.skip else rec.raw_outputs["FMPEarningsEvent"],
                    "expert_recommendation_id": recommendation_id,
                    "current_price": rec.current_price,
                    "analysis_timestamp": datetime.now(timezone.utc).isoformat(),
                }
            }
            market_analysis.status = (MarketAnalysisStatus.SKIPPED if rec.skip
                                      else MarketAnalysisStatus.COMPLETED)
            update_instance(market_analysis)
            self.logger.info(
                f"Completed earnings-event analysis for {symbol}: "
                f"{'SKIPPED (' + str(rec.skip_reason) + ')' if rec.skip else rec.signal.value}")

        except Exception as e:
            self.logger.error(f"Earnings-event analysis failed for {symbol}: {e}", exc_info=True)
            market_analysis.state = {
                "error": str(e),
                "error_timestamp": datetime.now(timezone.utc).isoformat(),
                "analysis_failed": True,
            }
            market_analysis.status = MarketAnalysisStatus.FAILED
            update_instance(market_analysis)
            raise

    # ------------------------------------------------------------------
    def _render_completed(self, market_analysis: MarketAnalysis) -> None:
        from nicegui import ui
        state = (market_analysis.state or {}).get("earnings_event")
        if not state:
            with ui.card().classes('w-full p-4'):
                ui.label('No analysis data available').classes('text-grey-7')
            return
        with ui.card().classes('w-full p-4'):
            ui.label(f"Earnings Event Analysis - {market_analysis.symbol}").classes('text-h6')
            if state.get("skipped"):
                ui.label(f"Skipped: {state.get('skip_reason')}").classes('text-grey-8 mt-2')
                return
            ev = state.get("event") or {}
            rec = state.get("recommendation") or {}
            with ui.row().classes('gap-8 mt-2'):
                ui.label(f"Signal: {rec.get('signal', 'N/A')}").classes('text-h6')
                ui.label(f"Confidence: {rec.get('confidence', 0):.1f}%")
                ui.label(f"Event: {ev.get('event_date')} ({ev.get('days_to_earnings')}d)")
            ui.label(f"Avg |move|: {ev.get('hist_move')} | "
                     f"Surprise vol: {ev.get('surprise_vol')} | "
                     f"Usable events: {ev.get('usable_events')}").classes('text-grey-8 mt-2')
