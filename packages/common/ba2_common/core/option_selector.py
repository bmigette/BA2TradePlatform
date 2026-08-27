"""Pure option-contract selection (no DB/network/broker). Operates on OptionContract lists.

Note: the `delta` and `percent_otm` methods require a non-None `strike_param`; callers must
validate (a None param raises, by design, to surface misconfigured rulesets).

`delta` selection needs `OptionContract.delta` populated. For the offline historical backtest
cache this is computed via Black-Scholes inversion of each contract's own daily close (see
`testplatform/backend/app/services/backtest/option_greeks.py` +
`HistoricalOptionsProvider.get_chain`'s per-as-of-date bar overlay) — not vendor-supplied, but
real point-in-time delta, not a snapshot fixed at the cache build's start date.
"""
from datetime import date
from typing import List, Optional, Tuple

from ba2_common.core.option_types import OptionContract
from ba2_common.core.types import OptionRight


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
    chain. Measured 2026-08-23 against the real cache: ``option_chain.open_interest`` is
    NULL for all 6,757,055 rows, so ``min_open_interest=100`` (the LIVE UI DEFAULT, set on
    all 14 live option entry actions) rejected 16/16 structures on 16/16 symbol-date-capital
    combinations. So the three states are: published-and-good (pass), published-and-thin
    (reject), and not-published-by-anyone (THIS — a configuration error, not a verdict on
    any contract).

    "Not published" includes PRESENT-BUT-DEGENERATE, not just absent: the same cache has
    ``bid == ask`` on all 6,757,055 rows, so ``spread_pct`` is a non-None constant 0.0 that
    grades nothing. See ``_publishes_spread``."""

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

    A PRESENT-BUT-CONSTANT-ZERO FIELD IS A PLACEHOLDER, NOT DATA (2026-08-23). ``spread_pct``
    is DERIVED — ``(ask - bid) / mid`` — so it is non-None whenever both columns are non-None,
    even when the source wrote the same number into both. Measured read-only against the real
    10 GB cache: ``SELECT sum(bid <> ask) FROM option_chain`` returns 0 over all 6,757,055
    rows, so ``spread_pct`` is exactly 0.0 for every quoted contract there. An ``is not None``
    probe therefore green-lit the gate, after which ``max_spread_pct`` measured nothing (0 is
    under every ceiling) while STILL fail-closing the 2,428,468 rows that carry no quote at
    all — a knob that silently drops a chunk of the chain and grades none of it.

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


def select_single(chain, *, method, strike_param, spot, option_type, dte_min, dte_max, today,
                  target_price=None, min_open_interest=None, max_spread_pct=None,
                  min_volume=None) -> Optional[OptionContract]:
    cands = _candidates(chain, option_type, dte_min, dte_max, today, min_open_interest,
                        max_spread_pct, min_volume)
    return _pick_by(method, cands, strike_param, spot, target_price, option_type)


def select_vertical_spread(chain, *, method, long_param, short_param, spot, option_type,
                           dte_min, dte_max, today, target_price=None,
                           min_open_interest=None, max_spread_pct=None, min_volume=None
                           ) -> Optional[Tuple[OptionContract, OptionContract]]:
    cands = _candidates(chain, option_type, dte_min, dte_max, today, min_open_interest,
                        max_spread_pct, min_volume)
    if len(cands) < 2:
        return None
    # Work within a single expiry: the earliest expiry in the window that has >=2 strikes.
    by_expiry = {}
    for c in cands:
        by_expiry.setdefault(c.expiry, []).append(c)
    for expiry in sorted(by_expiry):
        legs = by_expiry[expiry]
        if len(legs) < 2:
            continue
        long_leg = _pick_by(method, legs, long_param, spot, target_price, option_type)
        short_leg = _pick_by(method, [c for c in legs if c is not long_leg],
                             short_param, spot, target_price, option_type)
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
    expiry (wings must share the short leg's expiry)."""
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
