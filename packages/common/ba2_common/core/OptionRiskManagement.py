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
point all nineteen option builders end at. Wiring it there arms the live pass
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
module-level map keyed by expert instance, here, so the **entry** gate and the **transition**
pass read one latch. ``update_sleeve_breaker`` below owns the transitions; the entry gate
only READS. That asymmetry is deliberate and load bearing — ``update_breaker`` reports
``tripped`` as an *edge*, so an entry gate that also updated the state could consume the edge
on a bar where the transition pass had not yet run, and the flatten would never be signalled
at all. A process restart forgets the peak (persisting it needs a migration); that is stated
rather than hidden.

The breaker transitions in BOTH runtimes, through one function
---------------------------------------------------------------
``update_sleeve_breaker`` is that function. Live reaches it from
``option_lifecycle_service`` (the exit pass, on the ``JobManager`` schedule); the backtest
reaches it from ``daily_engine``'s per-bar flow, behind the SAME
``option_risk_manager_enabled`` check the entry gate dispatches on, so an equity trial makes
zero calls. Until 2026-09-01 the live pass was the only caller, so a backtest never ratcheted
the peak, never tripped and never cleared: ``RAIL_BREAKER_HALTED`` was unreachable there and
a ``classic_options`` backtest was systematically MORE PERMISSIVE than live.

What made that fix a ruling rather than a refactor: **what is the option sleeve's equity?**
``sleeve_equity`` used to call ``account.get_balance()``, which does not mean the same thing
on the two sides — ``AlpacaAccount.get_balance`` returns account EQUITY, while
``BacktestAccount.get_balance`` returns spendable CASH (``_cash``, capped). A drawdown
breaker measured on cash trips on DEPLOYING capital and clears on CLOSING a position,
irrespective of P&L, and the same mismatch was already the denominator of
``max_deployment_pct`` and ``max_notional_leverage``. The operator's ruling (2026-09-01) is
``account.get_account_snapshot().equity`` in both runtimes — see ``sleeve_equity``.

That settled the SIZING question. The review then found that it is the wrong answer to the
BREAKER's question, for one reason: on ``BacktestAccount`` the snapshot is
``deployed_equity() = min(the configured cap, cash + mark-to-market)``, and that clamp is
ONE-SIDED.
It compresses peaks and never troughs, so a 50k-capped account falling 100k -> 64k — a true
-36% — reports 50k on both bars, 0.0% drawdown, no stand-down, while the identical path live
(no cap) stands the sleeve down at -20%. A sizer must respect the cap; a loss measurement
must not. So there are two questions and two readers: ``sleeve_equity`` (capped, for
``max_deployment_pct`` / ``max_notional_leverage``) and ``sleeve_true_equity`` (uncapped, for
the breaker and nothing else). It is still ONE breaker function over ONE store — the runtime
difference lives in the account's own answer, ``ReadOnlyAccountInterface.true_equity``, which
every real broker answers with that same snapshot field.

The exit/servicing half (profit capture, tested delta, roll-DTE, the stops) is live-only for
a different and deliberate reason: a backtest expresses those as ``close_option`` exit rules
the GA searches, which is the engine's own settlement machinery, not a fork of this one.

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
    rearm, update_breaker,
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

#: Instances already told they declare no ``circuit_breaker_pct``. Same reason, and worse:
#: ``update_sleeve_breaker`` runs once per BAR, so the un-deduplicated line would be one per
#: bar for the length of a backtest.
_WARNED_UNDECLARED_BREAKER: set = set()


def reset_mode_warnings() -> None:
    """Forget which unadmitted modes have been warned about. Tests, and a fresh process."""
    _WARNED_UNADMITTED_MODES.clear()
    _WARNED_UNDECLARED_BREAKER.clear()


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
#: The sleeve's drawdown circuit breaker, in percent of the peak. ``update_breaker``
#: ``_require``s it and ``update_sleeve_breaker`` reads it; it is a REQUIRED rail because the
#: entry gate consults the latch that setting produces, in both runtimes (2026-09-01). A
#: sleeve that declares no breaker has a latch that can never trip, which is not a breaker.
BREAKER_SETTING = "circuit_breaker_pct"

