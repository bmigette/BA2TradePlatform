"""Pure option-contract selection (no DB/network/broker). Operates on OptionContract lists.

Note: the `delta` and `percent_otm` methods require a non-None `strike_param`; callers must
validate (a None param raises, by design, to surface misconfigured rulesets).

`delta` selection needs `OptionContract.delta` populated. For the offline historical backtest
cache this is computed via Black-Scholes inversion of each contract's own daily close (see
`testplatform/backend/app/services/backtest/option_greeks.py` +
`HistoricalOptionsProvider.get_chain`'s per-as-of-date bar overlay) — not vendor-supplied, but
real point-in-time delta, not a snapshot fixed at the cache build's start date.
"""
import logging
from datetime import date
from typing import List, Optional, Tuple

from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight

logger = logging.getLogger(__name__)


# Absolute floor on a contract's mark, in premium dollars per share. A contract marked below
# this is not realistically tradeable at any size: the bid/ask spread is 100%+ of its value, a
# systematic strategy would never get the modelled fill, and the whole position is worth less
# than one commission. This is an UNCONDITIONAL safety floor, not a tunable knob — the
# min_open_interest / max_spread_pct gates below are both opt-in (the options grid passes
# NEITHER, so they no-op there) and cannot be relied on to catch this.
#
# Evidence for the floor: the OS3 (skewed-credit) grid winner's 12 trades had entry premiums of
# $0.01-$0.09 (median $0.04), producing $2-$7 of P&L each on a $20k account — pure noise the GA
# nonetheless optimised toward, because nothing rejected it.
_MIN_TRADEABLE_PREMIUM = 0.10


class OptionSelectionConfigError(ValueError):
    """A selection parameter is configured such that it can never select anything.

    Raised INSTEAD of silently returning no contract, so a misconfigured rule is visible as
    an error rather than as a strategy that quietly stops trading. Callers in
    ``TradeActions._OptionEntryAction`` turn it into a failed TradeActionResult carrying the
    message, so it is loud but contained (it never crashes a live cycle or a backtest)."""


class OptionLiquidityDataUnavailable(OptionSelectionConfigError):
    """A liquidity gate is enabled but NO contract in the chain publishes that field.

    THE TRI-STATE. ``passes_liquidity`` fails CLOSED on ``None`` and that is deliberate:
    "this contract's liquidity is unknown while its peers report theirs" is a red flag, and
    flipping it to fail OPEN would let through exactly the illiquid contract the gate exists
    to stop. But fail-closed is only meaningful when the field is PUBLISHED AT ALL. When the
    data source emits it for nobody, "unknown" no longer distinguishes contracts — the gate
    rejects 100% of them and reports the same "No liquid <structure>" as a genuinely thin
    chain. Measured against the real cache: ``option_chain.open_interest`` is NULL for ALL
    of its rows (0 populated -- see THE CACHE, MEASURED below), so ``min_open_interest=100``
    (the LIVE UI DEFAULT, set on all 14 live option entry actions) rejected 16/16 structures
    on 16/16 symbol-date-capital combinations. So the three states are: published-and-good (pass), published-and-thin
    (reject), and not-published-by-anyone (THIS — a configuration error, not a verdict on
    any contract).

    "Not published" includes PRESENT-BUT-DEGENERATE, not just absent: the same cache has
    ``bid == ask`` on every one of its QUOTED rows, so ``spread_pct`` is a non-None constant
    0.0 that grades nothing. See ``_publishes_spread``."""

    def __init__(self, field: str, gate_value, underlying: Optional[str] = None):
        self.field = field
        self.gate_value = gate_value
        self.underlying = underlying
        where = f" for {underlying}" if underlying else ""
        super().__init__(
            f"Liquidity gate '{field}' is set to {gate_value} but no contract in the option "
            f"chain{where} publishes '{field}' — the data source does not provide it, so the "
            f"gate would reject every contract. Clear this gate, or use a data source that "
            f"publishes '{field}'.")


