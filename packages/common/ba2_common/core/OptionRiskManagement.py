"""The option risk manager: ONE implementation, reached by live and by the backtest.

Design: ``docs/superpowers/specs/2026-08-27-option-risk-manager-design.md`` — §4's binding
operator decision (2026-08-30) is that the option RM has **one** implementation shared by
both runtimes, exactly like the classic RM, wired at the ``TradeActions`` /
``TradeActionEvaluator`` seam in ``ba2_common``.

What this module is for
-----------------------
``option_book`` (the sleeve rails and the drawdown breaker) was written pure and tested
hard, and then never called from a production path. The 2026-08-30 program review recorded
that as **F5**: ``check_rails``/``admit`` had ZERO production callers, so
``max_deployment_pct``, ``max_notional_leverage``, ``undefined_risk_max_pct``, the
max-concurrent cap and the one-per-underlying cap were enforced *nowhere*, and the circuit
breaker flattened the book once and then gated nothing — the next entry cycle re-opened it
at the bottom of the drawdown. This module is those rails' first production caller.

It is reached from ONE place: ``_OptionEntryAction._submit_option_order``, the single choke
point all seventeen option builders end at. Wiring it there arms the live pass
(``TradeManager`` -> ``TradeActionEvaluator``) and the backtest engine (``daily_engine`` ->
``TradeActionEvaluator``) in the same commit, because both construct the *same*
``ba2_common`` evaluator and run the *same* action classes. There is deliberately no hook in
``daily_engine`` (backtest-only) and none in ``JobManager`` (live-only): a second wiring
point is a second implementation waiting to happen.

Opt-in, and provably inert until opted in
-----------------------------------------
Everything here is gated on ``risk_manager_mode == "classic_options"``. An expert in
``classic`` or ``smart`` mode — and an option action with no expert at all — never reaches a
single line of it, which is what makes design §11's "nothing existing changes" a property
rather than a hope. ``test_a_default_configuration_never_reaches_the_option_risk_manager``
pins it.

Fail closed, loudly
-------------------
* An **unknown** ``risk_manager_mode`` does NOT engage the option gate, and says so at
  WARNING naming the instance, the value and the admitted set (once per instance, not once
  per action). The DISPATCH question is the one thing here that fails open, deliberately:
  it used to raise from outside the guarded path, and one expert carrying the literal
  string ``"None"`` then aborted the whole Phase-1 entry pass under
  ``BA2_ERROR_MODE=enforce`` -- taking down unrelated experts' entries that had worked
  before the branch existed. See ``option_risk_manager_enabled``.
* A rail setting the expert does not declare **refuses the entry** and names the setting. It
  is never defaulted — ``option_book._require`` already refuses to substitute a default for a
  risk rail, and this module refuses one bar earlier so the operator gets a readable
  ``TradeActionResult`` instead of a ``KeyError`` traceback.
* An unmeasurable max loss, notional or assignment cost declines. ``check_rails`` enforces
  that; nothing here converts an unknown into a number first.

Nothing is reconstructed that was persisted
-------------------------------------------
The candidate's max loss is measured ONCE, by ``_submit_option_order``, and handed here —
this module never re-derives it from legs and never parses an OCC symbol. On every
structure but one it is exactly the value stamped on the order row
(``data["max_loss_per_contract"]``, design §8.2). The exception is a verified COVERED CALL
(review finding M3, 2026-09-01): the order stamps nothing there, because the stamp is
``loss_pct_of_max_loss``'s denominator against an option-legs-only numerator, while the
rails ask what the WHOLE position commits — so the RM is handed the cover-inclusive
(spot − credit) × 100 and the exit condition self-disarms. Two questions, two answers, one
measurement function. The sleeve's committed capital comes from
``option_lifecycle.structure_metrics`` over netted order rows, the single definition
``option_book`` already consumes.

The breaker peak is process state, and there is only one of it
--------------------------------------------------------------
``BreakerState`` is a value and something must carry it between evaluations. It lives in a
module-level map keyed by expert instance, here, so the **entry** gate and the **exit** pass
read one latch: ``option_lifecycle_service`` (the live exit pass) owns the transitions via
``update_breaker`` and stores them here; this module only READS. That asymmetry is
deliberate and load bearing — ``update_breaker`` reports ``tripped`` as an *edge*, so an
entry gate that also updated the state could consume the edge on a bar where the exit pass
had not yet run, and the flatten would never be signalled at all. A process restart forgets
the peak (persisting it needs a migration); that is stated rather than hidden.

Charging what is submitted but not yet visible
----------------------------------------------
A structure submitted this cycle is a ``WAITING`` transaction with no executed leg, so
``book_totals`` cannot see it. Left at that, three entries in one cycle each measure the
book as empty and all three open — which is exactly the concentrated book F5 says the rails
exist to stop. Every admitted candidate is therefore kept as a *pending charge* and replayed
through ``option_book.admit`` ahead of the next candidate, so the running sleeve includes it.
A pending charge is dropped the moment its transaction becomes visible in the book (or stops
existing), so nothing is ever counted twice.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from ba2_common.core.option_book import (
    RAIL_OK, BookTotals, BreakerState, CandidateStructure, RailVerdict, admit, book_totals,
    rearm,
)
from ba2_common.core.option_lifecycle import (
    LifecycleLeg, OptionStructure, put_assignment_cost, structure_metrics,
)
from ba2_common.core.option_request import (
    OPTION_RAILS_UNCONFIGURED_REFUSAL, OPTION_RAIL_REFUSAL, validate_refusal_phrase,
)
from ba2_common.core.types import OptionRight, OrderDirection
from ba2_common.logger import logger

_EPS = 1e-9

# ---------------------------------------------------------------------------
# the mode
# ---------------------------------------------------------------------------
#: Rule-based risk management through the automation rulesets (the default).
RISK_MANAGER_MODE_CLASSIC = "classic"
#: The agentic LLM risk manager.
RISK_MANAGER_MODE_SMART = "smart"
#: The option risk manager: option entries are gated by the sleeve rails and the drawdown
#: breaker in ``option_book`` before they reach the broker.
RISK_MANAGER_MODE_CLASSIC_OPTIONS = "classic_options"

#: THE admitted set. ``utils.get_risk_manager_mode`` and the ``risk_manager_mode`` setting
#: definition both read it from here so a fourth spelling of the same list cannot drift.
VALID_RISK_MANAGER_MODES: Tuple[str, ...] = (
    RISK_MANAGER_MODE_CLASSIC, RISK_MANAGER_MODE_SMART, RISK_MANAGER_MODE_CLASSIC_OPTIONS,
)


class OptionRiskManagerModeError(ValueError):
    """An expert declares a ``risk_manager_mode`` that is not one of the admitted three.

    A configuration error, not a market condition, and therefore a raise. The alternative —
    treating the unrecognised value as ``classic`` — is how a risk manager comes to be
    switched off by a typo while every log line claims it is running.
    """


def normalise_risk_manager_mode(settings: Optional[Mapping[str, Any]]) -> str:
    """The expert's declared mode, or ``classic`` when it declares none.

    Absent / empty / whitespace is ``classic``: that is the setting's own default and the
    state every existing expert is in, so it must stay a silent no-op. Anything else that is
    not an admitted mode RAISES.
    """
    if not settings or not isinstance(settings, Mapping):
        return RISK_MANAGER_MODE_CLASSIC
    raw = settings.get("risk_manager_mode", "") or ""
    mode = str(raw).strip().lower()
    if not mode:
        return RISK_MANAGER_MODE_CLASSIC
    if mode not in VALID_RISK_MANAGER_MODES:
        raise OptionRiskManagerModeError(
            f"risk_manager_mode {raw!r} is not one of {list(VALID_RISK_MANAGER_MODES)} — "
            f"refusing to fall back to {RISK_MANAGER_MODE_CLASSIC!r}, because a risk "
            f"manager silently switched off by a typo is worse than a loud refusal.")
    return mode


#: ``(expert instance id, offending raw mode)`` already logged. The entry gate is consulted
#: once per candidate STRUCTURE and a Phase-1 pass evaluates many, so an un-deduplicated
#: warning would bury the log under one identical line per action.
_WARNED_UNADMITTED_MODES: set = set()


def reset_mode_warnings() -> None:
    """Forget which unadmitted modes have been warned about. Tests, and a fresh process."""
    _WARNED_UNADMITTED_MODES.clear()


def option_risk_manager_enabled(settings: Optional[Mapping[str, Any]],
                                expert_instance_id: Optional[int] = None) -> bool:
    """Does this expert route its option entries through the option risk manager?

    **THE DISPATCH QUESTION FAILS OPEN, and only the dispatch question** (review finding
    H2, 2026-09-01). The gate engages on ``classic_options`` and on nothing else: any other
    string -- ``classic``, ``smart``, or garbage -- answers ``False``, and the entry then
    takes the legacy path it took before this module existed, byte for byte.

    It used to RAISE on an unadmitted string, from a call site OUTSIDE the guarded path, so
    one expert carrying the literal string ``"None"`` (``ExtendableSettingsInterface`` line
    87 documents that population: ``str(None)`` was once written to the settings table)
    aborted the whole Phase-1 entry pass under ``BA2_ERROR_MODE=enforce`` -- killing the
    remaining actions of experts that had nothing to do with options, where before the
    branch the same value read as ``classic`` and traded normally. A risk manager that
    cannot be selected by a typo is worth having; a typo that stops OTHER experts trading
    is not, and it is the same leniency ``utils.get_risk_manager_mode`` already documents
    for the same reason.

    The garbage is not swallowed: it is logged at WARNING naming the instance, the value and
    the admitted set, ONCE per (instance, value) rather than once per action.

    FAIL-CLOSED STAYS INSIDE THE OPTION PATH. An expert that *did* select
    ``classic_options`` and cannot produce its sleeve rails still refuses the entry
    (``admit_option_entry`` -> ``rail_settings``). What is fail-open here is only *whether
    the option risk manager is the thing being asked*.
    """
    try:
        return normalise_risk_manager_mode(settings) == RISK_MANAGER_MODE_CLASSIC_OPTIONS
    except OptionRiskManagerModeError as e:
        raw = (settings or {}).get("risk_manager_mode") if isinstance(settings, Mapping) else None
        key = (expert_instance_id, str(raw))
        if key not in _WARNED_UNADMITTED_MODES:
            _WARNED_UNADMITTED_MODES.add(key)
            logger.warning(
                f"Option RM: expert instance {expert_instance_id} declares "
                f"risk_manager_mode {raw!r}, which is not one of "
                f"{list(VALID_RISK_MANAGER_MODES)} -- the option risk manager is NOT "
                f"engaged and this expert keeps its legacy (classic) entry behaviour. "
                f"Fix the setting if the sleeve rails were meant to run. ({e})")
        return False


# ---------------------------------------------------------------------------
# the rails the expert must declare
# ---------------------------------------------------------------------------
#: Read on EVERY candidate. ``option_book.check_rails`` raises on a missing one; this module
#: refuses one step earlier so the operator sees which knob is missing in the UI.
REQUIRED_RAIL_SETTINGS: Tuple[str, ...] = (
    "max_concurrent_structures", "max_deployment_pct", "max_notional_leverage",
)
#: Read only when the candidate measures (or declares) as undefined risk.
UNDEFINED_RISK_SETTING = "undefined_risk_max_pct"


def rail_settings(expert) -> Tuple[Dict[str, Any], List[str]]:
    """(the sleeve's rails, the REQUIRED ones this expert does not declare).

    Read through ``get_setting_with_interface_default`` — the same accessor every other
    expert setting uses, and the same shape ``option_lifecycle_service._lifecycle_settings``
    uses — so a configured value wins and anything else is UNDECLARED.

    ABSENT MEANS UNDECLARED, and that is why ``MarketExpertInterface`` gives these four no
    ``default`` (review finding M1, 2026-09-01). While it did, the accessor always returned
    a number, ``missing`` was structurally always empty, and the refusal below could only be
    reached by a test double that raised where no real expert does — i.e. the "never a
    substituted default for a risk rail" rule was documented, tested against a fake, and
    enforced nowhere. A risk limit nobody stated is not a risk limit.
    """
    settings: Dict[str, Any] = {}
    missing: List[str] = []
    for key in REQUIRED_RAIL_SETTINGS + (UNDEFINED_RISK_SETTING,):
        try:
            value = expert.get_setting_with_interface_default(key, log_warning=False)
        except Exception:  # noqa: BLE001 — ValueError today; any lookup failure is "absent"
            value = None
        if value is None:
            if key in REQUIRED_RAIL_SETTINGS:
                missing.append(key)
            continue
        settings[key] = value
    return settings, missing


# ---------------------------------------------------------------------------
# per-sleeve process state: the breaker latch, the in-flight charges, the journal
# ---------------------------------------------------------------------------
def _sleeve_key(expert_instance_id: int) -> Tuple[Optional[int], int]:
    """The key this sleeve's process state is filed under.

    LIVE: ``(None, expert_id)`` -- a process-wide key, deliberately. The exit pass runs on
    the ``JobManager`` scheduler thread and entries run on ``WorkerQueue`` worker threads,
    so a thread-local latch would mean the breaker that flattens the book gates no entry at
    all: exactly the F5 defect, reintroduced by an isolation mechanism.

    BACKTEST: ``(thread id, expert_id)``. The GA runs trials CONCURRENTLY in worker threads
    of one process, each with its own thread-local trade store, and they reuse the same
    expert instance ids. A process-wide latch would let trial B's drawdown stand trial A's
    sleeve down -- a cross-trial dependency that makes a GA result irreproducible. The tell
    for "this thread is a backtest" is the thread-local in-memory trade store, the same
    signal every other dual-path accessor keys on.

    Sequential trials on ONE worker thread still share a key, which is why
    ``backtest_trading_db`` calls :func:`reset_thread_state` at both ends of every run.
    """
    from ba2_common.core.trade_store import inmem_trades_active

    if inmem_trades_active():
        return (threading.get_ident(), expert_instance_id)
    return (None, expert_instance_id)


_BREAKER_STATE: Dict[Tuple[Optional[int], int], BreakerState] = {}


def get_breaker_state(expert_instance_id: int) -> BreakerState:
    """The sleeve's breaker as it stands. A sleeve nobody has evaluated is un-halted."""
    return _BREAKER_STATE.get(_sleeve_key(expert_instance_id), BreakerState())


def set_breaker_state(expert_instance_id: int, state: BreakerState) -> None:
    """Store the state ``option_book.update_breaker`` just produced.

    ONLY the exit pass calls this. The entry gate reads; it must never write, because
    ``tripped`` is an edge and a consumer that is not the flatten would swallow it.
    """
    if not isinstance(state, BreakerState):
        raise TypeError(
            f"set_breaker_state requires a BreakerState, got {type(state).__name__}")
    _BREAKER_STATE[_sleeve_key(expert_instance_id)] = state


def reset_breaker_states() -> None:
    """Forget every sleeve's breaker. Tests, and an operator starting clean."""
    _BREAKER_STATE.clear()


def rearm_breaker(expert_instance_id: int) -> BreakerState:
    """Clear a stand-down UNCONDITIONALLY (``option_book.rearm``): the operator override.

    Nothing calls this automatically — it re-risks a sleeve that has not recovered. The peak
    is deliberately KEPT, so the drawdown that caused the stand-down is not erased.
    """
    state = rearm(get_breaker_state(expert_instance_id))
    _BREAKER_STATE[_sleeve_key(expert_instance_id)] = state
    logger.warning(f"Option RM: circuit-breaker stand-down for expert "
                   f"{expert_instance_id} cleared by an explicit re-arm")
    return state


# ---------------------------------------------------------------------------
# the sleeve: persisted rows -> OptionStructure values
# ---------------------------------------------------------------------------
def open_option_transactions(expert_instance_id: int) -> List[Any]:
    """This expert's OPENED transactions whose intent is OPTION.

    OPENED only, not WAITING: a WAITING transaction has no executed leg, so there is nothing
    to net and nothing to total. What it *would* commit is carried as a pending charge
    instead (see the module docstring).

    Reads through the dual-path ``transactions_where`` accessor, which is why one
    implementation serves both runtimes: live it issues the SELECT, and under the backtest's
    in-memory trade store it reads the RAM dict store that a raw SQL session is blind to.
    """
    from ba2_common.core.trade_store import transactions_where
    from ba2_common.core.types import AssetClass, TransactionStatus

    rows = transactions_where(expert_id=expert_instance_id,
                              status=TransactionStatus.OPENED)
    return sorted((t for t in rows if t.asset_class == AssetClass.OPTION),
                  key=lambda t: t.id)


def executed_option_orders(transaction_id: int) -> List[Any]:
    """The transaction's EXECUTED option order rows, contract-bearing legs only."""
    from ba2_common.core.trade_store import orders_where
    from ba2_common.core.types import AssetClass, OrderStatus

    executed = OrderStatus.get_executed_statuses()
    return [o for o in orders_where(transaction_id=transaction_id)
            if getattr(o, "asset_class", None) == AssetClass.OPTION
            and getattr(o, "contract_symbol", None)
            and o.status in executed]


def build_structure(txn) -> Optional[OptionStructure]:
    """One ``OptionStructure`` from one transaction's order rows, or ``None``.

    **Netting**, per contract symbol over the transaction's EXECUTED option orders, BUY ``+``
    and SELL ``-`` — the same netting ``CloseOptionAction`` already does. A leg bought back
    to close nets to zero and stops counting.

    ``None`` means the transaction has NO executed option leg on record: a position the
    ledger cannot describe. The caller reports it; it is never silently skipped, and it is
    never treated as an empty (zero-committing) structure.

    Promoted out of ``option_lifecycle_service._build_structure`` so the live exit pass and
    the shared entry gate build one book from one definition. The service now delegates here.
    """
    orders = executed_option_orders(txn.id)
    if not orders:
        return None

    net: Dict[str, float] = {}
    meta: Dict[str, Any] = {}
    total_cash = 0.0
    cash_known = True
    for order in orders:
        contract = order.contract_symbol
        qty = float(order.filled_qty or order.quantity or 0.0)
        sign = 1.0 if order.side == OrderDirection.BUY else -1.0
        net[contract] = net.get(contract, 0.0) + sign * qty
        meta[contract] = order
        if order.open_price is None:
            cash_known = False
            continue
        total_cash += -sign * float(order.open_price) * qty

    legs = tuple(
        LifecycleLeg(
            contract_symbol=contract,
            net_qty=net[contract],
            strike=getattr(meta[contract], "strike", None),
            option_type=getattr(meta[contract], "option_type", None),
            expiry=getattr(meta[contract], "expiry", None),
            underlying=getattr(meta[contract], "underlying_symbol", None),
        )
        for contract in sorted(net)
    )

    quantity = abs(float(txn.quantity or 0.0))
    entry_premium = entry_net_premium(txn)
    if entry_premium is None or not cash_known or quantity < _EPS:
        entry_premium, realized_cash = None, 0.0
    else:
        realized_cash = total_cash - (-entry_premium * quantity)

    return OptionStructure(
        transaction_id=txn.id,
        underlying=txn.symbol,
        strategy=txn.option_strategy or "",
        legs=legs,
        quantity=quantity,
        multiplier=int(txn.multiplier or 100),
        entry_net_premium=entry_premium,
        realized_cash=realized_cash,
        expiry=txn.expiry,
    )


def entry_net_premium(txn) -> Optional[float]:
    """The structure's entry net premium per share, signed: ``+`` debit, ``-`` credit.

    Normalised through the transaction's ``side`` exactly as
    ``TradeConditions._get_spread_pnl_via_transaction`` does, so a row that stored the
    magnitude prices identically to one that stored the sign. ``None`` (never recorded) stays
    ``None`` — it is the percent basis, and an unknown basis is undefined, not zero.
    """
    if txn.open_price is None:
        return None
    price = float(txn.open_price)
    if txn.side == OrderDirection.SELL:
        return -abs(price)
    return abs(price)


def sleeve_structures(expert_instance_id: int) -> Tuple[List[OptionStructure], List[int]]:
    """(this expert's open option structures, the transaction ids that could not be built)."""
    structures: List[OptionStructure] = []
    unbuildable: List[int] = []
    for txn in open_option_transactions(expert_instance_id):
        structure = build_structure(txn)
        if structure is None:
            unbuildable.append(txn.id)
            continue
        structures.append(structure)
    return structures, unbuildable


# ---------------------------------------------------------------------------
# the candidate: an entry order, as a value the rails can price
# ---------------------------------------------------------------------------
def candidate_from_entry(*, underlying: str, option_strategy: str, legs: Sequence[Any],
                         quantity: int, max_loss_per_contract: Optional[float],
                         multiplier: int = 100,
                         stock_cover_price: Optional[float] = None) -> CandidateStructure:
    """The ``CandidateStructure`` for one entry order about to be submitted.

    ``max_loss_per_contract`` is what ``_submit_option_order`` has ALREADY measured for the
    RAILS' question. It arrives as an argument rather than being re-derived here: one
    definition, one place, and no leg reconstruction. On every structure but a verified
    covered call it is also the value persisted on the order row (design §8.2); on a covered
    call the order persists nothing and this is the cover-inclusive measurement (review
    finding M3, 2026-09-01 — the two questions have different numerators). ``None`` means
    the payoff evaluator returned UNBOUNDED or UNMEASURABLE, and it stays ``None`` all the
    way into ``check_rails``, which declines it — design §8.3's default refusal for
    undefined risk, and the only honest answer for a broken quote.

    ``notional`` and ``is_defined_risk`` come from ``structure_metrics`` over the candidate's
    own legs — the SAME arithmetic the book side uses, deliberately, because a second
    formula for short-side notional is precisely the divergence ``structure_metrics`` was
    promoted to end. ``short_put_assignment`` is ``put_assignment_cost`` per short put leg,
    the single shared definition.

    ``stock_cover_price`` is the covered-call seam (2026-08-31, operator decision): the
    submitting builder has VERIFIED the account holds the covering shares and priced them
    at current spot (the same value ``max_loss_per_contract`` above was measured with).
    ``structure_metrics`` sees
    only the ORDER's option legs, so from those alone a covered call's short call reads
    as naked; the declared cover overrides that to COVERED, which is what routes the
    candidate's (measured) max loss to the deployment cap instead of the
    ``undefined_risk_max_pct`` sub-cap (``option_book._is_undefined_risk`` honours the
    explicit ``is_defined_risk`` declaration).
    """
    lifecycle_legs = []
    for leg in legs:
        ratio = int(getattr(leg, "ratio_qty", None) or 1)
        sign = 1.0 if leg.side == OrderDirection.BUY else -1.0
        lifecycle_legs.append(LifecycleLeg(
            contract_symbol=leg.contract_symbol,
            net_qty=sign * ratio * float(quantity),
            strike=getattr(leg, "strike", None),
            option_type=getattr(leg, "option_type", None),
            expiry=getattr(leg, "expiry", None),
            underlying=getattr(leg, "underlying", None) or underlying,
        ))
    structure = OptionStructure(transaction_id=-1, underlying=underlying,
                               strategy=option_strategy, legs=tuple(lifecycle_legs),
                               quantity=float(quantity), multiplier=multiplier)
    metrics = structure_metrics(structure)
    is_defined_risk = metrics.is_defined_risk
    if stock_cover_price is not None and is_defined_risk is False:
        # The verified held-stock cover (docstring above): covered, not naked. Applied
        # only over a measured False -- an unmeasurable metrics answer stays None.
        is_defined_risk = True

    assignment: Optional[float] = 0.0
    for leg in structure.held_legs:
        if not leg.is_short:
            continue
        if leg.option_type is None:
            assignment = None       # unknown right: unknown is not "not a put"
            break
        if leg.option_type != OptionRight.PUT:
            continue
        cost = put_assignment_cost(leg.strike, abs(leg.net_qty), multiplier)
        if cost is None:
            assignment = None
            break
        assignment += cost

    max_loss = (None if max_loss_per_contract is None
                else float(max_loss_per_contract) * float(quantity))
    return CandidateStructure(
        underlying=underlying,
        strategy=option_strategy,
        max_loss=max_loss,
        notional=metrics.notional,
        short_put_assignment=assignment,
        is_defined_risk=is_defined_risk,
    )


# ---------------------------------------------------------------------------
# pending charges: admitted this cycle, not yet visible in the book
# ---------------------------------------------------------------------------
@dataclass
class _PendingCharge:
    transaction_id: Optional[int]
    candidate: CandidateStructure


#: A hard ceiling on in-flight charges per sleeve. Nothing should ever approach it (the
#: concurrent-structure rail caps the book long before), but a charge whose transaction id
#: could not be read is untrackable, and untrackable charges that could accumulate forever
#: would silently stop a sleeve trading. FIFO: the oldest goes first.
_PENDING_LIMIT = 50

_PENDING: Dict[Tuple[Optional[int], int], List[_PendingCharge]] = {}


def reset_pending_charges() -> None:
    """Forget every sleeve's in-flight charges. Tests, and a fresh backtest."""
    _PENDING.clear()


def pending_charges(expert_instance_id: int) -> Tuple[CandidateStructure, ...]:
    return tuple(p.candidate for p in _PENDING.get(_sleeve_key(expert_instance_id), ()))


def record_submitted(expert_instance_id: int, transaction_id: Optional[int],
                     candidate: CandidateStructure) -> None:
    """Remember what was just put on the wire so the NEXT candidate is charged for it."""
    key = _sleeve_key(expert_instance_id)
    charges = _PENDING.setdefault(key, [])
    charges.append(_PendingCharge(transaction_id, candidate))
    if len(charges) > _PENDING_LIMIT:
        del charges[:-_PENDING_LIMIT]


def _live_transaction_ids(expert_instance_id: int) -> Optional[set]:
    """The ids of this expert's transactions that still exist and could still open.

    ``None`` when the ledger could not be read -- and an unreadable ledger must NOT be
    treated as "everything was cancelled", because that would forgive every in-flight
    charge and re-open the very hole the charges exist to close.
    """
    from ba2_common.core.trade_store import transactions_where
    from ba2_common.core.types import TransactionStatus

    try:
        rows = transactions_where(expert_id=expert_instance_id,
                                  statuses=[TransactionStatus.WAITING,
                                            TransactionStatus.OPENED])
    except Exception as e:  # noqa: BLE001 -- any ledger failure is the same fact
        logger.error(f"Option RM: could not read the open transactions for expert "
                     f"{expert_instance_id} ({e}) -- in-flight charges are kept, which is "
                     f"the conservative reading", exc_info=True)
        return None
    return {t.id for t in rows}


def _prune_pending(expert_instance_id: int,
                   visible: Sequence[OptionStructure]) -> List[CandidateStructure]:
    """Drop charges the book can now see, and charges whose order never became a position.

    Two reasons a charge stops being owed, and they are different facts:

    * its transaction is OPENED and ``book_totals`` now counts it -- keeping the charge as
      well would double-charge the sleeve and stop it trading;
    * its transaction is gone or resolved (a rejected or cancelled entry) -- it never became
      exposure, and a charge for it would block the sleeve for the life of the process.

    A charge whose transaction id was never captured is KEPT (it really was submitted); the
    FIFO ``_PENDING_LIMIT`` is what stops those accumulating.
    """
    key = _sleeve_key(expert_instance_id)
    charges = _PENDING.get(key)
    if not charges:
        return []
    seen = {s.transaction_id for s in visible}
    live = _live_transaction_ids(expert_instance_id)
    kept: List[_PendingCharge] = []
    for charge in charges:
        if charge.transaction_id is None:
            kept.append(charge)
            continue
        if charge.transaction_id in seen:
            continue
        if live is not None and charge.transaction_id not in live:
            continue
        kept.append(charge)
    if kept:
        _PENDING[key] = kept
    else:
        _PENDING.pop(key, None)
    return [c.candidate for c in kept]


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OptionEntryVerdict:
    """May this ONE option entry be submitted, and if not, which rail said no.

    ``phrase`` is empty on an admission and one of the stable, greppable refusal phrases
    otherwise (design §9): a refusal that carries only free text is invisible to every log
    grep and every UI filter.
    """
    allowed: bool
    phrase: str
    reason: str
    detail: str
    candidate: Optional[CandidateStructure] = None
    verdict: Optional[RailVerdict] = None

    @property
    def message(self) -> str:
        return f"{self.phrase}: {self.detail}" if self.phrase else self.detail


# ---------------------------------------------------------------------------
# the journal: what the RM decided, for the run record
# ---------------------------------------------------------------------------
#: Bounded on purpose: a backtest evaluates tens of thousands of entries in one process and
#: an unbounded journal would be a slow memory leak nobody attributed to the risk manager.
_JOURNAL_LIMIT = 500
_JOURNAL: Dict[Tuple[Optional[int], int], Deque[Dict[str, Any]]] = {}


def journal(expert_instance_id: int) -> Tuple[Dict[str, Any], ...]:
    return tuple(_JOURNAL.get(_sleeve_key(expert_instance_id), ()))


def reset_journal() -> None:
    _JOURNAL.clear()


def _journal_entry(expert_instance_id: int, verdict: OptionEntryVerdict,
                   underlying: str, option_strategy: str, quantity: int) -> None:
    entries = _JOURNAL.setdefault(_sleeve_key(expert_instance_id),
                                  deque(maxlen=_JOURNAL_LIMIT))
    entries.append({
        "action_type": f"option_rm:{option_strategy}",
        "success": verdict.allowed,
        "status": "success" if verdict.allowed else "error",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "arguments": {"symbol": underlying, "option_strategy": option_strategy,
                      "quantity": quantity},
        "result": {"symbol": underlying, "allowed": verdict.allowed,
                   "rail": verdict.reason, "phrase": verdict.phrase,
                   "detail": verdict.detail},
    })


def reset_state() -> None:
    """Forget EVERY thread's breaker latches, pending charges, journals and mode warnings.

    Process-wide, deliberately: this is the test fixture's and the operator's reset, the one
    that must leave nothing behind anywhere. A **running trial must never call it** -- see
    :func:`reset_thread_state`, which is what a backtest run boundary uses.
    """
    reset_breaker_states()
    reset_pending_charges()
    reset_journal()
    reset_mode_warnings()


def _clear_this_threads_keys(store: Dict[Tuple[Optional[int], int], Any]) -> None:
    """Drop the entries filed under ``(this thread's id, ...)`` and nothing else."""
    me = threading.get_ident()
    for key in [k for k in store if k[0] == me]:
        del store[key]


def reset_thread_state() -> None:
    """Forget only what THIS thread filed. The backtest run boundary's reset.

    A backtest keys its sleeve state per THREAD (see :func:`_sleeve_key`) precisely so
    concurrent GA trials cannot see each other. ``reset_state`` -- a bare ``.clear()`` on
    three process-wide dicts -- then undoes that at every run boundary: with
    ``--parallel > 1`` the FIRST trial to finish wipes the breaker latches, the in-flight
    charges and the journals of every sibling still running, and those siblings then trade
    on from a sleeve the rails believe is empty and un-halted. Scoping the clear to this
    thread's own keys is what makes the per-thread key mean anything.

    TWO KINDS OF KEY ARE DELIBERATELY LEFT ALONE:

    * ``(other thread id, expert)`` -- another trial's state, the whole point;
    * ``(None, expert)`` -- the LIVE keys. A backtest thread must never clear live breaker
      latches, and it would: this runs from ``backtest_trading_db``'s ``finally``, AFTER the
      in-memory trade store has been exited, so ``_sleeve_key`` would answer ``(None, ...)``
      here. The scope is therefore read off the KEY SHAPE, never off the ambient store.

    Consequence, stated rather than hidden: a FILE-backed run (``in_memory=False``, the
    persisted top-N re-runs) never activates the in-memory trade store, so its sleeve state
    is filed under the live ``(None, expert)`` keys and this reset does not touch it. Those
    runs already shared one process-wide key with each other and with live; giving a
    backtest permission to wipe live latches to fix that would be the worse trade.
    """
    _clear_this_threads_keys(_BREAKER_STATE)
    _clear_this_threads_keys(_PENDING)
    _clear_this_threads_keys(_JOURNAL)


# ---------------------------------------------------------------------------
# THE gate
# ---------------------------------------------------------------------------
def admit_option_entry(*, expert, account, expert_instance_id: int,
                       underlying: str, option_strategy: str, legs: Sequence[Any],
                       quantity: int, max_loss_per_contract: Optional[float],
                       multiplier: int = 100,
                       stock_cover_price: Optional[float] = None) -> OptionEntryVerdict:
    """May this option entry reach the broker? The rails' first production caller.

    One caller, two runtimes: ``_OptionEntryAction._submit_option_order`` reaches this from
    the live ``TradeManager`` pass and from the backtest ``daily_engine``, through the same
    ``TradeActionEvaluator``.

    :param expert:   the expert instance, for its declared rails.
    :param account:  the sleeve's account, for equity and the cash that would fund delivery.
    :param max_loss_per_contract: what the submit path has already measured (§8.2) --
                     including the verified stock cover where one was supplied, which is why
                     this can be a number on a covered call whose ORDER stamps none (M3).
                     ``None`` is UNBOUNDED or UNMEASURABLE and declines.
    :param stock_cover_price: the verified held-stock cover, when the submitting builder
                     supplied one (covered call; see ``candidate_from_entry``). Marks the
                     candidate COVERED so its measured max loss charges the deployment
                     cap, not the undefined-risk sub-cap.
    """
    rails, missing = rail_settings(expert)
    if missing:
        detail = (f"{option_strategy} on {underlying}: this expert runs the option risk "
                  f"manager but declares no {', '.join(missing)} — refusing the entry "
                  f"rather than substituting a default for a risk rail")
        verdict = OptionEntryVerdict(False, OPTION_RAILS_UNCONFIGURED_REFUSAL,
                                     "rails_unconfigured", detail)
        logger.error(f"Option RM: {detail}")
        _journal_entry(expert_instance_id, verdict, underlying, option_strategy, quantity)
        return verdict

    candidate = candidate_from_entry(
        underlying=underlying, option_strategy=option_strategy, legs=legs,
        quantity=quantity, max_loss_per_contract=max_loss_per_contract,
        multiplier=multiplier, stock_cover_price=stock_cover_price)

    structures, unbuildable = sleeve_structures(expert_instance_id)
    book = sleeve_book_from(structures, unbuildable)
    charged = _prune_pending(expert_instance_id, structures)

    equity = sleeve_equity(account, expert_instance_id)
    cash = assignment_cash(account, expert_instance_id)
    breaker = get_breaker_state(expert_instance_id)

    # ``admit`` rather than ``check_rails``: the running sleeve must include what this cycle
    # has ALREADY put on the wire but the book cannot see yet. Three 20k structures do not
    # all fit under a 40k cap just because each one fits against an empty book.
    verdicts = admit(list(charged) + [candidate], book, equity, rails, breaker, cash)
    for prior, rail_verdict in zip(charged, verdicts[:-1]):
        if not rail_verdict.allowed:
            # A charge that no longer fits means the sleeve is already beyond its rails
            # (equity fell, or the book grew). Dropping its charge to make room for the new
            # candidate would be the fail-OPEN reading of the same fact.
            detail = (f"{option_strategy} on {underlying}: a structure already submitted "
                      f"this cycle ({prior.strategy} on {prior.underlying}) no longer fits "
                      f"the sleeve rails ({rail_verdict.reason}: {rail_verdict.detail}) — "
                      f"refusing to add to a sleeve that is already over its limits")
            out = OptionEntryVerdict(False, OPTION_RAIL_REFUSAL, rail_verdict.reason,
                                     detail, candidate, rail_verdict)
            logger.warning(f"Option RM: {detail}")
            _journal_entry(expert_instance_id, out, underlying, option_strategy, quantity)
            return out

    rail_verdict = verdicts[-1]
    if rail_verdict.allowed:
        out = OptionEntryVerdict(True, "", RAIL_OK, rail_verdict.detail, candidate,
                                 rail_verdict)
        logger.info(f"Option RM: admitted {option_strategy} on {underlying} "
                    f"({rail_verdict.detail})")
    else:
        detail = (f"{option_strategy} on {underlying}: {rail_verdict.reason} — "
                  f"{rail_verdict.detail}")
        out = OptionEntryVerdict(False, OPTION_RAIL_REFUSAL, rail_verdict.reason, detail,
                                 candidate, rail_verdict)
        logger.warning(f"Option RM: {detail}")
    _journal_entry(expert_instance_id, out, underlying, option_strategy, quantity)
    return out


def sleeve_book_from(structures: Sequence[OptionStructure],
                     unbuildable: Sequence[int]) -> BookTotals:
    """``book_totals`` over already-built structures, with unbuildable ones folded in."""
    totals = book_totals(structures)
    if not unbuildable:
        return totals
    blind = totals.unmeasurable + tuple(
        f"transaction {tid}: OPENED option position with no executed option order row — it "
        f"cannot be netted or priced, so the sleeve's committed capital is unmeasurable"
        for tid in unbuildable)
    return BookTotals(None, None, None, None, None,
                      totals.structure_count + len(unbuildable),
                      totals.underlyings, blind)


def sleeve_equity(account, expert_instance_id: int) -> Optional[float]:
    """The balance the sleeve sizes against, or ``None`` — never a fabricated 0.0.

    ``None`` DECLINES in ``check_rails``; that is the one thing ``_within_rails`` already got
    right and it is kept.
    """
    try:
        return account.get_balance()
    except Exception as e:  # noqa: BLE001 — any broker failure is the same fact
        logger.error(f"Option RM: could not read the balance for expert "
                     f"{expert_instance_id} ({e}) — declining the entry", exc_info=True)
        return None


def assignment_cash(account, expert_instance_id: int) -> Optional[float]:
    """Cash that could fund delivery on every short put the sleeve would then hold.

    ``OptionsAccountInterface.cash_available_for_delivery`` is the SINGLE definition, already
    used by the account-wide ``assignment_capacity`` gate. It is deliberately NOT buying
    power net of the reserve pool: the pool has already subtracted the CSP strikes this
    total is charging, and netting the two double-charges the same cash.
    """
    reader = getattr(account, "cash_available_for_delivery", None)
    if not callable(reader):
        return None
    try:
        return reader()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Option RM: could not read the cash available for delivery for "
                     f"expert {expert_instance_id} ({e}) — the assignment rail declines",
                     exc_info=True)
        return None