#: Read on EVERY candidate. ``option_book.check_rails`` raises on a missing one; this module
#: refuses one step earlier so the operator sees which knob is missing in the UI.
REQUIRED_RAIL_SETTINGS: Tuple[str, ...] = (
    "max_concurrent_structures", "max_deployment_pct", "max_notional_leverage",
    BREAKER_SETTING,
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

    ``circuit_breaker_pct`` joined ``REQUIRED_RAIL_SETTINGS`` on 2026-09-01, when the breaker
    started transitioning in BOTH runtimes. The entry gate consults the latch that setting
    produces (``RAIL_BREAKER_HALTED``), so an undeclared breaker is a latch that can never
    trip — an entry rail that is silently absent, which is the exact shape of the defect M1
    was about. It refuses the entry by name like the other three. The remaining lifecycle
    thresholds (``profit_capture_pct``, ``roll_dte``, ``tested_delta_enabled``,
    ``dr_stop_enabled``, ``ur_stop_enabled``) are declared on ``MarketExpertInterface`` with
    no default for the same reason but are NOT rails: nothing on the ENTRY path reads them,
    the live exit pass already refuses to manage a sleeve missing any of them by name, and a
    backtest expresses its exits as the strategy's own ``close_option`` rules. Requiring them
    to open a position would be a rail that measures nothing.
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


#: ONE lock over ALL THREE stores, held by every writer and by the key scan that clears a
#: thread's own keys. It exists because ``_clear_this_threads_keys`` has to ITERATE the store
#: to find its keys, and under ``--parallel > 1`` a sibling trial's insert lands inside that
#: iteration: CPython then raises ``RuntimeError: dictionary changed size during iteration``
#: out of ``reset_thread_state`` -- which is the FIRST statement of ``backtest_trading_db``'s
#: ``finally``, so the raise also skipped ``clear_threadlocal_db()`` and left the finishing
#: trial's DB override installed on that worker thread (review finding, 2026-09-01).
#:
#: ONE lock rather than three: the three stores are keyed alike, cleared together, and the
#: contention is nil (a handful of dict operations per bar), so three locks would buy nothing
#: and add an ordering rule to get wrong. It is an ``RLock`` because the guarded functions
#: legitimately call one another -- ``reset_state`` -> ``reset_breaker_states``,
#: ``update_sleeve_breaker`` -> ``set_breaker_state``.
#:
#: The alternative considered and rejected was a per-thread key REGISTRY popped without
#: iterating. It removes the scan but not the need for a lock: the registry is itself shared
#: mutable state, every writer would have to maintain it in step with the store it mirrors,
#: and two structures that must agree about which keys exist is a second bug waiting for the
#: first writer that forgets one. The scan is O(keys) on a dict of at most a few hundred
#: sleeve keys, once per RUN, so it costs nothing worth that risk.
#:
#: READS are deliberately NOT guarded: ``get_breaker_state`` is consulted once per candidate
#: and is a single ``dict.get``, which is atomic under the GIL -- it cannot observe a torn
#: state, only a slightly stale one, and it could observe a stale one with the lock too. The
#: cold readers that iterate a per-key container (``journal``, ``pending_charges``) ARE
#: guarded, because iterating a deque or list while a sibling appends is the same defect one
#: level down.
_STATE_LOCK = threading.RLock()

_BREAKER_STATE: Dict[Tuple[Optional[int], int], BreakerState] = {}


def get_breaker_state(expert_instance_id: int) -> BreakerState:
    """The sleeve's breaker as it stands. A sleeve nobody has evaluated is un-halted.

    Lock-free on purpose: one atomic ``dict.get`` (see ``_STATE_LOCK``), on the hot path.
    """
    return _BREAKER_STATE.get(_sleeve_key(expert_instance_id), BreakerState())


def set_breaker_state(expert_instance_id: int, state: BreakerState) -> None:
    """Store the state ``option_book.update_breaker`` just produced.

    ONLY ``update_sleeve_breaker`` calls this (live's exit pass and the backtest's per-bar
    flow both reach it through that one function). The entry gate reads; it must never write,
    because ``tripped`` is an edge and a consumer that is not the flatten would swallow it.
    """
    if not isinstance(state, BreakerState):
        raise TypeError(
            f"set_breaker_state requires a BreakerState, got {type(state).__name__}")
    key = _sleeve_key(expert_instance_id)
    with _STATE_LOCK:
        _BREAKER_STATE[key] = state


def reset_breaker_states() -> None:
    """Forget every sleeve's breaker. Tests, and an operator starting clean."""
    with _STATE_LOCK:
        _BREAKER_STATE.clear()


def rearm_breaker(expert_instance_id: int) -> BreakerState:
    """Clear a stand-down UNCONDITIONALLY (``option_book.rearm``): the operator override.

    Nothing calls this automatically — it re-risks a sleeve that has not recovered. The peak
    is deliberately KEPT, so the drawdown that caused the stand-down is not erased.
    """
    key = _sleeve_key(expert_instance_id)
    with _STATE_LOCK:
        state = rearm(_BREAKER_STATE.get(key, BreakerState()))
        _BREAKER_STATE[key] = state
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
    #: The price of the FIRST executed fill on each contract -- the one that OPENED it. Not
    #: ``meta``'s (which keeps the last row seen, so after a buy-back it would be the CLOSING
    #: price), and not the structure's net premium, which after a roll is a year-old LEAPS
    #: debit mixed with several overlays' credits. The overlay's buyback trigger divides by
    #: this number, so it has to be the credit that overlay actually collected.
    entry_price: Dict[str, float] = {}
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
        if contract not in entry_price:
            entry_price[contract] = abs(float(order.open_price))
        total_cash += -sign * float(order.open_price) * qty

    legs = tuple(
        LifecycleLeg(
            contract_symbol=contract,
            net_qty=net[contract],
            strike=getattr(meta[contract], "strike", None),
            option_type=getattr(meta[contract], "option_type", None),
            expiry=getattr(meta[contract], "expiry", None),
            underlying=getattr(meta[contract], "underlying_symbol", None),
            entry_premium=entry_price.get(contract),
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
    with _STATE_LOCK:
        _PENDING.clear()


def pending_charges(expert_instance_id: int) -> Tuple[CandidateStructure, ...]:
    key = _sleeve_key(expert_instance_id)
    with _STATE_LOCK:
        return tuple(p.candidate for p in _PENDING.get(key, ()))


def record_submitted(expert_instance_id: int, transaction_id: Optional[int],
                     candidate: CandidateStructure) -> None:
    """Remember what was just put on the wire so the NEXT candidate is charged for it."""
    key = _sleeve_key(expert_instance_id)
    with _STATE_LOCK:
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

    THE LEDGER READ HAPPENS OUTSIDE ``_STATE_LOCK``, and the store is then edited by REMOVING
    the charges this pass decided against rather than by overwriting the list wholesale.
    ``_live_transaction_ids`` issues a SELECT; holding a lock every writer needs across a
    database round trip would serialise the sleeve on I/O for no benefit. The consequence of
    dropping the lock in between is that a sibling worker thread may ``record_submitted`` a
    NEW charge while this read is in flight, and an overwrite would silently discard it -- the
    charge would be forgotten and the structure never counted. Splicing by identity leaves it
    in place. The value RETURNED is still what this pass measured: a charge that arrived after
    the ledger read was never tested against it, and charging the current candidate for it
    would be a number this evaluation cannot justify. It is charged on the next candidate.
    """
    key = _sleeve_key(expert_instance_id)
    with _STATE_LOCK:
        charges = list(_PENDING.get(key, ()))
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
    dropped = {id(c) for c in charges} - {id(c) for c in kept}
    with _STATE_LOCK:
        current = _PENDING.get(key, ())
        remaining = [c for c in current if id(c) not in dropped]
        if remaining:
            _PENDING[key] = remaining
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
    key = _sleeve_key(expert_instance_id)
    with _STATE_LOCK:
        return tuple(_JOURNAL.get(key, ()))


def reset_journal() -> None:
    with _STATE_LOCK:
        _JOURNAL.clear()


def _journal_entry(expert_instance_id: int, verdict: OptionEntryVerdict,
                   underlying: str, option_strategy: str, quantity: int) -> None:
    key = _sleeve_key(expert_instance_id)
    entry = {
        "action_type": f"option_rm:{option_strategy}",
        "success": verdict.allowed,
        "status": "success" if verdict.allowed else "error",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "arguments": {"symbol": underlying, "option_strategy": option_strategy,
                      "quantity": quantity},
        "result": {"symbol": underlying, "allowed": verdict.allowed,
                   "rail": verdict.reason, "phrase": verdict.phrase,
                   "detail": verdict.detail},
    }
    with _STATE_LOCK:
        _JOURNAL.setdefault(key, deque(maxlen=_JOURNAL_LIMIT)).append(entry)


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
    """Drop the entries filed under ``(this thread's id, ...)`` and nothing else.

    UNDER ``_STATE_LOCK``, because the scan for those keys has to iterate a dict that sibling
    GA trial threads are writing to. Unlocked, a sibling's ``set_breaker_state`` landing
    inside the comprehension raised ``RuntimeError: dictionary changed size during
    iteration`` out of :func:`reset_thread_state` -- and that runs first in
    ``backtest_trading_db``'s ``finally``, so the raise ALSO skipped
    ``clear_threadlocal_db()`` and left the finished run's DB override installed on the
    worker thread. Pinned by ``test_option_rm_state_is_thread_safe.py``.
    """
    me = threading.get_ident()
    with _STATE_LOCK:
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
    # THREE separate acquisitions, one per store, and deliberately not one around all three.
    # A sibling can only ever read or write keys under ITS OWN thread id, so it cannot
    # observe this thread half-cleared; making the boundary atomic across the three stores
    # would buy nothing and would put the lock in two places, leaving the one that actually
    # guards the iteration (``_clear_this_threads_keys``) removable without a test noticing.
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
    # Keyword-constructed on purpose: BookTotals carries FIVE consecutive Optional[float]
    # money fields, so a positional call is five unlabelled Nones that any field reorder
    # would silently permute into the wrong slots.
    return BookTotals(committed=None, naked_committed=None, notional=None,
                      premium_outlay=None, short_put_assignment=None,
                      structure_count=totals.structure_count + len(unbuildable),
                      underlyings=totals.underlyings, unmeasurable=blind)


def sleeve_equity(account, expert_instance_id: int) -> Optional[float]:
    """The equity the sleeve sizes against, or ``None`` — never a fabricated 0.0.

    ``None`` DECLINES in ``check_rails``; that is the one thing ``_within_rails`` already got
    right and it is kept.

    ONE DEFINITION, BOTH RUNTIMES: ``AccountSnapshot.equity`` -- "cash plus positions marked
    to market" (its own contract). Operator ruling, 2026-09-01. It replaces
    ``account.get_balance()``, which was NOT one definition:

      * ``AlpacaAccount.get_balance``   -> ``TradeAccount.equity``  (account EQUITY)
      * ``BacktestAccount.get_balance`` -> ``self._cash``, or ``min(cash, deployed_equity())``
                                           under an equity cap  (spendable CASH)

    THE SIZING QUESTION ONLY. This is *how many dollars may the sleeve deploy*, and the
    answer is deliberately the CAPPED one on a backtest. The breaker asks a different
    question -- *how much has the sleeve lost from its peak* -- and reads
    :func:`sleeve_true_equity` instead, because the cap is one-sided and would mask the very
    drawdown the breaker exists to catch. Read that function before moving either caller.

    So the rails' denominator was cash in a backtest and equity live, and a drawdown breaker
    built on it would have tripped on DEPLOYING capital and cleared on CLOSING a position
    regardless of P&L. ``get_account_snapshot().equity`` is the same CONCEPT on both sides:
    Alpaca overrides it and maps ``TradeAccount.equity``; ``BacktestAccount`` inherits
    ``ReadOnlyAccountInterface``'s tolerant probe, which reads ``get_account_info()["equity"]``
    = ``deployed_equity()`` = ``min(cap, cash + mark-to-market of open positions)``. The cap is
    the seam every sizer already looks through, so the rails keep measuring the same dollars
    the sizer spends.

    THIS RE-BASES THE EXISTING RAILS. ``max_deployment_pct`` and ``max_notional_leverage``
    read this function, so in a BACKTEST their denominator moves from cash to equity. That is
    a ratification, not a refactor -- an option grid run before and after measures a different
    rail -- and it is acceptable now only because nothing selects the mode yet: no shipped
    expert spec does (``test_no_shipped_expert_spec_selects_a_risk_manager_mode``) and the
    settings dialog renders none of the sleeve rails, so there is no live ``classic_options``
    sleeve either.

    STILL ACCOUNT-WIDE, while the rail it feeds is per-sleeve -- the same approximation
    ``assignment_cash`` documents, and unchanged by this ruling: two ``classic_options``
    sleeves on one account each measure the whole account.
    """
    return _read_equity(lambda: getattr(account.get_account_snapshot(), "equity", None),
                        expert_instance_id, "the account snapshot's equity",
                        "declining the entry")


def sleeve_true_equity(account, expert_instance_id: int) -> Optional[float]:
    """The sleeve's TRUE equity, for the DRAWDOWN BREAKER only. Never fabricated.

    THE SECOND QUESTION (review finding, 2026-09-01). ``sleeve_equity`` above answers *how
    many dollars may this sleeve deploy*, and on ``BacktestAccount`` under a fixed-notional cap
    that is deliberately ``min(cap, cash + mark-to-market)`` -- the seam every sizer looks
    through. The breaker asks something else: *how much has this sleeve lost from its peak*,
    and the cap is the wrong instrument for that because it is ONE-SIDED. It compresses peaks
    and never troughs, so an account capped at 50k that falls 100k -> 64k (a true -36%)
    reports 50k on both bars, a 0.0% drawdown and no stand-down -- while the identical path
    live, where no cap exists, stands the sleeve down at -20%. The backtest was silently the
    more permissive runtime again, in the one rail whose whole job is to stop a loss.
    The backtest's equity-cap module states the same hazard for the metrics ("report zero P&L for every
    period spent above the cap") and ships ``capped_drawdown_curve`` instead of differencing
    the capped figure.

    STILL ONE SHARED FUNCTION AND ONE STORE. ``update_sleeve_breaker`` is unchanged in shape
    and is the only caller of this: the runtime difference lives in the ACCOUNT's answer to
    "what is your true equity" (``ReadOnlyAccountInterface.true_equity``, which every real
    broker answers with the SAME snapshot field this module already read, and which
    ``BacktestAccount`` overrides with its uncapped ``equity()``), never in forked breaker
    logic. Live behaviour is byte-identical: no cap exists there, so both readers return the
    same number.

    THE SIZING RAILS ARE NOT MOVED ONTO THIS. ``max_deployment_pct`` and
    ``max_notional_leverage`` keep reading ``sleeve_equity`` -- a sizer must respect the cap,
    or a capped backtest would deploy capital it does not have. Pinned in both directions:
    ``test_the_breaker_trips_at_the_TRUE_drawdown_on_a_capped_account`` and
    ``test_the_sizing_rails_still_read_the_CAPPED_equity``.

    ``None`` leaves the breaker BLIND (``update_breaker`` says so and refuses to report a
    drawdown it did not measure); it never becomes 0.0, which against a ratcheted peak is a
    measured 100% drawdown.
    """
    return _read_equity(lambda: account.true_equity(), expert_instance_id,
                        "the account's true (uncapped) equity",
                        "the drawdown breaker is blind this evaluation")


def _read_equity(read, expert_instance_id: int, what: str, consequence: str
                 ) -> Optional[float]:
    """One reader for both equity questions: read, refuse to invent, coerce, or ``None``.

    Shared so the two callers cannot drift on the part that is genuinely identical -- an
    unread, absent or non-numeric equity is UNKNOWN in both, and unknown must never become
    zero. What differs is only which question was asked and what ``None`` costs, and both
    are said in the log line rather than in a second copy of this body.
    """
    try:
        equity = read()
    except Exception as e:  # noqa: BLE001 — any broker failure is the same fact
        logger.error(f"Option RM: could not read {what} for expert {expert_instance_id} "
                     f"({e}) — {consequence}", exc_info=True)
        return None
    if equity is None:
        logger.error(f"Option RM: {what} for expert {expert_instance_id} is not published "
                     f"— {consequence}, rather than substituting a number the account did "
                     f"not state")
        return None
    try:
        return float(equity)
    except (TypeError, ValueError):
        logger.error(f"Option RM: {what} for expert {expert_instance_id} is non-numeric "
                     f"({equity!r}) — {consequence}")
        return None


def update_sleeve_breaker(*, expert, account,
                          expert_instance_id: int) -> Optional[BreakerState]:
    """Ratchet this sleeve's peak, test the drawdown, STORE the new latch. Two callers.

    THE shared transition (operator ruling, 2026-09-01). Live calls it from
    ``option_lifecycle_service.run_option_lifecycle_pass``; the backtest calls it from
    ``daily_engine``'s per-bar flow, behind the same ``option_risk_manager_enabled`` check
    the entry gate dispatches on. One implementation, one equity reader
    (``sleeve_true_equity``), one store — which is the whole content of "the breaker means
    the same thing in a backtest as it does live".

    IT READS ``sleeve_true_equity``, NOT ``sleeve_equity`` (review finding, 2026-09-01). The
    sizing reader is the CAPPED figure on a backtest, and a cap compresses peaks without
    compressing troughs, so a breaker built on it measures a drawdown that did not happen and
    misses the one that did. The rails that size keep the capped reader; the rail that
    measures a loss takes the account's true equity. Live the two are the same number.

    Returns the new state, or ``None`` when the sleeve declares no ``circuit_breaker_pct``.
    That case does NOT substitute a threshold and does not raise out of a per-bar loop: the
    same missing key already refuses every entry by name through ``rail_settings``
    (it is in ``REQUIRED_RAIL_SETTINGS``), so a sleeve with no declared breaker can open
    nothing to stand down from. It is logged ONCE per instance rather than once per bar.
    """
    try:
        pct = expert.get_setting_with_interface_default(BREAKER_SETTING, log_warning=False)
    except Exception:  # noqa: BLE001 — ValueError today; any lookup failure is "absent"
        pct = None
    if pct is None:
        if expert_instance_id not in _WARNED_UNDECLARED_BREAKER:
            _WARNED_UNDECLARED_BREAKER.add(expert_instance_id)
            logger.error(f"Option RM: expert instance {expert_instance_id} runs the option "
                         f"risk manager but declares no {BREAKER_SETTING} — the drawdown "
                         f"breaker is NOT evaluated, and every option entry is refused by "
                         f"name for the same reason")
        return None

    equity = sleeve_true_equity(account, expert_instance_id)
    # Ratchet on EVERY evaluation, including a flat sleeve: a just-flattened sleeve that
    # stopped tracking its peak would re-arm against a stale one.
    #
    # Read-ratchet-store under ONE acquisition of ``_STATE_LOCK`` (re-entrant, so
    # ``set_breaker_state`` nests freely): the peak is a running maximum, and two callers
    # interleaving read/write would let the lower of two concurrent equities overwrite the
    # higher and silently un-ratchet the peak. The equity read is deliberately OUTSIDE it --
    # it can reach a broker.
    with _STATE_LOCK:
        state = update_breaker(get_breaker_state(expert_instance_id), equity,
                               {BREAKER_SETTING: pct})
        set_breaker_state(expert_instance_id, state)
    return state


def assignment_cash(account, expert_instance_id: int) -> Optional[float]:
    """Cash that could fund delivery on every short put the sleeve would then hold.

    ``OptionsAccountInterface.cash_available_for_delivery`` is the SINGLE definition, already
    used by the account-wide ``assignment_capacity`` gate. It is deliberately NOT buying
    power net of the reserve pool: the pool has already subtracted the CSP strikes this
    total is charging, and netting the two double-charges the same cash.

    ACCOUNT-WIDE, WHILE THE RAIL IT FEEDS IS PER-SLEEVE, and ``expert_instance_id`` is
    therefore used only for the log line. That is deliberate and it is also an approximation
    worth naming: delivery cash is a property of the BROKER ACCOUNT, not of one expert's
    sleeve, so two classic_options sleeves on one account each measure the whole account's
    cash and can each admit a short put the account could not both fund. It is the SAFE
    direction for the single-sleeve case this branch ships (the same number the account-wide
    ``assignment_capacity`` gate already enforces downstream, so nothing here can admit past
    it) and the wrong one for a shared account. A real per-sleeve split needs a definition of
    what share of account cash a sleeve owns -- design work, not a comment -- and is flagged
    rather than guessed at here.
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
def _decision_from_journal_entry(entry: Dict[str, Any]):
    """One journal entry -> one ``risk_manager_run.decision`` row.

    The journal is keyed per ENTRY ATTEMPT, not per symbol: one pass can weigh two
    structures on the same underlying, and both get a row. ``symbol`` therefore is not
    unique within a run — the strategy that distinguishes them rides along as
    ``option_strategy``, the way the classic manager carries its ``side``.
    """
    from ba2_common.core.risk_manager_run import OUTCOME_FUNDED, OUTCOME_RAIL, decision

    args = entry.get("arguments") or {}
    result = entry.get("result") or {}
    symbol = args.get("symbol") or result.get("symbol") or "?"
    strategy = args.get("option_strategy") or ""
    quantity = args.get("quantity")
    if entry.get("success"):
        detail = result.get("detail") or "admitted by the sleeve rails"
        return decision(symbol, OUTCOME_FUNDED,
                        f"{strategy}: {detail}" if strategy else detail,
                        quantity=quantity, option_strategy=strategy,
                        rail=result.get("rail"), timestamp=entry.get("timestamp"))
    # ``reason`` names the RAIL that spoke, because "refused" on its own is the non-answer
    # the whole record exists to replace. ``quantity`` stays absent: a refused entry has no
    # size, and the size it ASKED for is a different fact, recorded under its own key.
    detail = result.get("detail") or result.get("phrase") or "refused by the sleeve rails"
    return decision(symbol, OUTCOME_RAIL, detail, option_strategy=strategy,
                    rail=result.get("rail"), phrase=result.get("phrase"),
                    requested_quantity=quantity, timestamp=entry.get("timestamp"))


def flush_option_rm_run(expert_instance_id: int, account_id: int,
                        started_at: Optional[float] = None) -> Optional[int]:
    """Write this expert's option-RM pass as a ``RiskManagerRun`` and clear the journal.

    THE RECORD SHAPE IS ``RiskManagerRun`` WITH ``mode=MODE_OPTIONS`` — the row the runs
    table's Options filter already queries (``marketanalysis._fetch_all_risk_manager_runs``,
    ``RiskManagerRun.mode == 'options'``). Review 2026-08-30 (dev-merge) FIX 2: this
    previously wrote a ``SmartRiskManagerJob`` with ``model_used="classic_options"``, a
    column no filter looks at — so the Options filter returned nothing and every option run
    surfaced mislabelled under **Smart**, beside LLM jobs it has nothing in common with.
    Both renderers were read before switching, and the ``RiskManagerRun`` one is strictly
    the better fit for this manager:

      * it has ``mode``, which is what the table filters and labels on — the whole
        handshake;
      * its detail view lists REFUSALS FIRST with a ``reason`` column, which is exactly
        what a rail decision is, where the smart renderer's per-action expansions are
        built for a LangGraph tool call (iteration, confidence, model, graph-state viewer)
        and would read as empty fields for a rail;
      * ``symbols_funded / symbols_received`` is the admitted-of-weighed count the smart
        shape could only express as a bare ``actions_taken_count``.

    Nothing the smart renderer shows is lost that this manager produces: it has no
    LangGraph state, no iterations, no model and no portfolio snapshot. Per-entry
    ``timestamp`` and ``option_strategy`` are carried on each decision row (persisted in
    the JSON beside the four columns the detail table renders). **One row, one table** —
    the ``SmartRiskManagerJob`` write is REPLACED, not duplicated, so a run can never be
    counted twice across the two sources the table unions.

    ``started_at`` is a ``time.monotonic()`` reading from the top of the pass, matching
    ``risk_manager_run.record_run`` and the classic manager: monotonic so the duration
    cannot jump when the system clock is adjusted mid-run.

    Returns the new row id, or ``None`` when there was nothing to record. **Skipped entirely
    under the backtest's in-memory trade store**: a backtest has no runs table to read and
    tens of thousands of passes to write, and a shared implementation that wrote rows during
    a grid would put grid noise into the live database. (``record_run`` refuses on the same
    seam; the check is repeated here so the journal is never drained by a call that was
    never going to write.)
    """
    from ba2_common.core.trade_store import inmem_trades_active

    # The BACKTEST check comes FIRST, before the pop. Popping and then declining to write
    # destroys the run record in the one case the caller was told nothing would be written,
    # so a backtest that later wanted ``journal()`` found it emptied by a function that had
    # explicitly done nothing. Reordering is free: both branches return None.
    if inmem_trades_active():
        return None
    key = _sleeve_key(expert_instance_id)
    with _STATE_LOCK:
        entries = list(_JOURNAL.pop(key, ()))
    if not entries:
        return None

    from ba2_common.core.risk_manager_run import MODE_OPTIONS, record_run

    breaker = get_breaker_state(expert_instance_id)
    # The run's sleeve-wide inputs, shown beside the per-entry decisions. The breaker is
    # here because a halted sleeve explains an ENTIRE run of refusals at once, where
    # repeating it per row would say the same thing N times.
    context = {
        "risk_manager_mode": RISK_MANAGER_MODE_CLASSIC_OPTIONS,
        "breaker_halted": breaker.halted,
        "breaker_blind": breaker.blind,
    }
    if breaker.detail:
        context["breaker_detail"] = breaker.detail

    # The MAPPING is guarded, not just the write. ``decision()`` RAISES on a row it cannot
    # explain (no symbol, no reason) and ``record_run``'s own guard starts after its
    # arguments are built, so an unmappable journal entry would escape this function --
    # which recording must never do: the entries it describes are already on the wire, and
    # the caller is a risk manager that has finished its real work. Same discipline as
    # ``TradeRiskManagement._record_classic_run``.
    try:
        decisions = [_decision_from_journal_entry(e) for e in entries]
    except Exception as e:  # noqa: BLE001 -- observability must not fail the entry pass
        logger.warning(f"Failed to build the option risk-manager run record for expert "
                       f"{expert_instance_id}: {e}")
        return None

    return record_run(expert_instance_id=expert_instance_id, account_id=account_id,
                      mode=MODE_OPTIONS, decisions=decisions,
                      context=context, started_at=started_at)


# Guard the two phrases at import: a typo'd constant would otherwise surface as an
# unfilterable free-text refusal months later.
validate_refusal_phrase(OPTION_RAIL_REFUSAL)
validate_refusal_phrase(OPTION_RAILS_UNCONFIGURED_REFUSAL)