class OptionLiquidityDataMissingToday(OptionLiquidityDataUnavailable):
    """The gate's field is absent from THIS fetch, but the source has published it before.

    NOT THE SAME BUG, AND NOT THE SAME ADVICE (2026-08-23). ``OptionLiquidityDataUnavailable``
    tells the user to change their configuration, which is right when the source structurally
    lacks the field (the historical cache's all-NULL ``open_interest``) and wrong when it is a
    hole in one fetch. Live, Alpaca types ``open_interest`` as ``Optional`` and a snapshot page
    can legitimately come back without it; telling someone to clear a gate they will want back
    tomorrow — and shouting ERROR about it once per symbol per day — is worse than useless.

    The two are distinguished by evidence, not by guessing: ``_FIELDS_SEEN_PUBLISHED`` records
    every (source, field) this process has actually seen a value for. Having seen one proves
    the source CAN publish it, so a later empty fetch is a gap. Never having seen one leaves
    the structural reading, which is the loud one — the conservative direction, since the
    first fetch of a fresh process is judged by the stricter rule.

    Either way the action still FAILS: a gate nobody can answer is never applied to a chain it
    cannot measure, so nothing illiquid slips through. Only the severity and the advice differ
    (``_OptionEntryAction.execute`` logs this one at WARNING, not ERROR)."""

    def __init__(self, field: str, gate_value, underlying: Optional[str] = None,
                 source: Optional[str] = None):
        self.field = field
        self.gate_value = gate_value
        self.underlying = underlying
        self.source = source
        where = f" for {underlying}" if underlying else ""
        who = source or "this data source"
        # NB: skips OptionLiquidityDataUnavailable.__init__ on purpose — same attributes,
        # different message, and inheriting the "clear this gate" advice is the whole bug.
        OptionSelectionConfigError.__init__(
            self, f"Liquidity gate '{field}' is set to {gate_value} but no contract in "
                  f"today's option chain{where} carries '{field}', although {who} has "
                  f"published it before — treating this as a transient data gap, not a "
                  f"misconfiguration. No order is placed for this cycle; nothing to change.")


class OptionDteWindowError(OptionSelectionConfigError):
    """The configured DTE window cannot contain any expiry."""


def _publishes_spread(c: OptionContract) -> bool:
    """Does this contract carry a spread the ``max_spread_pct`` gate can actually measure?

    A PRESENT-BUT-CONSTANT-ZERO FIELD IS A PLACEHOLDER, NOT DATA. ``spread_pct`` is DERIVED
    — ``(ask - bid) / mid`` — so it is non-None whenever both columns are non-None, even when
    the source wrote the same number into both. On the cache below ``SELECT sum(bid <> ask)
    FROM option_chain`` returns 0, so ``spread_pct`` is exactly 0.0 for every quoted contract
    there. An ``is not None`` probe therefore green-lit the gate, after which
    ``max_spread_pct`` measured nothing (0 is under every ceiling) while STILL fail-closing
    the 357,211 rows that carry no quote at all — a knob that silently drops a chunk of the
    chain and grades none of it.

    THE CACHE, MEASURED — the ONE re-verified record; cite this, do not re-derive it.
    Re-measured 2026-08-31 against the only ``options_history.sqlite`` that ``CACHE_FOLDER``
    resolves to (4.12 GB; no WAL, ``page_count * page_size`` equals the file size, so this is
    the whole file). It REPLACES a "6,757,055 rows / 10 GB / three as_of snapshots" figure
    that had been copied into ~20 docstrings across the repo and matches nothing in this file
    — treat any surviving copy as stale.

      ``option_chain``  1,440,782 rows, 101 underlyings, and a SINGLE ``as_of`` snapshot
                        (2024-02-01) — not three.
                          * ``open_interest``  0 populated (100% NULL) — genuinely dead, and
                            ``option_bar`` has no such column, so nothing recovers it.
                          * ``volume``         0 populated (100% NULL) IN THE CHAIN. NOT dead
                            downstream: ``HistoricalOptionsProvider`` reads volume from the
                            BAR (see ``options_provider``'s VOLUME note), where it is 100%
                            populated. "NULL in option_chain" and "absent from the selector"
                            are different claims and were being conflated.
                          * ``bid``/``ask``/``last``  1,083,571 populated (75.2%); the other
                            357,211 (24.8%) are NULL. ``bid <> ask`` on 0 rows.
                          * ``iv``/``delta``/``gamma``/``theta``/``vega``  663,111 populated
                            (46.0%) — NOT "NULL on every row", as several docstrings still
                            claim. Present on 98 of 101 underlyings.
      ``option_bar``    19,484,995 rows over 2024-02-01..2026-07-07 (608 dates).
                          * ``volume`` 100% populated; ``iv``/greeks 88.2% (17,185,281),
                            present for all 101 underlyings.

    So on THIS cache delta selection is not dead and volume-based ranking is not dead; only
    ``open_interest`` is. The vendor-switch rationale recorded elsewhere in the repo overstates
    the first two — see the note in ``ba2_providers.options.tastytrade``.

    So "published" means a spread that is present AND non-degenerate: strictly positive. Zero
    means bid == ask (no market was quoted, only a close was copied into both sides), and
    negative means a crossed book, which ``passes_liquidity`` refuses outright — a wholly
    crossed chain would be another silent 100% rejection. Neither can grade liquidity.

    This is deliberately NOT generalised to ``open_interest``/``volume``: those are OBSERVED
    counts where 0 is a true, discriminating fact ("nobody traded it") and exactly what the
    gate should reject. ``HistoricalOptionsProvider`` even coerces a bar-less row's volume to
    a known 0 on purpose. Only the derived, two-column spread has a degenerate value that
    means "not published".

    COST: none beyond what the probe already paid. Detecting this needs no scan of the source
    — the in-memory chain being filtered is the sample — and ``any()`` short-circuits on the
    first contract with a real spread, so a healthy source stops at element 0."""
    sp = c.spread_pct
    return sp is not None and sp > 0


