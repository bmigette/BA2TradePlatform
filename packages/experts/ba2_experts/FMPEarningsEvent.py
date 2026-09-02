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
* ``vol_cheapness``— hist_move ÷ implied move (Task 8). Implied move is the ATM
                     straddle's mid-price sum divided by spot, ×100, read from an
                     option chain via a DUCK-TYPED seam: whatever object offers
                     ``get_option_chain(underlying, expiry_min, expiry_max, ...)`` on
                     the same shape ``OptionsAccountInterface`` declares answers it —
                     ``context.account`` (already as-of-scoped) in backtest, the live
                     account resolved through ``ba2_common.core.instance_resolver`` in
                     ``run_analysis``. No capability, no expiry with runway, no strike
                     quoted on both legs, or a non-positive mid → the feature is
                     ABSENT, never 0 (see ``_fetch_implied_leg``/``implied_move_pct`` —
                     an absent feature never demotes, ``composite_confidence``
                     renormalizes over the present ones).

HOW SEVERE IS THE DEFAULT ``min_analysts=3``? Measured 2026-09-01 over a random 600
of the 4,728 cached ``earnings_estimates_quarterly`` payloads, replaying this
expert's own gate (nearest estimate row on/after the as-of, plural analyst key,
0 read as "no count"): it REFUSES 66% of symbols at a 2023-06-10 as-of and 47% at
2025-06-10. Coverage thins going back because the endpoint is forward-biased. So the
default is deliberately strict, not accidentally so — it discards roughly half the
universe at recent dates and two thirds at older ones. Task 10 should pick the gene
range with those two numbers in front of it: a range that never reaches low values
would leave most of the mid/small bands unreachable, which is exactly where design
§9 says O_ERN runs FIRST.

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
Every read goes through ``ProviderBundle``. The calendar and the price history use
the ``ba2_providers.cache.cached_get`` alias layer (``past_earnings_get`` /
``ohlcv_get``); the analyst count calls ``get_earnings_estimates`` on the same
bundle-resolved provider directly, because that category has no alias in
``cached_get``. All three land on the SAME ``fmp_history_disk_cached`` TTL-frozen +
hermetic machinery every other expert uses: a backtest reads the pre-warmed disk
cache (a missing symbol disables that symbol, many abort the run), live hits the
API. No raw HTTP, no direct cache-file reads.

THE ONE EXCEPTION IS THE IMPLIED LEG (Task 8). Options are not a ``ProviderBundle``
category, so ``vol_cheapness`` reads the chain through the ACCOUNT instead:
``context.account`` in backtest (a ``BacktestAccount``, already scoped to the run's
as-of date by whoever built it) and, in ``run_analysis``, the account resolved for
THIS expert instance via
``ba2_common.core.instance_resolver.get_instance_resolver().get_account_instance``
— the same seam ``_get_current_price`` already uses. Both answer the same
``get_option_chain(underlying, expiry_min, expiry_max, ...)`` shape
``OptionsAccountInterface`` declares, so the expert never has to know which runtime
it is in: DUCK-TYPED, any object offering that method works, and one that does not
(a stock-only account, an unwired resolver, ``account=None``) simply means the
feature is absent, logged once (``_log_no_chain_capability_once``). The expert reads
the chain's own marks (``OptionContract.mid``; bid==ask==close in the backtest
store) and NEVER the engine's Black-Scholes mark fallback (Task 3) — that facility
exists to price a STRUCTURE the platform is about to TRADE when the store has no
quote; this expert only RANKS, and a rank built on a synthesized price would be
scoring its own guess. No OCC parsing anywhere: only ``OptionContract``'s typed
fields (``strike``/``expiry``/``option_type``/``mid``).

