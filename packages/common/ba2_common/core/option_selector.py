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
    any contract)."""

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


class OptionDteWindowError(OptionSelectionConfigError):
    """The configured DTE window cannot contain any expiry."""


# field name -> "does this contract publish it?". `spread` is derived (needs BOTH sides of
# the quote), which is why it is probed via spread_pct rather than a raw attribute.
_LIQUIDITY_PROBES = {
    "open_interest": lambda c: c.open_interest is not None,
    "volume": lambda c: c.volume is not None,
    "spread": lambda c: c.spread_pct is not None,
}


def check_liquidity_data_available(chain: List[OptionContract], *,
                                   min_open_interest: Optional[int] = None,
                                   max_spread_pct: Optional[float] = None,
                                   min_volume: Optional[int] = None,
                                   underlying: Optional[str] = None) -> None:
    """Raise ``OptionLiquidityDataUnavailable`` for the first ENABLED gate whose field no
    contract in ``chain`` publishes. See that exception for the reasoning.

    Call this ONCE per action on the FULL fetched chain — never on a pre-filtered sublist
    (e.g. the straddle's same-strike put candidates), where a single unpublished value is a
    property of that one contract, not of the data source.

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
        if not any(probe(c) for c in chain):
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


def _target_strike(method, strike_param, spot, target_price, option_type) -> Optional[float]:
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
    ts = _target_strike(method, strike_param, spot, target_price, option_type)
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