# field name -> "does this contract publish it?". `spread` is derived (needs BOTH sides of
# the quote), which is why it is probed via spread_pct rather than a raw attribute.
_LIQUIDITY_PROBES = {
    "open_interest": lambda c: c.open_interest is not None,
    "volume": lambda c: c.volume is not None,
    "spread": _publishes_spread,
}


# (source, field) pairs this process has seen a real value for at least once. See
# OptionLiquidityDataMissingToday for what it buys: it is the ONLY thing that can tell a
# source which never publishes a field from a source whose fetch happened to come back
# without one. Bounded by len(sources) * 3, so it never grows.
_FIELDS_SEEN_PUBLISHED = set()


def check_liquidity_data_available(chain: List[OptionContract], *,
                                   min_open_interest: Optional[int] = None,
                                   max_spread_pct: Optional[float] = None,
                                   min_volume: Optional[int] = None,
                                   underlying: Optional[str] = None,
                                   source: Optional[str] = None) -> None:
    """Raise for the first ENABLED gate whose field no contract in ``chain`` publishes:
    ``OptionLiquidityDataMissingToday`` when ``source`` has published that field before in
    this process, ``OptionLiquidityDataUnavailable`` when it never has. See those exceptions.

    Call this ONCE PER FETCHED CHAIN — on the full chain, never on a pre-filtered sublist
    (e.g. the straddle's same-strike put candidates), where a single unpublished value is a
    property of that one contract rather than of the data source. Equally, never on several
    chains POOLED together: calls and puts are separate fetches and a source can answer one
    and not the other, so a flattened call+put universe lets one publishing call vouch for a
    put chain that publishes nothing — re-arming the exact silent 100% rejection this guard
    exists to abolish. ``TradeActions._liq`` therefore loops and checks each side on its own.

    An EMPTY chain is a no-op: "no contracts at all" is its own condition and every caller
    already reports it ("Empty option chain"); mislabelling it as a gate problem would send
    the user to the wrong knob."""
    if not chain:
        return
    for field, gate in (("open_interest", min_open_interest),
                        ("volume", min_volume),
                        ("spread", max_spread_pct)):
        if gate is None:
            continue
        probe = _LIQUIDITY_PROBES[field]
        key = (source or "", field)
        if any(probe(c) for c in chain):
            _FIELDS_SEEN_PUBLISHED.add(key)
            continue
        if key in _FIELDS_SEEN_PUBLISHED:
            raise OptionLiquidityDataMissingToday(field, gate, underlying, source=source)
        raise OptionLiquidityDataUnavailable(field, gate, underlying)