POINT-IN-TIME, TWO DIFFERENT DATES. ``expiry_min`` for the chain query is the EVENT
date (a contract expiring before it has no runway through the print), but the CHAIN
ITSELF — which contracts exist, what they are marked at — is read at the DECISION
date (``context.account``'s own as-of clock in backtest; "now" live), and the spot
the straddle is divided by is ``current_price``, the same as-of close every other
feature in this file uses. Nothing here ever reads a chain or a price AT the event
date.

FEATURES ARE COMPUTED STRICTLY FROM EVENTS DATED BEFORE ``as_of``. The upcoming
event is found by asking the calendar for rows up to ``as_of + earnings_days_look``
(the same trick ``DaysToEarningsCondition`` uses — FMP's
``historical_earning_calendar`` also returns already-SCHEDULED future prints), and
that future row contributes ONLY its date and slot. Its ``eps`` is an actual in the
cache file; letting it reach ``surprise_vol`` would be a textbook lookahead leak,
so the past/future split is done EXPLICITLY, in ``_split_events`` -- never inferred
from field order or a raw date compare.

``_split_events`` is in fact called TWICE per analysis (``_process`` for the ranking
decision, ``_gather`` for Task 8's implied-leg date), on the SAME ``rows``/
``look_days``. In backtest both calls share one caller-supplied ``as_of``, so they
always agree. LIVE CAVEAT: both call sites pass ``as_of=None`` and each
independently resolves ``now = datetime.now(timezone.utc)``, so the two calls could
in principle straddle a UTC-midnight boundary and read a different ``as_of_day``
string -- a single-digit-millisecond window, and a live analysis run sits nowhere
near UTC midnight (that's the middle of the US trading day), so this is a real but
negligible edge, not a practical concern.

THE MOST RECENT PAST EVENT reads the as-of close. An 'amc' print dated as_of-1
reacts during the as-of session, so its post-close IS ``Close(as_of)`` -- the very
bar every expert on this platform already prices its decisions at. That is
platform-consistent, not a leak: the as-of close is the decision-time information
set, and refusing it here would make this expert's history end a day earlier than
everyone else's for no gain. LIVE CAVEAT: intraday, that as-of "close" is a PARTIAL
bar (whatever the provider last stamped), so a same-session amc move can move as the
day goes on -- it settles at the real close and matters only for the single newest
observation of an eight-event average.

KNOWN, ACCEPTED LIMIT: the cache file is a present-day snapshot, so a backtest sees
the event date as it was FINALLY recorded, not as it was announced at ``as_of``.
That is inherent to backtesting an earnings CALENDAR and is the same compromise
``DaysToEarningsCondition`` already makes; ``allow_unconfirmed_dates`` is the knob
that lets the GA decide how much of that risk it will take. It touches the DATE
only — never a feature value.
"""

import math
from datetime import date, datetime, timedelta, timezone
from statistics import stdev
from typing import Any, Dict, List, Optional, Tuple

from ba2_common.core.backtest_context import BacktestContext, ProviderBundle
from ba2_common.core.db import add_instance, get_db, update_instance
# THE STAMP CONTRACT, IMPORTED RATHER THAN RE-SPELLED (review 2026-09-01, finding 1).
# This module is the WRITER; ba2_common.core.TradeConditions' two O_ERN timing gates are
# the READERS. When both sides spelled the key path by hand, a rename on either side
# desynced them SILENTLY -- every suite green, both gates permanently unevaluable, which
# is exactly the dead-gene failure the whitelist note in TradeActions warns about. One
# definition now, and test_fmp_earnings_event.py pins the emitted payload's keys against
# it so a future hand-spelled key fails loudly. (experts -> common is contract-legal;
# see packages/experts/.importlinter.)
from ba2_common.core.earnings_stamp import (
    DAYS_TO_EARNINGS_KEY,
    EARNINGS_STAMP_NAMESPACE,
    EVENT_DATE_KEY,
)
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import AnalysisOutput, ExpertRecommendation, MarketAnalysis
from ba2_common.core.types import (
    MarketAnalysisStatus, OptionRight, OrderRecommendation, Recommendation, RiskLevel,
    TimeHorizon,
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

#: Maximum calendar-day span the reacting PAIR of closes may straddle. A normal pair
#: is consecutive sessions (1 day), 3 across a weekend, up to ~5 across a holiday
#: weekend. Anything wider is a HOLE in the series -- a halt, a quotation gap, an
#: illiquid stretch with no prints -- and attributing that whole gap's drift to the
#: earnings announcement would credit the print with days of unrelated movement.
#: 7 admits every real weekend/holiday combination and excludes the holes.
_MAX_REACTION_GAP_DAYS = 7

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
    # Task 8's implied-move leg (_fetch_implied_leg / implied_move_pct) supplies this
    # when the chain seam answers; whenever it cannot (no capability, no expiry with
    # runway, no two-sided ATM quote) 'vol_cheapness' is simply never added to the
    # features dict and _composite renormalizes without it -- inert, never demoting.
    "vol_cheapness": "w_vol_cheapness",
}

#: Calendar-day span PAST the event to search for an expiry that has not yet happened
#: by the time the print lands ("runway"). Standard monthly option cycles are listed
#: roughly every 30-35 calendar days, so a name whose only cycle is monthly still has
#: one inside this window even from the worst-case point right after a monthly
#: expiry; a weekly-enabled name has several. This is a SEARCH window only -- the
#: selected expiry is still the EARLIEST one found at or after the event date (see
#: ``_nearest_expiry_with_runway``), never just "whatever is soonest after this many
#: days".
_IMPLIED_LEG_EXPIRY_WINDOW_DAYS = 45


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
    does a missing bar on either side of the reacting session, or a reacting pair whose
    two closes sit more than ``_MAX_REACTION_GAP_DAYS`` apart (an interior hole in the
    series, whose whole drift is not this announcement's doing).
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
    pre_day, pre_close = bars[pre_i]
    post_day, post_close = bars[post_i]
    # INTERIOR-GAP GUARD. 'The session before' is only meaningful if it really was the
    # session before: with a hole in the series the neighbouring bar can be weeks away,
    # and the whole gap's drift would be booked as the earnings reaction.
    gap = _parse_day(post_day) - _parse_day(pre_day)
    if gap.days > _MAX_REACTION_GAP_DAYS:
        return None
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
    computable (a confirmed slot, bars on both sides, no interior gap) -- that is the
    population the ``min_hist_events`` floor is about, since an event we cannot price
    tells us nothing about how this name trades its prints.

    THE WINDOW IS FILLED WITH USABLE EVENTS, not with the newest rows. ``past_events``
    is walked newest-first and the first ``_MAX_HIST_EVENTS`` that survive attrition are
    kept, so a name whose recent prints are unslotted still gets a full history from the
    older ones instead of being starved by them. This is what the deliberately generous
    ``_CALENDAR_PERIODS`` fetch is FOR.

    ``surprise_vol`` IS DELIBERATELY COUPLED to that same usable set: only an event that
    contributed a move contributes a surprise. The two features then describe ONE event
    population, so a symbol cannot report a move profile from 4 prints and a surprise
    profile from 12 -- and the ``min_hist_events`` floor governs both. It needs at least
    TWO surprises to have a standard deviation at all; with fewer the feature is ABSENT
    (dropped from the dict), not 0.0 -- a zero would read as "this name never
    surprises", the strongest possible statement, from no data.
    """
    moves: List[float] = []
    surprises: List[float] = []
    for row in past_events:
        if len(moves) >= _MAX_HIST_EVENTS:
            break
        day = row.get("report_date") or row.get("fiscal_date_ending")
        move = earnings_day_move_pct(bars, day, row.get("time"))
        if move is None:
            continue
        moves.append(move)
        sp = row.get("surprise_percent")
        if sp is None:
            reported, estimated = row.get("reported_eps"), row.get("estimated_eps")
            # THE COERCED-ZERO TRAP. The provider maps a MISSING eps leg to 0.0 and, on
            # exactly those rows, refuses to state a surprise (surprise_percent=None).
            # Recomputing from the coerced legs turns that refusal into a fabricated
            # -100% "total miss" -- and a scheduled-but-unreported quarter is the single
            # most common way a leg goes missing. A real 0.0 print is indistinguishable
            # downstream, so the row contributes NO surprise. Same guard, same reason as
            # ba2_experts.earnings_surprise.surprise_history.
            sp = (_surprise_percent(reported, estimated)
                  if (reported and estimated) else None)
        if sp is not None and math.isfinite(float(sp)):
            surprises.append(float(sp))

    features: Dict[str, float] = {}
    if moves:
        features["hist_move"] = sum(moves) / len(moves)
    if len(surprises) >= 2:
        features["surprise_vol"] = stdev(surprises)
    return {"features": features, "usable_events": len(moves),
            "moves": moves, "surprises": surprises}


# ---------------------------------------------------------------------------------
# Task 8: the implied-move leg. Three pure functions -- expiry selection, ATM-strike
# straddle selection, and the final ratio -- kept separate from the chain FETCH
# (``FMPEarningsEvent._fetch_implied_leg``, provider I/O) so each is directly
# testable against canned ``OptionContract`` rows with no account/context in play.
# ---------------------------------------------------------------------------------
def _nearest_expiry_with_runway(contracts: List[Any], event_date: date) -> Optional[date]:
    """The EARLIEST expiry at/after ``event_date`` among ``contracts``, or None.

    Filters defensively even though the caller already bounds the chain query to
    ``expiry_min=event_date``: a chain provider (or a test double) is free to return
    rows outside the filter it was asked for, and an expiry BEFORE the event has no
    runway -- the contract would already be dead by the time the print it is meant
    to price actually happens, so picking it would price a straddle on nothing.
    """
    expiries = {c.expiry for c in contracts
               if getattr(c, "expiry", None) is not None and c.expiry >= event_date}
    return min(expiries) if expiries else None


def _atm_straddle_mids(contracts: List[Any], expiry: date,
                       spot: float) -> Optional[Dict[str, Any]]:
    """{'call_mid', 'put_mid', 'strike', 'expiry'} for the ATM straddle at ``expiry``,
    or None when it cannot be honestly priced.

    ATM = the strike NEAREST ``spot`` that has a usable (finite, positive) mid on
    BOTH the call and the put at ``expiry`` -- a straddle needs both legs, and a
    strike quoted on only one side cannot price one. Reads only the chain objects'
    own typed fields (``strike``/``option_type``/``mid``); no OCC parsing, no
    Black-Scholes fallback (module docstring) -- a missing/zero/negative mid on
    either leg is a missing leg, not a leg priced at 0.
    """
    calls: Dict[float, float] = {}
    puts: Dict[float, float] = {}
    for c in contracts:
        if getattr(c, "expiry", None) != expiry:
            continue
        mid = c.mid
        if mid is None or mid <= 0:
            continue
        if c.option_type == OptionRight.CALL:
            calls[c.strike] = mid
        elif c.option_type == OptionRight.PUT:
            puts[c.strike] = mid
    common_strikes = set(calls) & set(puts)
    if not common_strikes:
        return None
    strike = min(common_strikes, key=lambda s: abs(s - spot))
    return {"call_mid": calls[strike], "put_mid": puts[strike],
            "strike": strike, "expiry": expiry}


def implied_move_pct(implied_leg: Optional[Dict[str, Any]],
                     spot: Optional[float]) -> Optional[float]:
    """ATM straddle mid-price sum ÷ spot × 100, or None when either input is unusable.

    ``implied_leg`` is the dict ``_atm_straddle_mids`` returns (or None). ``spot`` is
    ``current_price`` -- the SAME as-of close every other feature in this module is
    computed against (design §9: "implied = ATM straddle price ÷ spot from the
    options store at decision date"). A non-positive spot or a non-positive straddle
    sum is unusable and returns None, never a divide-by-zero or a fabricated value.
    """
    if implied_leg is None or spot is None or spot <= 0:
        return None
    straddle = implied_leg["call_mid"] + implied_leg["put_mid"]
    if straddle <= 0:
        return None
    return straddle / spot * 100.0


class FMPEarningsEvent(AnalysisStatusRenderMixin, MarketExpertInterface):
    """Ranks the UPCOMING earnings event for one symbol (design §9)."""

    #: Trading bars of price history this expert needs warmed up before the FIRST
    #: analysis bar. Derived, not guessed: ``_OHLCV_LOOKBACK_DAYS`` (900 calendar days,
    #: itself sized to reach the oldest of _MAX_HIST_EVENTS=8 quarterly prints plus its
    #: reference session) must be COVERED by the warmup the handler derives, and
    #: ``daily_backtest_handler.derive_warmup_days`` converts bars -> calendar days as
    #: ``int(bars * 1.45) + 10``. Solving for 900: (900 - 10) / 1.45 = 613.8 bars, so
    #: 620 -> int(620*1.45)+10 = 909 calendar days, covering the window with margin.
    #: Without this the earliest bars of a run silently see fewer usable events and get
    #: refused by min_hist_events -- a crippled universe that looks like a quiet expert.
    #:
    #: TASK 10 STILL OWES THE REGISTRATION. ``derive_warmup_days`` only consults this
    #: attribute for classes listed in ``_SUPPORTED_EXPERTS``; until Task 10 adds
    #: "FMPEarningsEvent" there (and, optionally, to the ``_EXPERT_WARMUP_BARS`` table)
    #: this number is inert and the handler falls back to the 20-bar default.
    BACKTEST_WARMUP_BARS: int = 620

    RENDER_PENDING_MESSAGE = 'Earnings-event analysis for {symbol} is queued'
    RENDER_RUNNING_MESSAGE = 'Scoring the next earnings event for {symbol}...'

    @classmethod
    def description(cls) -> str:
        return "Rank upcoming earnings events (historical move, surprise volatility)"

    def __init__(self, id: int):
        super().__init__(id)
        self._load_expert_instance(id)
        self.logger = get_expert_logger("FMPEarningsEvent", id)
        # Task 8: whether "no option-chain capability" has already been logged once
        # for this instance -- see _log_no_chain_capability_once.
        self._chain_capability_warned = False

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
                # DEFAULT LOWERED 3 -> 1 (Task 11, 2026-09-02). Measured 2026-09-01 (see the
                # module docstring, "HOW SEVERE IS THE DEFAULT min_analysts=3?"): replaying this
                # expert's own gate over a random 600 of 4,728 cached earnings_estimates_quarterly
                # payloads, the OLD default=3 refused 66% of the universe at a 2023-06-10 as-of and
                # 47% at 2025-06-10 -- decaying strictness that tilts the traded universe across the
                # window. 1 is a DATA-QUALITY floor (some estimate exists at all), not a selection
                # filter; the grid's own min_analysts gene (0-5, 0 = gate off) is what searches the
                # selection question.
                "type": "int", "required": True, "default": 1,
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
                               "what you GET versus what you PAY. The implied leg reads an ATM "
                               "straddle from an option chain via a duck-typed seam (backtest: "
                               "the run's account; live: the account resolved for this expert "
                               "instance); a deployment with no options-capable account never "
                               "computes the feature, and the weight stays inert for it (an "
                               "absent feature is renormalized out, it never demotes a symbol).",
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
        as_of_day = now.strftime("%Y-%m-%d")
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

        # RECOMPUTED here (a second, cheap, pure call -- _process does its own for the
        # skip logic) ONLY to learn whether there is an upcoming event and, if so, its
        # date: that is what the Task 8 implied-move leg needs to know whether/where to
        # read a chain. _gather stays provider-I/O-only; this is not decision logic.
        upcoming, _past_ignored = _split_events(rows, as_of_day, look_days)

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

        # Task 8: the implied-move leg. ONE chain read, only for a symbol that HAS an
        # upcoming event -- see _fetch_implied_leg for the duck-typed seam and every
        # fail-to-absent path.
        implied_leg = self._fetch_implied_leg(upcoming, symbol, current_price)

        return {"symbol": symbol, "earnings_rows": rows, "bars": bars,
                "analyst_count": analyst_count, "current_price": current_price,
                "implied_leg": implied_leg}

    def _fetch_implied_leg(self, upcoming: Optional[Dict[str, Any]], symbol: str,
                           spot: Optional[float]) -> Optional[Dict[str, Any]]:
        """The Task 8 implied leg's ONE provider I/O step: read a chain, pick the
        nearest expiry with runway, price the ATM straddle. Returns
        ``_atm_straddle_mids``'s dict, or None -- FAIL-TO-ABSENT at every step, never
        an exception escaping to the caller for anything short of a programming error.
        """
        if upcoming is None or spot is None or spot <= 0:
            return None
        account = getattr(self, "_gather_account", None)
        chain_fn = getattr(account, "get_option_chain", None)
        if not callable(chain_fn):
            # DUCK-TYPED CAPABILITY CHECK. `account` may be None (no context.account in
            # backtest, an unresolvable live account) or a real account that simply does
            # not support options -- both mean the same thing here: no chain, no feature.
            self._log_no_chain_capability_once()
            return None

        event_day = str(upcoming.get("report_date") or upcoming.get("fiscal_date_ending"))
        event_dt = _parse_day(event_day)
        if event_dt is None:
            return None
        event_date = event_dt.date()
        expiry_max = event_date + timedelta(days=_IMPLIED_LEG_EXPIRY_WINDOW_DAYS)
        try:
            contracts = chain_fn(symbol, event_date, expiry_max) or []
        except Exception as e:
            self._log_chain_read_failure(symbol, e)
            return None

        expiry = _nearest_expiry_with_runway(contracts, event_date)
        if expiry is None:
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.info(
                    f"FMPEarningsEvent: no expiry with runway past "
                    f"{event_date.isoformat()} in the {symbol} chain -- vol_cheapness "
                    f"stays absent for this symbol")
            return None
        return _atm_straddle_mids(contracts, expiry, spot)

    def _log_no_chain_capability_once(self, detail: str = "") -> None:
        """'absent capability -> feature absent, logged once' (design §9 Task 8): a
        structurally options-incapable deployment (no account, an account with no
        options support, an unwired live resolver) must not spam this once per symbol
        per analysis run for the whole life of the instance."""
        # getattr-guarded (not self._chain_capability_warned directly) so this also
        # works for a bare-constructed test double that skipped __init__ -- same
        # reason _log_skip reads self.logger via getattr rather than assuming it.
        if getattr(self, "_chain_capability_warned", False):
            return
        self._chain_capability_warned = True
        logger = getattr(self, "logger", None)
        if logger is not None:
            logger.info(
                "FMPEarningsEvent: no option-chain capability available for the "
                "vol_cheapness implied leg" + (f" ({detail})" if detail else "") +
                " -- the feature stays absent for every symbol this instance analyzes.")

    def _log_chain_read_failure(self, symbol: str, error: Exception) -> None:
        """A per-symbol chain-read failure (broker error, transient outage) -- logged
        every time like _log_skip, since it is an ordinary per-symbol data condition,
        not the structural "this deployment has no chain capability at all" case
        _log_no_chain_capability_once guards."""
        logger = getattr(self, "logger", None)
        if logger is not None:
            logger.info(f"FMPEarningsEvent: option chain read failed for {symbol} "
                       f"(vol_cheapness implied leg stays absent): {error}")

    def _resolve_live_account(self) -> Optional[Any]:
        """Best-effort LIVE account resolution for the Task 8 implied leg.

        Same seam ``MarketExpertInterface._get_current_price`` already uses
        (``ba2_common.core.instance_resolver``), reused rather than duplicated. NEVER
        fatal: an unwired resolver, an unresolvable account id, or any other failure
        here all mean the same thing -- no chain capability, so the feature stays
        absent (module docstring, 'Live seam'). run_analysis calls this ONCE per
        analysis, not the resolver directly, so a future account-shaped source needs
        to change only here.
        """
        try:
            from ba2_common.core.instance_resolver import get_instance_resolver
            # self.instance is whatever _load_expert_instance(id) read at __init__ --
            # NOT re-fetched here. If this expert instance is ever re-parented to a
            # different account after construction, a long-lived process (this
            # object outliving that change) would keep resolving the OLD
            # account_id until the instance cache recycles it. No different from
            # every other self.instance.* read in this class today; noted because
            # Task 8 is the first one that turns it into an OUTBOUND call (a chain
            # read) rather than a settings/label lookup.
            return get_instance_resolver().get_account_instance(self.instance.account_id)
        except Exception as e:
            self._log_no_chain_capability_once(str(e))
            return None

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
        # Task 8: hist_move / implied_move. PURE arithmetic here -- the implied leg
        # itself (the chain read) already happened in _gather; implied_move_pct and
        # this division never touch a provider or an account. Needs BOTH legs: an
        # absent implied move (no chain capability, no runway expiry, no two-sided ATM
        # quote) OR an absent hist_move (min_hist_events==0 admitted a symbol with zero
        # usable moves) leaves 'vol_cheapness' out of `features` entirely -- never 0.0.
        implied_pct = implied_move_pct(data_bundle.get("implied_leg"), current_price)
        if implied_pct is not None and "hist_move" in features:
            features["vol_cheapness"] = features["hist_move"] / implied_pct

        weights = {name: float(settings[key])
                   for name, key in _FEATURE_WEIGHT_SETTING.items()}
        confidence = composite_confidence(features, weights)
        if confidence is None:
            return _skip("no weighted feature available (every present feature's weight is 0)")

        days_to_earnings = (_parse_day(event_day) - _parse_day(as_of_day)).days
        payload = {
            DAYS_TO_EARNINGS_KEY: days_to_earnings,
            EVENT_DATE_KEY: event_day,
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
                # rec_days_to_earnings condition reads exactly this path -- from the same
                # constant, not from a second spelling of it.
                EARNINGS_STAMP_NAMESPACE: payload,
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
        pattern as FMPEarningsDrift/FMPRating). _gather_account (Task 8) is
        ``context.account`` itself -- the BacktestAccount the run already built,
        already scoped to THIS bar's as-of date; nothing is re-resolved here."""
        self._gather_symbol = context.extra.get("symbol", getattr(self, "_gather_symbol", None))
        self._gather_earnings_days_look = int(context.settings["earnings_days_look"])
        self._gather_min_analysts = int(context.settings["min_analysts"])
        self._gather_account = context.account
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
            self._gather_account = self._resolve_live_account()
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
                    # Same key path the backtest engine produces from raw_outputs -- and
                    # from the SAME constant, so the two producers cannot drift apart.
                    data={EARNINGS_STAMP_NAMESPACE:
                          rec.raw_outputs[EARNINGS_STAMP_NAMESPACE]},
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
                    "event": (None if rec.skip
                              else rec.raw_outputs[EARNINGS_STAMP_NAMESPACE]),
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
                ui.label(f"Event: {ev.get(EVENT_DATE_KEY)} "
                         f"({ev.get(DAYS_TO_EARNINGS_KEY)}d)")
            ui.label(f"Avg |move|: {ev.get('hist_move')} | "
                     f"Surprise vol: {ev.get('surprise_vol')} | "
                     f"Usable events: {ev.get('usable_events')}").classes('text-grey-8 mt-2')