# ---------------------------------------------------------------------------
# the run record
# ---------------------------------------------------------------------------
#: What a ``classic_options`` run stores where the runs table reads ``model_used``. The runs
#: table filters on expert and status, so an option run needs no UI work to appear — it
#: needed a record, and until now nothing could write one because ``risk_manager_mode``
#: admitted only ``classic`` and ``smart``.
OPTION_RUN_MODEL = RISK_MANAGER_MODE_CLASSIC_OPTIONS


def flush_option_rm_run(expert_instance_id: int, account_id: int,
                        started_at: Optional[datetime] = None) -> Optional[int]:
    """Write this expert's option-RM pass as a run record and clear the journal.

    Same record shape the runs table already reads (``SmartRiskManagerJob``): the decisions
    go into ``graph_state["actions_log"]`` in the shape the renderer expects, so an option
    run shows up under the existing expert/status filter with no UI change.

    Returns the new row id, or ``None`` when there was nothing to record. **Skipped entirely
    under the backtest's in-memory trade store**: a backtest has no runs table to read and
    tens of thousands of passes to write, and a shared implementation that wrote rows during
    a grid would put grid noise into the live database.
    """
    from ba2_common.core.trade_store import inmem_trades_active

    entries = list(_JOURNAL.pop(_sleeve_key(expert_instance_id), ()))
    if not entries:
        return None
    if inmem_trades_active():
        return None

    from ba2_common.core.db import add_instance
    from ba2_common.core.models import SmartRiskManagerJob

    started = started_at or datetime.now(timezone.utc)
    admitted = sum(1 for e in entries if e.get("success"))
    refused = len(entries) - admitted
    summary = (f"Option risk manager: {admitted} entr{'y' if admitted == 1 else 'ies'} "
               f"admitted, {refused} refused by the sleeve rails")
    job = SmartRiskManagerJob(
        expert_instance_id=expert_instance_id,
        account_id=account_id,
        run_date=started,
        model_used=OPTION_RUN_MODEL,
        user_instructions="",
        graph_state={"actions_log": entries, "final_summary": summary},
        actions_taken_count=admitted,
        actions_summary=summary,
        status="COMPLETED",
    )
    return add_instance(job)


# Guard the two phrases at import: a typo'd constant would otherwise surface as an
# unfilterable free-text refusal months later.
validate_refusal_phrase(OPTION_RAIL_REFUSAL)
validate_refusal_phrase(OPTION_RAILS_UNCONFIGURED_REFUSAL)