def passes_liquidity(c: OptionContract, min_open_interest: Optional[int],
                     max_spread_pct: Optional[float],
                     min_volume: Optional[int] = None) -> bool:
    # Penny/near-worthless contracts are rejected outright. Judged on the mark (mid when both
    # sides quote, else last). A contract with NO price at all is left to the callers' own
    # missing-quote guards rather than being silently dropped here.
    mark = c.mid if c.mid is not None else c.last
    if mark is not None and mark < _MIN_TRADEABLE_PREMIUM:
        return False
    # DAILY TRADED VOLUME (2026-07-25). The most broadly available liquidity signal in the
    # historical cache: open_interest is NULL for every cached row, but volume is populated
    # for every bar. It matters because the BACKTEST FILL ENGINE independently enforces a
    # participation cap (an order may absorb at most ~10% of the bar's volume), so selecting
    # a contract that trades 1-3 contracts/day produces an order that can never fill — it
    # just sits pending and retries. Gating here makes the selector agree with the filler
    # instead of handing it unfillable candidates. Measured distribution across 13.7M cached
    # bars: p10=1, p25=3, p50=14, p75=71, p90=319 contracts/day — i.e. most of the chain is
    # far too thin to trade. A contract with NO volume figure is treated as failing the gate
    # only when the gate is ON (fail-closed: unknown liquidity is not assumed to be good).
    if min_volume is not None:
        if c.volume is None or c.volume < min_volume:
            return False
    if min_open_interest is not None:
        if c.open_interest is None or c.open_interest < min_open_interest:
            return False
    if max_spread_pct is not None:
        sp = c.spread_pct
        if sp is None or sp < 0 or sp > max_spread_pct:
            return False
    return True


def filter_dte(chain: List[OptionContract], today: date,
               dte_min: Optional[int], dte_max: Optional[int]) -> List[OptionContract]:
    out = []
    for c in chain:
        dte = (c.expiry - today).days
        if dte_min is not None and dte < dte_min:
            continue
        if dte_max is not None and dte > dte_max:
            continue
        out.append(c)
    return out


def target_strike(method, strike_param, spot, target_price, option_type) -> Optional[float]:
    """The strike a non-delta method aims at, or None when there is no target strike.

    ``None`` has THREE causes, not one: the ``delta`` method (which targets a delta, not a
    strike), an unrecognised method, and ``consensus_target`` with no ``target_price`` — that
    last branch returns the missing price verbatim, and it is reachable in production because
    ``select_single``'s ``target_price`` defaults to ``None``. Spelling all three out matters
    now that a second consumer exists: a caller who reads "None for the delta method" and
    writes ``if method != "delta": abs(c.strike - target_strike(...))`` gets a TypeError on a
    consensus_target rule whose recommendation carried no target.

    PUBLIC because ``option_selection_policy`` needs the identical number to measure a
    candidate's distance from the box centre. A second copy of this arithmetic would drift, and
    the first symptom would be the new policy picking a different contract at DEFAULT weights —
    breaking the no-op guarantee that lets the policy ship without changing any backtest.
    """
    if method == "percent_otm":
        if option_type == OptionRight.CALL:
            return spot * (1 + strike_param / 100.0)
        return spot * (1 - strike_param / 100.0)
    if method == "consensus_target":
        # TODO(P2 Task 5): optionally prefer strike <= target for calls / >= target for puts (currently nearest-absolute).
        return target_price
    return None


def _candidates(chain, option_type, dte_min, dte_max, today, min_oi, max_spread, min_volume=None):
    out = [c for c in chain if c.option_type == option_type]
    out = filter_dte(out, today, dte_min, dte_max)
    out = [c for c in out if passes_liquidity(c, min_oi, max_spread, min_volume)]
    return out


def describe_pick_failure(chain, *, method, option_type, dte_min, dte_max, today,
                          min_open_interest=None, max_spread_pct=None,
                          min_volume=None) -> Optional[str]:
    """Why a ``select_single``/``select_vertical_spread`` pick returned ``None``, when the
    cause is worth naming instead of the caller's own generic "No liquid <structure>" message.

    THE ONE CAUSE THIS NAMES (2026-09-01, F12 exact-cause discipline): the ``delta`` method's
    chain-wide missing-delta refusal. An ``O_LEAPC``-style job hitting a symbol whose chain
    carries no greeks must read as "no delta data on this chain", never as generic illiquidity
    -- a grid post-mortem cannot tell "nobody quotes this name" from "this vendor never
    publishes delta for this name" apart from the message text, and the two point at opposite
    remedies (skip the symbol vs. skip the method). ``option_selection_policy._no_candidate_reason``
    already draws this exact distinction for the (opt-in, non-default) ``SelectionPolicy`` path;
    this is the same distinction for the LEGACY path every builder actually runs under today.

    EVERY OTHER CAUSE RETURNS ``None`` ON PURPOSE -- an unrecognised method, a DTE/liquidity
    window that filtered the chain to nothing, or (non-default policy only) a box/ceiling that
    admitted no candidate. Those already have an honest generic message at the call site; this
    function only ever ADDS a reason, never removes the caller's fallback.

    GATED ON ``method == 'delta'`` FIRST -- the perf-acceptance rule. The re-filter below is a
    second ``_candidates`` pass, so a non-delta pick (the overwhelming majority of refusals)
    never pays for it, and a SUCCESSFUL delta pick never calls this at all: every call site
    invokes it only from inside its existing ``if contract is None`` / ``if pair is None``
    branch, i.e. only once, only on an already-failed pick.

    A candidate that merely lacks delta while its chain-mates carry one is NOT this cause --
    ``_pick_by`` already skips it and picks among the rest, so ``None`` is never reached for
    that chain. This only fires when the chain is non-empty after DTE/liquidity filtering and
    LITERALLY NONE of the survivors carry a delta."""
    if method != "delta":
        return None
    cands = _candidates(chain, option_type, dte_min, dte_max, today, min_open_interest,
                        max_spread_pct, min_volume)
    if cands and all(c.delta is None for c in cands):
        return (f"strike_method='delta' but none of the {len(cands)} candidate contracts in "
                f"the chain carry a delta — refusing rather than silently falling back to "
                f"another strike method")
    return None


# Expiry is the FINAL tie-break on every pick (2026-08-23). The cache lists the same strike
# in more than one in-window expiry, so candidates routinely tie on BOTH the distance metric
# and the strike, and ``min()`` then resolved them by INPUT-LIST ORDER — reversing the chain
# flipped BAC240405C00037000 -> BAC240412C00037000, and every leg pinned to that leg's expiry
# inherited the arbitrariness. Placing expiry LAST only orders pairs that were previously
# unordered, so no pre-existing selection changes; earliest expiry wins (the front expiry is
# the more liquid of two otherwise-identical contracts).
def _tie(c):
    return (c.strike, c.expiry)


def _pick_by(method, cands, strike_param, spot, target_price, option_type):
    if not cands:
        return None
    if method == "delta":
        usable = [c for c in cands if c.delta is not None]
        if not usable:
            return None
        return min(usable, key=lambda c: (abs(abs(c.delta) - abs(strike_param)), *_tie(c)))
    ts = target_strike(method, strike_param, spot, target_price, option_type)
    if ts is None:
        return None
    return min(cands, key=lambda c: (abs(c.strike - ts), *_tie(c)))


def _policy_pick(method, cands, strike_param, spot, target_price, option_type, today,
                 policy, structure_fn):
    """Route ONE box pick through ``SelectionPolicy`` — the F17 production seam.

    Only ever called with a NON-DEFAULT policy: the caller's guard is what preserves both the
    no-op guarantee (a default policy is byte-identical to ``_pick_by``, proven in
    ``test_option_selection_policy_noop.py``) and the legacy path's 27us cost for every trial
    that leaves the weights alone.

    THE APPLICABILITY REPORT IS WIRED HERE, NOT LEFT AS DECORATION (F17). When a payoff
    weight is live, one ``payoff_columns`` pass is computed and handed to BOTH
    ``inapplicable_features`` and ``pick`` — the report then describes the very numbers the
    ranking used (a stateful closure cannot make them disagree) and the ~5ms pass runs once.
    An inapplicable feature is INERT, never a demotion: the pick below still ranks every
    candidate identically on that column, and the log line is what separates "this gene is
    dead for this builder" from "this gene is live and unhelpful" in a grid post-mortem.

    THE PAYOFF PASS IS BUILT ON THE ELIGIBLE SET, NOT THE RAW ONE, and that ordering is the
    whole reason the sharing works. ``pick`` narrows through ``eligible`` before it scores,
    so a payoff computed over the RAW candidates describes a superset of what gets ranked.
    ``pick_with_reason`` defends the alignment (it drops a payoff whose length no longer
    matches), so the scores were never wrong — but the defence costs the very thing the
    sharing exists to buy: on any narrowing chain the shared pass was thrown away and
    ``score_all`` computed a SECOND one, 2 passes where the docstring promised 1. And the
    REPORT above, which is not length-checked, went on describing the superset while the
    ranking scored the narrowed set — an inert/live verdict about candidates that were never
    in the running. Narrowing is not hypothetical: under ``method="delta"`` a single contract
    with no delta is enough.

    Calling ``eligible`` here and handing the result to ``pick`` is safe because the filter is
    a per-candidate predicate and therefore IDEMPOTENT — ``pick`` re-runs it on an already
    filtered list and gets the same list back, so the length check now always passes and the
    payoff survives. The re-run is free unless a ``max_loss_ceiling`` AND a ``structure_fn``
    are both set (``_eligible_and_reason`` returns before charging anything otherwise), and
    even then it recharges only the already-narrowed survivors.
    """
    from ba2_common.core import option_selection_policy as _osp

    ctx = _osp.PolicyContext(strike_method=method, today=today, target=strike_param,
                             spot=spot, target_price=target_price, option_type=option_type,
                             structure_fn=structure_fn)
    cands = _osp.eligible(cands, ctx)
    if not cands:
        # ``pick`` would also return None here; returning early keeps the payoff pass and the
        # report off an empty set rather than computing both to describe nothing.
        return None
    payoff = None
    if policy.w_profit or policy.w_rr:
        payoff = _osp.payoff_columns(cands, ctx)
        inert = _osp.inapplicable_features(cands, ctx, payoff=payoff)
        if inert:
            logger.info(
                "option selection: features %s are inapplicable for this pick (%d "
                "candidates, structure_fn %s) — their weights are inert, not demoting",
                ", ".join(inert), len(cands),
                "supplied" if structure_fn is not None else "absent")
    return _osp.pick(cands, ctx, policy, payoff=payoff)


def select_single(chain, *, method, strike_param, spot, option_type, dte_min, dte_max, today,
                  target_price=None, min_open_interest=None, max_spread_pct=None,
                  min_volume=None, policy=None, structure_fn=None) -> Optional[OptionContract]:
    cands = _candidates(chain, option_type, dte_min, dte_max, today, min_open_interest,
                        max_spread_pct, min_volume)
    # A None or DEFAULT policy takes the legacy path. Not an optimisation only: the default
    # policy is PROVABLY the same answer (the no-op suite), so the branch cannot change a
    # result — but `is_default` is the property every weight must be listed in, so routing on
    # it keeps "policy present but all-zero" byte-identical AND at the legacy 27us.
    if policy is not None and not policy.is_default:
        return _policy_pick(method, cands, strike_param, spot, target_price, option_type,
                            today, policy, structure_fn)
    return _pick_by(method, cands, strike_param, spot, target_price, option_type)


def select_vertical_spread(chain, *, method, long_param, short_param, spot, option_type,
                           dte_min, dte_max, today, target_price=None,
                           min_open_interest=None, max_spread_pct=None, min_volume=None,
                           policy=None, structure_fn=None
                           ) -> Optional[Tuple[OptionContract, OptionContract]]:
    cands = _candidates(chain, option_type, dte_min, dte_max, today, min_open_interest,
                        max_spread_pct, min_volume)
    if len(cands) < 2:
        return None

    # BOTH legs are box picks, so BOTH route through the policy (same non-default guard as
    # ``select_single``). Wiring only the long leg would leave the short strike — the leg that
    # carries a credit vertical's whole thesis — outside the searched choosing.
    def _leg(legs_, param):
        if policy is not None and not policy.is_default:
            return _policy_pick(method, legs_, param, spot, target_price, option_type,
                                today, policy, structure_fn)
        return _pick_by(method, legs_, param, spot, target_price, option_type)

    # Work within a single expiry: the earliest expiry in the window that has >=2 strikes.
    by_expiry = {}
    for c in cands:
        by_expiry.setdefault(c.expiry, []).append(c)
    for expiry in sorted(by_expiry):
        legs = by_expiry[expiry]
        if len(legs) < 2:
            continue
        long_leg = _leg(legs, long_param)
        short_leg = _leg([c for c in legs if c is not long_leg], short_param)
        if not long_leg or not short_leg or long_leg.strike == short_leg.strike:
            continue
        # For a debit CALL spread, long is the lower strike. Order so long<short.
        lo, hi = sorted([long_leg, short_leg], key=lambda c: c.strike)
        if option_type == OptionRight.CALL:
            return (lo, hi)   # buy lower, sell higher (debit)
        return (hi, lo)       # put debit spread: buy higher strike, sell lower
    return None


def select_wing(chain, *, center_strike, width_pct, option_type,
                dte_min, dte_max, today, expiry=None,
                min_open_interest=None, max_spread_pct=None,
                min_volume=None) -> Optional[OptionContract]:
    """Pick the wing contract nearest ``center_strike`` moved ``width_pct`` percent
    farther OTM (calls: up; puts: down). When ``expiry`` is given, restrict to that
    expiry (wings must share the short leg's expiry).

    DELIBERATELY NOT POLICY-GOVERNED, and this is the record of that exclusion (2026-08-31 —
    the decision was implicit before, which is why it is written down here rather than
    inferred from a missing kwarg). ``select_single`` and ``select_vertical_spread`` both take
    a ``policy`` and route non-default ones through ``_policy_pick``; this function takes none,
    so the FIVE ``TradeActions`` call sites that reach it choose their wing outside the searched
    ranking. They are the iron condor's two wings, the call-side wing of the broken-wing
    butterfly's neighbour structure, the butterfly's upper wing, and the put ratio spread's
    short leg.

    THE RATIONALE IS THAT A WING HAS NO BOX TO RANK INSIDE. Every other pick is "the best
    contract within a band", which is what a ``SelectionPolicy`` grades: ``_in_box`` bounds the
    candidates and ``distance_from_target`` scores them. A wing is not a band — it is ONE
    derived strike, ``center_strike`` moved ``width_pct`` percent, and the width is itself a
    searched gene. Handing the same choice to a policy would put two search dimensions on one
    axis and let the weights pull the wing off the width the GA just chose: the degenerate
    double-search the design rejects for ``w_box_center`` (a uniform rescale changes no
    ranking) and for ``w_rr`` (collinear with ``w_premium``). Nearest-strike-to-a-derived-target
    is the whole intent, so there is nothing left for a weight to express.

    THE ONE SITE WHERE THAT ARGUMENT IS WEAKEST is ``_OpenPutRatioSpreadAction``: its SHORT put
    — the 2x leg carrying the structure's risk — is a ``select_wing``, so it is ungoverned while
    its long leg is not. That is structurally the same shape as the vertical defect this track
    already fixed (wiring only the long leg leaves "the leg that carries the thesis" outside the
    searched choosing — see ``select_vertical_spread``). It is left as-is here ON PURPOSE and
    not by oversight: a vertical's short leg has its OWN ``short_param`` target and therefore a
    band to rank within, whereas the ratio's short leg is defined only as a width from the long
    strike. Governing it would mean either giving ``select_wing`` a policy and a box (an API
    change touching all five sites) or re-modelling O_RS's short leg as a target-based pick (a
    live behaviour change). Both are real options and neither is a docstring fix; if the ratio
    spread ever underperforms in a way that points at its short strike, THIS is the paragraph to
    reopen.

    Liquidity gates DO still apply (``_candidates`` runs them), so an ungoverned wing is still
    a tradable one; only the RANKING is excluded."""
    cands = _candidates(chain, option_type, dte_min, dte_max, today,
                        min_open_interest, max_spread_pct, min_volume)
    if expiry is not None:
        cands = [c for c in cands if c.expiry == expiry]
    if not cands:
        return None
    if option_type == OptionRight.CALL:
        target = center_strike * (1 + width_pct / 100.0)
    else:
        target = center_strike * (1 - width_pct / 100.0)
    return min(cands, key=lambda c: (abs(c.strike - target), *_tie(c)))
