"""Portfolio Allocation page — manually traded accounts only.

Shows the account's current allocation, grouped by the instrument labels the user
chose to manage, and IS WHERE THE TARGETS ARE SET. Every decision this page makes
lives in the pure, unit-tested module
``ba2_trade_platform/ui/utils/portfolio_allocation_view.py``; this file only does
IO (broker + DB) and draws widgets.

THREE THINGS ARE EDITED INLINE, AND THAT IS THE POINT
=====================================================
Each label's PORTFOLIO target, each symbol's SHARE OF ITS LABEL, and the account's
cash RESERVE. All three used to be reachable only from inside the Allocate wizard,
which meant that on an account nobody had run the wizard on every label sat at
``target_pct = 0`` while the symbol table cheerfully printed a 20% share --
``get_symbol_weights``'s even-split default -- resolving to a target value of
$0.00, because 20% of a 0% label is nothing. The page showed a plausible number
that meant nothing.

The Allocate button is still here, and it is now for EXECUTING against the saved
targets: dry run, precheck, submit. Both sides write the same rows, and
``_load_flow_inputs`` re-reads them at the moment the wizard opens, so a fresh
inline edit is what the dry run solves with.

TWO DIFFERENT THINGS CALLED "TARGET"
====================================
A label's target is its share of the PORTFOLIO; a symbol's is its share of THAT
LABEL. Both numbers were right and neither said its denominator, which is why
"target 0.0%" over a column of 20s looked like a bug. The column is now
"Share of label %", the header says "portfolio target", and the ⓘ beside each
label reconciles the two.

Everything persists ON CHANGE, not behind a Save button: switching the global
account calls ``ui.run_javascript('window.location.reload()')``
(``ui/layout.py``), so the page never gets a chance to flush a pending edit. A
REFUSED edit is put back to the stored value rather than left on screen -- a
number the database does not have is the defect this feature exists to remove.
Symbols can be added to or removed from a label whether or not they have an open
position (but they do have to exist at the broker — ``symbols_exist``, or a typo
becomes a permanent global ``Instrument`` row), and a symbol carrying two managed
labels is allowed — it just gets a warning icon.

The over-100 rule is enforced HERE, without a dry run, exactly as the wizard's
step 1 always has: an inline label target that would take the set past 100 is
refused (``validate_label_target_edit``, on the engine's own tolerance and in the
engine's own words), and a running advisory plus a totals footer report the set as
it stands.

Eager persistence has ONE exception, and it is deliberate: unmanaging a label
DESTROYS its target, its comment and every per-symbol weight and comment under it,
with no undo, so a removal is confirmed first (``_confirm_unmanage``). Additions
still save immediately. For the same reason the picker's options are the UNION of
the selectable labels and the ones already managed: NiceGUI's ``Select`` silently
drops any selected value that is not an option, so a managed label missing from
the list would be reported as deselected the instant the picker was touched.

Two house rules are load-bearing here:

* ``get_positions()`` returning ``None`` means the broker fetch FAILED, while
  ``[]`` means genuinely flat. ``positions_by_symbol`` raises
  ``PositionFetchFailed`` on ``None`` and this page shows an error banner instead
  of pretending the account is empty.
* Prices come from ``get_instrument_current_price`` in ONE bulk, cached call, and
  work for symbols with no position. Alpaca's default feed is ``delayed_sip``
  (15 minutes delayed), which the page states next to the data.

This repo uses no ``ui.refreshable`` / ``ui.stepper`` / ``ui.aggrid``: refresh is
``container.clear()`` followed by rebuilding inside ``with container:``. Blocking
broker work goes through ``asyncio.to_thread``.

``...core.utils`` is imported INSIDE the functions that need it, never at module
scope: it is the split shim whose live half pulls the expert/LLM registries
(langchain_core, openai, torch, transformers — ~5s and ~10 heavy roots). That does
NOT make this module cheap to import — ``ui/pages/__init__.py`` imports
``overview``/``settings`` and pulls the same stack before this file's body even
runs, which is exactly why the pure logic lives in ``ui/utils/`` instead. It only
keeps the registries out of THIS module's own graph, so the deferral survives if
the package ``__init__`` is ever trimmed.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from nicegui import ui
from sqlmodel import select

from ...core import portfolio_allocation_service as svc
from ...core.db import get_db
from ...core.models import ExpertInstance
from ...core.portfolio_allocation import (
    ALLOCATION_MODE_INVEST_LABEL, ALLOCATION_MODE_REBALANCE,
    VALUATION_MODE_COST, VALUATION_MODE_MARKET,
    LabelTarget, SymbolTarget, build_base_snapshot, compute_allocation,
    compute_base_notional, compute_label_investment, current_value,
    unconsumed_income_notice,
)
from ...core.portfolio_allocation_store import (
    add_symbols_to_label, get_allocation_config, get_managed_labels, get_symbol_comments,
    get_previous_symbol_weights, get_symbol_rows, get_symbol_weights,
    remove_symbols_from_label, replace_managed_labels, save_allocation_targets,
    set_allocation_config, set_managed_label, set_symbol_weight,
)
from ...logger import logger
from ..account_filter_context import get_selected_account_id
from ..utils.portfolio_allocation_view import (
    DEFAULT_MACHINE_LABEL_FAMILIES, GATE_NO_ACCOUNT, LABEL_STATUS_CLASSES,
    LABEL_TOTAL_NOTICE_CLASSES, MARKET_SOURCE_UNAVAILABLE, NO_LABEL_COLOR,
    GateResult, ManagedLabel,
    PositionFetchFailed, account_value_card, account_value_from_snapshot,
    build_label_bars, build_label_views,
    collect_managed_symbols, diff_managed_labels,
    evaluate_gate, evaluate_market_gate, expert_shortname_families,
    format_allocation_footer, format_label_header, format_label_target_tooltip,
    format_label_total_notice, format_reserve_caption,
    format_reserve_row, label_color_options, managed_total_value,
    missing_quote_symbols, picker_options, positions_by_symbol,
    resolve_label_icon_color, sort_label_views, store_color_value,
    symbol_target_values,
    validate_label_target_edit, validate_reserve_edit, validate_symbol_weight_edit,
    working_orders_notice,
)
from .portfolio_allocation_wizard import (
    open_allocation_steps, open_allocation_wizard, render_income_panel, render_outcomes,
)

#: Quasar debounce (ms) for the comment inputs. Every keystroke used to run a
#: SELECT + UPDATE + commit + refresh on the NiceGUI event loop; the page's own
#: convention is that blocking work goes through ``asyncio.to_thread``, and this
#: keeps the number of those round trips down to one per pause in typing.
COMMENT_DEBOUNCE_MS = 600

#: Same, for the inline TARGET % boxes -- and here it does more than save round
#: trips. A rejected edit is put BACK (see ``_revert_symbol_cell``), and without a
#: debounce the "1" on the way to "15" would be judged, accepted and persisted as
#: 1% before the user finished the number.
TARGET_DEBOUNCE_MS = 600


# ---------------------------------------------------------------------------
# Blocking IO (always called through asyncio.to_thread)
# ---------------------------------------------------------------------------

def _enabled_expert_names(account_id: int) -> List[str]:
    """Display names of the account's ENABLED experts; empty list when there are none."""
    with get_db() as session:
        rows = session.exec(
            select(ExpertInstance).where(
                ExpertInstance.account_id == account_id,
                ExpertInstance.enabled == True,  # noqa: E712 — SQL boolean, not identity
            )
        ).all()
        return [(r.alias or r.expert) for r in rows]


def _load_gate(account_id: Optional[int]) -> GateResult:
    """Resolve the three gate inputs.

    An account that cannot be instantiated is reported as "not manual" rather than
    crashing the page — the user's next action (open Settings) is the same either way.
    """
    from ...core.utils import get_account_instance_from_id

    if account_id is None:
        return evaluate_gate(None, False, [])
    try:
        account = get_account_instance_from_id(account_id)
    except Exception as e:
        logger.error(f"Portfolio allocation: cannot load account {account_id}: {e}", exc_info=True)
        account = None
    if account is None:
        return evaluate_gate(account_id, False, [])
    manual = bool(account.get_setting_with_interface_default(
        'manual_trading_enabled', log_warning=False))
    return evaluate_gate(account_id, manual, _enabled_expert_names(account_id))


def _load_view_payload(account_id: int, valuation_mode: str) -> Dict[str, Any]:
    """One render's worth of data: managed labels, membership, positions, prices.

    ``valuation_mode`` is threaded through to ``build_label_views`` and echoed back
    in the payload so the render names the mode that produced the numbers next to
    them -- switching modes RE-COMPUTES rather than silently reinterpreting.

    ALSO reads the account snapshot, which this function did not do at all before
    W3: without buying power the page cannot show cash, cannot show the unallocated
    group requirement 3 asks for, and cannot put the current and target percentages
    under one denominator. The cost is bounded -- Alpaca caches the snapshot for 5s
    -- and a broker that will not answer costs the RESERVE ROW, not the page:
    ``base_notional`` and ``available_buying_power`` come back ``None`` and every
    figure derived from them is omitted rather than guessed.

    Raises:
        PositionFetchFailed: the broker position fetch failed (NOT a flat account).
        RuntimeError: the account could not be instantiated.
    """
    from ...core.utils import get_account_instance_from_id, get_symbols_by_label

    managed = [ManagedLabel(label=row.label, target_pct=row.target_pct,
                            comment=row.comment, color=row.color)
               for row in get_managed_labels(account_id)]
    symbols_by_label = get_symbols_by_label([m.label for m in managed])
    symbols = collect_managed_symbols(symbols_by_label)

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")

    positions = positions_by_symbol(account.get_positions())

    prices: Dict[str, Optional[float]] = {}
    if symbols:
        fetched = account.get_instrument_current_price(symbols)
        if isinstance(fetched, dict):
            prices = dict(fetched)
        else:
            logger.warning(f"Bulk price fetch returned {type(fetched).__name__}, "
                           f"expected a dict — rendering without prices")

    # ``positions_by_symbol`` does not populate ``price`` -- quotes come from the
    # bulk call above -- so stamp them on before anything measures a market value.
    # ``build_label_views`` builds its own priced copy either way; what needs this
    # is ``compute_base_notional`` below, which reads ``PositionState.price``
    # directly and would otherwise value the whole book at 0 in market mode.
    for symbol, state in positions.items():
        if state.price is None:
            state.price = prices.get(symbol)

    comments: Dict[tuple, str] = {}
    weights: Dict[str, Dict[str, float]] = {}
    for entry in managed:
        for symbol, text in get_symbol_comments(account_id, entry.label).items():
            comments[(entry.label, symbol)] = text
        weights[entry.label] = get_symbol_weights(
            account_id, entry.label, symbols_by_label.get(entry.label, []))

    buying_power = base_notional = account_value = None
    try:
        # ONE snapshot for both figures. A second ``get_account_snapshot()`` for
        # the account value would double the REST cost of every refresh -- and on
        # Alpaca that call is two endpoints, not one.
        snapshot = account.get_account_snapshot()
        # Extracted FIRST, so a failure in the base arithmetic below costs the
        # reserve row (as it always has) and not the account-value card as well.
        account_value = account_value_from_snapshot(snapshot)
        if snapshot is not None and snapshot.buying_power is not None:
            buying_power = float(snapshot.buying_power)
            # Exactly ``compute_base_notional``'s rule, which is what the wizard's
            # ``build_base_snapshot`` uses: buying power plus the DISTINCT managed
            # value under the active mode. Anything else and the page's percentages
            # would divide by a different base from the plan's.
            base_notional = compute_base_notional(buying_power, positions, symbols,
                                                  valuation_mode=valuation_mode)
    except Exception as e:
        # The label table is this page's job; buying power is a bonus on top of it.
        # None propagates and the reserve row is simply not drawn -- better a
        # missing row than one measured against a guessed base.
        logger.warning(f"Account snapshot unavailable for account {account_id}: {e}; "
                       f"the unallocated group and the % of base will be omitted")

    # The stored reserve, read from the SAME config row as the valuation mode. It
    # scales the TARGET money the page shows so those figures match the ones the
    # dry run will solve with; every "current" figure keeps dividing the gross base.
    unallocated_pct = float(get_allocation_config(account_id).unallocated_pct or 0.0)

    return {
        'views': build_label_views(managed, symbols_by_label, positions, prices, comments,
                                   valuation_mode=valuation_mode,
                                   base_notional=base_notional,
                                   symbol_weights=weights,
                                   unallocated_pct=unallocated_pct),
        'symbols_by_label': symbols_by_label,
        'valuation_mode': valuation_mode,
        'base_notional': base_notional,
        'available_buying_power': buying_power,
        # The account's OWN value (net liquidating value), as distinct from the
        # managed positions' market value. ``None`` is UNKNOWN, never 0.0.
        'account_value': account_value,
        'unallocated_pct': unallocated_pct,
    }


def _load_valuation_mode(account_id: int) -> str:
    """The account's stored valuation mode, creating the config row on first use."""
    return get_allocation_config(account_id).valuation_mode


def _load_flow_inputs(account_id: int, valuation_mode: str):
    """Everything the wizard needs to OPEN, in one thread hop. Blocking.

    Returns:
        Tuple: ``(base, labels, allow_fractional, symbol_values, positions,
        unallocated_pct)`` -- the frozen base snapshot, the managed labels with
        their symbol weights AND the previous generation of both, the account's
        remembered fractional choice, ``{SYMBOL: current value}`` under
        ``valuation_mode`` for the wizard's read-only "now" captions, the raw
        ``{SYMBOL: PositionState}`` behind them, and the stored cash reserve that
        pre-fills the Unallocated box.

        ``symbol_values`` goes through the engine's own ``current_value`` rather
        than being re-derived, so the caption beside a target and the base that
        target divides are measured the same way. It is DISPLAY only -- nothing in
        it reaches a plan.

        ``positions`` is the SAME map ``symbol_values`` and ``base`` were built
        from, handed over unreduced because the wizard's unrealised P&L may not be
        measured on a mode-aware current value: in COST mode ``current_value`` IS
        the cost basis, so a P&L taken from ``symbol_values`` reads 0.00 on every
        row. Quantity, cost basis and the live ``price`` are needed separately, and
        ``build_position_states`` fetches quotes in BOTH modes, so the figure is
        available whichever mode the account is on. Display only, like the values.

    Raises:
        PositionFetchFailed: the broker's position fetch failed. NOT a flat account,
            and the wizard must not open on the difference.
        RuntimeError: the account could not be instantiated.
        ValueError: the broker published no buying power (``build_base_snapshot``).
    """
    from ...core.utils import get_account_instance_from_id, get_symbols_by_label

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")
    managed = get_managed_labels(account_id)
    symbols_by_label = get_symbols_by_label([row.label for row in managed])
    symbols = collect_managed_symbols(symbols_by_label)

    labels = []
    for row in managed:
        members = symbols_by_label.get(row.label, [])
        weights = get_symbol_weights(account_id, row.label, members)
        # NULL stays None all the way to the dialog: "there is no last" is what
        # disables the Load-last button, and it is a different fact from 0.0.
        previous_weights = get_previous_symbol_weights(account_id, row.label, members)
        labels.append(LabelTarget(
            label=row.label, target_pct=float(row.target_pct or 0.0),
            symbols=[SymbolTarget(symbol=s, weight_pct=float(weights.get(s, 0.0)),
                                  previous_weight_pct=previous_weights.get(s))
                     for s in members],
            comment=row.comment,
            previous_target_pct=row.previous_target_pct))

    current = svc.build_position_states(account, symbols)
    base = build_base_snapshot(account.get_account_snapshot(), current, symbols,
                               valuation_mode=valuation_mode)
    symbol_values = {s: current_value(current.get(s), valuation_mode) for s in symbols}
    config = get_allocation_config(account_id)
    return (base, labels, bool(config.allow_fractional), symbol_values, current,
            float(config.unallocated_pct or 0.0))


def _solve_plan(account_id: int, *, mode: str, labels, scope_label, amount: float,
                allow_fractional: bool, valuation_mode: str,
                unallocated_pct: float = 0.0,
                force_market_refresh: bool = False):
    """Solve one dry run against FRESH positions, prices and margin info. Blocking.

    Re-reads everything rather than reusing the open dialog's snapshot: Refresh
    exists precisely because the numbers move, and a plan solved against a stale
    price is a plan submitted at the wrong size.

    ``unallocated_pct`` is the cash reserve, and it reaches the REBALANCE branch
    only. The invest branch deploys a specific amount the user named, so there is
    no base for a share of it to be taken from -- see ``compute_label_investment``,
    which deliberately has no such parameter.

    ``force_market_refresh`` drops the account's cached market-hours answer first.
    ``get_market_hours`` caches for ``min(TTL, next session boundary)``, which is
    right for the several reads one render makes and wrong for a user who pressed
    Refresh *because* they believe the market has opened. Only the wizard's Refresh
    passes it: the first solve of a flow has nothing to invalidate, and clearing on
    every solve would turn the de-duplicator into a broker call per read.

    Returns:
        Tuple: ``(base, plan, current, hours)``. ``hours`` is the broker's
        ``MarketHours`` or ``None``; ONE read feeds both the banner and the gate.
    """
    from ...core.utils import get_account_instance_from_id

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")
    if force_market_refresh:
        svc.clear_market_hours_cache(account)
    symbols = collect_managed_symbols(
        {lt.label: [st.symbol for st in lt.symbols] for lt in labels})
    current = svc.build_position_states(account, symbols)
    margin = svc.fetch_margin_info(account, symbols)
    base = build_base_snapshot(account.get_account_snapshot(), current, symbols,
                               valuation_mode=valuation_mode)
    if mode == ALLOCATION_MODE_INVEST_LABEL:
        scope = next((lt for lt in labels if lt.label == scope_label), None)
        if scope is None:
            raise ValueError(f"Label {scope_label!r} is no longer managed")
        plan = compute_label_investment(
            scope, float(amount), current, margin,
            available_buying_power=base.available_buying_power,
            allow_fractional=allow_fractional, default_bp_factor=base.default_bp_factor,
            valuation_mode=valuation_mode)
    else:
        plan = compute_allocation(
            base.base_notional, base.available_buying_power, labels, current, margin,
            allow_fractional=allow_fractional,
            default_bp_factor=base.default_bp_factor, valuation_mode=valuation_mode,
            unallocated_pct=unallocated_pct)
    # ``margin`` is REQUIRED and is the SAME dict the plan above was solved with:
    # the precheck may re-solve, and a re-solve without it rebuilds a bare
    # MarginInfo per fractional row and rounds on the default 4dp grid, losing
    # min_trade_increment / min_order_size / min_fractional_notional.
    plan = svc.precheck_plan(account, plan,
                             available_buying_power=base.available_buying_power,
                             margin=margin)
    return base, plan, current, svc.fetch_market_hours(account)


def _submit_plan(account_id: int, plan, current, base, *, mode: str, scope_label):
    """Submit a reviewed plan. Blocking. The service re-checks the market gate."""
    from ...core.utils import get_account_instance_from_id

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")
    return svc.run_allocation(account, plan, current, base, mode=mode,
                              scope_label=scope_label)


def _load_income_panel(account_id: int):
    """Sync the ledger from the broker, read it back, and say what is still owed.

    Blocking; always called through ``asyncio.to_thread``.

    ``svc.sync_income_events`` runs the unconsumed-run reconcile pass first (it is
    the panel's Refresh handler AND this page's load call, so that one hook covers
    both). Runs whose orders were still working when they finalised consumed NO
    income, and with about a quarter of the book on whole shares that is the common
    outcome, not the rare one -- so the drain has to be automatic, and what SURVIVES
    it has to be visible. ``describe_unconsumed_runs`` is the DB-only read behind
    that sentence: without it the panel shows an unallocated figure that never goes
    down and never explains itself.

    Returns:
        ``(events, open_total, working_note)`` where ``working_note`` is the
        ``(text, severity)`` pair or ``None``.
    """
    from ...core.utils import get_account_instance_from_id

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")
    svc.sync_income_events(account)
    outstanding = svc.describe_unconsumed_runs(account_id)
    working_note = unconsumed_income_notice(len(outstanding["run_ids"]),
                                            len(outstanding["working_order_ids"]))
    return (svc.get_recent_income_events(account_id),
            svc.get_open_income_total(account_id),
            working_note)


def _expert_label_families() -> frozenset:
    """The machine-tag families the CURRENT expert registry can generate.

    ``InstrumentAutoAdder`` stamps ``MarketExpertInterface.shortname`` --
    ``f"{cls.__name__.lower()}-{id}"`` -- onto every instrument an expert picks, so
    the families are exactly the registry's lower-cased class names. Deriving them
    means a newly registered expert is hidden from the picker on the day it ships
    instead of leaking until someone remembers to edit a literal.

    The registry import is LOCAL: it pulls TradingAgents and with it the LLM stack.
    A registry that cannot be loaded falls back to the built-in floor rather than
    failing the picker -- worst case a tag is offered, which is recoverable; the
    picker not opening is not.
    """
    try:
        from ...modules.experts import experts as expert_classes
    except Exception as e:
        logger.warning(f"Could not read the expert registry for the label filter: {e}")
        return DEFAULT_MACHINE_LABEL_FAMILIES
    return frozenset(expert_shortname_families(expert_classes)
                     | DEFAULT_MACHINE_LABEL_FAMILIES)


def _load_picker_data(account_id: int) -> Dict[str, Any]:
    """Current managed labels, every label in use, and the machine-tag families."""
    from ...core.utils import get_all_instrument_labels

    managed = get_managed_labels(account_id)
    return {
        'current': [row.label for row in managed],
        # NULL stays None: "no colour chosen" is a different fact from a stored
        # default, and it is what makes the picker's "No colour" entry truthful.
        'colors': {row.label: row.color for row in managed},
        'all_labels': get_all_instrument_labels(),
        'machine_families': _expert_label_families(),
    }


def _stored_symbol_counts(account_id: int, labels) -> Dict[str, int]:
    """How many per-symbol weight/comment rows each label would take with it."""
    return {label: len(get_symbol_rows(account_id, label)) for label in labels}


def _validate_symbols(account_id: int, symbols: List[str]):
    """Split ``symbols`` into ``(known, unknown)`` per the broker's own list.

    Instrument rows are GLOBAL, so an unchecked "add symbol" turns any typo --
    'APPL', or the empty string left by a trailing comma -- into a permanent
    ``Instrument`` row that every account and every label picker then sees. The
    broker already knows which tickers exist; ask it.

    Raises:
        RuntimeError: the account could not be instantiated. Refusing the add is
        the right answer -- guessing would write the phantom row.
    """
    from ...core.utils import get_account_instance_from_id

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")
    existence = account.symbols_exist(symbols)
    known = [s for s in symbols if existence.get(s)]
    unknown = [s for s in symbols if not existence.get(s)]
    return known, unknown


# ---------------------------------------------------------------------------
# Eager persistence handlers (no Save button -- switching the global account
# hard-reloads the document, so a pending edit would be lost)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The live figure registry
#
# Inline editing is only worth having if the CONSEQUENCE of an edit is on screen
# immediately, and this page rebuilds by ``container.clear()`` -- which would tear
# down the very ``ui.number`` the change event came from. So one render's widgets
# and their inputs are registered here and the derived figures are rewritten IN
# PLACE. Every number in it is produced by a pure function in
# ``ui/utils/portfolio_allocation_view.py``; nothing below does arithmetic.
#
# Three edits, three blast radii, and all three have to work:
#   * a SYMBOL weight  -> that label's table only (but ALL its rows -- the unstored
#                         siblings share whatever is left of 100, so storing one
#                         weight moves what the others resolve to);
#   * a LABEL target   -> that label's header AND every row beneath it;
#   * the RESERVE      -> the reserve line, every header and every row on the page.
# ---------------------------------------------------------------------------

#: Severity -> stylesheet classes for the totals footer. Red the moment the label
#: targets pass 100, which is the existing validation surfaced BEFORE the user
#: presses Allocate rather than inside the dry run.
FOOTER_CLASSES = {
    'ok': 'text-xs text-secondary-custom',
    'warning': 'text-xs text-orange-400',
    'negative': 'text-xs text-red-400 font-bold',
}

#: Money and percentages line up column-to-column only if the digits are the same
#: width. Applied to every figure on the label rows, so 39.5% and 1.4% put their
#: decimal points in the same place.
TABULAR_NUMS = 'font-variant-numeric: tabular-nums;'

#: NiceGUI markers on the mini-bar's two positioned divs. Their entire content is
#: an inline style -- the geometry IS the information -- so a test needs a handle
#: on each that does not depend on document order.
MARKER_BAR_FILL = 'pf-bar-fill'
MARKER_BAR_NOTCH = 'pf-bar-notch'
#: The whole label header row, so a test can assert on the row rather than on a
#: count of everything that happens to share a style.
MARKER_BAR_ROW = 'pf-bar-row'

#: The mini-bar track. Height and radius only -- the FILL's colour comes from the
#: label's own palette entry, which is what makes the bars tell labels apart.
BAR_TRACK_STYLE = ('position:relative;height:10px;border-radius:3px;'
                   'background:rgba(255,255,255,0.08);')
#: The target notch. White and 2px, i.e. deliberately not one of the palette hues:
#: it has to read against every fill colour, including the pale yellow.
BAR_NOTCH_STYLE = ('position:absolute;top:-3px;bottom:-3px;width:2px;'
                   'background:#FFFFFF;opacity:0.85;')


def _new_live_state(*, base_notional: Optional[float] = None,
                    available_buying_power: Optional[float] = None,
                    unallocated_pct: float = 0.0) -> Dict[str, Any]:
    """One render's mutable view of what is on screen. Not persisted anywhere.

    ``views`` is the ORDERED ``LabelView`` list and is the single source for every
    per-label number; an accepted edit mutates the view in place and the redraw
    reads it back. Keeping a parallel ``{label: target}`` dict beside it was the
    obvious alternative and is exactly how the header and the bar would come to
    disagree.
    """
    return {
        'base_notional': base_notional,
        'available_buying_power': available_buying_power,
        'unallocated_pct': float(unallocated_pct or 0.0),
        'views': [],            # ordered LabelView list, MUTATED on an accepted edit
        'view_by_label': {},
        'weights': {},          # label -> {symbol: effective weight %}
        'tables': {},           # label -> ui.table
        'captions': {},         # label -> [elements whose text IS the header line]
        'label_inputs': {},     # label -> the label's target ui.number
        'expansions': {},       # label -> ui.expansion
        'bars': {},             # label -> the mini-bar row's mutable elements
        'reserve_row': None,    # the "Unallocated (free buying power)" line
        'reserve_caption': None,
        'reserve_number': None,
        'reserve_slider': None,
        'total_notice': None,   # the running "labels total N%" advisory
        'footer': None,         # the totals line under the list
    }


def _register_view(live: Dict[str, Any], view) -> None:
    """Put one label's view into the registry, preserving render order."""
    if view.label not in live['view_by_label']:
        live['views'].append(view)
    live['view_by_label'][view.label] = view


def _label_targets(live: Dict[str, Any]) -> Dict[str, float]:
    """``{label: target_pct}`` as it stands right now. Derived, never stored twice."""
    return {v.label: float(v.target_pct or 0.0) for v in live['views']}


def _apply_symbol_figures(live: Dict[str, Any], label: str) -> None:
    """Rewrite one label's Share-of-label % and Target value cells. In place.

    Every row, not just the edited one: ``get_symbol_weights`` fills the symbols
    with no stored row from an even split of whatever is left of 100, so storing a
    weight for ONE symbol changes what its siblings resolve to. Showing them their
    old numbers is how the table stops matching the database.
    """
    table = live['tables'].get(label)
    if table is None:
        return
    view = live['view_by_label'].get(label)
    weights = live['weights'].get(label) or {}
    values = symbol_target_values(
        weights, label_target_pct=(float(view.target_pct or 0.0) if view else 0.0),
        base_notional=live['base_notional'], unallocated_pct=live['unallocated_pct'])
    for row in table.rows:
        symbol = row['symbol']
        if symbol not in weights:
            continue
        row['weight_pct'] = round(float(weights[symbol]), 2)
        value = values.get(symbol)
        row['target_value'] = None if value is None else round(value, 2)
    table.update()


def _apply_bars(live: Dict[str, Any]) -> None:
    """Redraw every mini-bar, header line and status word. In place.

    ALL of them, on any change, because the track's scale spans the whole page: one
    label's target rising can move the notch and the fill of every other row. A
    per-row update would leave the rest measured against a scale that no longer
    exists, which is the same class of defect as a notch drawn on its own scale.
    """
    bars = build_label_bars(live['views'], base_notional=live['base_notional'],
                            unallocated_pct=live['unallocated_pct'])
    for bar in bars:
        widgets = live['bars'].get(bar.label)
        if widgets is None:
            continue
        widgets['fill'].style(replace=(
            f'position:absolute;left:0;top:0;bottom:0;border-radius:3px;'
            f'width:{bar.bar_fraction * 100.0:.2f}%;background:{bar.color};'))
        if bar.notch_fraction is None:
            widgets['notch'].set_visibility(False)
        else:
            widgets['notch'].set_visibility(True)
            widgets['notch'].style(
                replace=BAR_NOTCH_STYLE + f'left:{bar.notch_fraction * 100.0:.2f}%;')
        widgets['value'].set_text(f'${bar.current_value:,.2f}')
        widgets['pct'].set_text(f'{bar.current_pct:.1f}%')
        widgets['target'].set_text(f'tgt {bar.target_pct:.1f}%')
        widgets['status'].set_text(bar.status)
        widgets['status'].classes(replace='text-xs ' + LABEL_STATUS_CLASSES[bar.status])
        widgets['tooltip'].set_text(format_label_target_tooltip(
            target_pct=bar.raw_target_pct, base_notional=live['base_notional'],
            unallocated_pct=live['unallocated_pct']))
        for element in live['captions'].get(bar.label, []):
            element.set_text(format_label_header(
                label=bar.label,
                current_value=live['view_by_label'][bar.label].current_value,
                target_pct=bar.raw_target_pct,
                pct_of_base=live['view_by_label'][bar.label].pct_of_base,
                pct_of_total=live['view_by_label'][bar.label].pct_of_total,
                base_notional=live['base_notional'],
                unallocated_pct=live['unallocated_pct']))


def _apply_total_notice(live: Dict[str, Any]) -> None:
    """Refresh the running "labels total N%" advisory and the totals footer.

    This is the page's own over/under-100 check -- no dry run needed, exactly as in
    the wizard's step 1. Both readouts come from the same targets, so they cannot
    say different things.
    """
    targets = _label_targets(live)
    element = live['total_notice']
    if element is not None:
        notice = format_label_total_notice(targets)
        if notice is None:
            element.set_text('')
            element.set_visibility(False)
        else:
            text, severity = notice
            element.set_text(text)
            element.classes(replace=LABEL_TOTAL_NOTICE_CLASSES[severity])
            element.set_visibility(True)
    footer = live['footer']
    if footer is not None:
        text, severity = format_allocation_footer(targets, live['unallocated_pct'])
        footer.set_text(text)
        footer.classes(replace=FOOTER_CLASSES[severity])


def _apply_page_figures(live: Dict[str, Any]) -> None:
    """Everything derived, redrawn: bars, headers, every table, both totals.

    ONE entry point for the two edits whose blast radius is the whole page -- a
    label target (it moves the shared bar scale and both totals) and the reserve
    (it re-bases every target). A symbol weight is the cheap case and keeps its own
    ``_apply_symbol_figures``.
    """
    _apply_bars(live)
    for label in list(live['view_by_label']):
        _apply_symbol_figures(live, label)
    _apply_total_notice(live)


def _apply_reserve(live: Dict[str, Any]) -> None:
    """The reserve moved: redraw its own two lines and EVERYTHING it re-bases.

    Raising the reserve shrinks ``investable_notional``, so every label's target
    money, every symbol's share of it, every notch and every status word move with
    it. That is the entire reason the control is worth having on this page.
    """
    if live['reserve_caption'] is not None:
        live['reserve_caption'].set_text(
            format_reserve_caption(live['base_notional'], live['unallocated_pct']))
    if live['reserve_row'] is not None:
        text = format_reserve_row(base_notional=live['base_notional'],
                                  available_buying_power=live['available_buying_power'],
                                  unallocated_pct=live['unallocated_pct'])
        if text is not None:
            live['reserve_row'].set_text(text)
    _apply_page_figures(live)


def _revert_symbol_cell(live: Dict[str, Any], label: str, symbol: str) -> None:
    """Put a REFUSED target cell back to the stored number.

    The cell is a Quasar ``q-input`` bound to ``props.value``; leaving the row data
    alone leaves the typed text in the DOM, because Vue's watcher only fires when
    the bound value CHANGES. So the row carries a ``weight_key`` that the template
    uses as the input's ``:key`` -- bumping it remounts the input from the row data.

    A rejected edit must never leave a number on screen the database does not have.
    That is the same defect ("a plausible-looking number that means nothing") this
    whole feature exists to remove.
    """
    table = live['tables'].get(label)
    if table is None:
        return
    for row in table.rows:
        if row['symbol'] == symbol:
            row['weight_key'] = int(row.get('weight_key', 0)) + 1
    table.update()


def _restore_value(widget, value) -> None:
    """Put a plain ``ui.number`` / ``ui.slider`` back, tolerating an absent widget.

    The write-back fires the widget's own change handler; every handler here starts
    with a compare-against-stored and returns on a match, so the echo is a no-op.
    That is the same guard ``_set_mode`` uses for the valuation selector.
    """
    if widget is not None:
        widget.set_value(value)


def _write_symbol_weight(account_id: int, label: str, symbol: str, weight_pct: float,
                         label_symbols: List[str]):
    """Persist ONE symbol's target weight and read the label's map back. Blocking.

    Returns ``None`` -- a refusal, not an empty result -- when the label is no
    longer managed. A page rendered before the label was unmanaged (in the picker,
    in another tab, by an account switch) still has its table on screen, and
    ``set_symbol_weight`` creates rows unconditionally, so typing in it would leave
    an orphan allocation row under a label the account does not manage. Same guard,
    same reason, as ``_write_label_comment``.

    The map is re-read rather than patched locally because writing one row changes
    what the UNSTORED siblings resolve to: ``get_symbol_weights`` shares whatever is
    left of 100 among them. One reader, so the table and the engine cannot disagree.

    ``comment`` is deliberately NOT passed: ``None`` leaves it alone, and a ``''``
    here would wipe the symbol's note on every accepted keystroke.
    """
    if label not in {row.label for row in get_managed_labels(account_id)}:
        return None
    set_symbol_weight(account_id, label, symbol, weight_pct=float(weight_pct))
    return get_symbol_weights(account_id, label, label_symbols)


async def _save_symbol_weight(account_id: int, live: Dict[str, Any], label: str,
                              symbol: str, raw, label_symbols: List[str]) -> None:
    """One TARGET % cell. Validate, persist, then redraw the label's rows."""
    edit = validate_symbol_weight_edit(label=label, symbol=symbol, raw=raw)
    if not edit.accepted:
        ui.notify(edit.message, type='warning')
        _revert_symbol_cell(live, label, symbol)
        return
    try:
        weights = await asyncio.to_thread(_write_symbol_weight, account_id, label,
                                          symbol, edit.value, label_symbols)
    except Exception as e:
        logger.error(f"Saving target for {label}/{symbol} failed: {e}", exc_info=True)
        ui.notify(f'Could not save target: {e}', type='negative')
        _revert_symbol_cell(live, label, symbol)
        return
    if weights is None:
        logger.warning(f"Target for '{label}'/{symbol} ignored: the label is no longer "
                       f"managed by account {account_id}")
        ui.notify(f"'{label}' is no longer managed — refresh the page", type='warning')
        _revert_symbol_cell(live, label, symbol)
        return
    live['weights'][label] = weights
    _apply_symbol_figures(live, label)


def _write_label_target(account_id: int, label: str, target_pct: float) -> bool:
    """Save a managed label's target. Blocking; returns False when it is STALE.

    ``set_managed_label`` CREATES the row when it is absent, so without this an
    edit made after the label was unmanaged elsewhere would resurrect it -- and
    resurrect it as a label the user did not choose to manage, holding money.
    """
    if label not in {row.label for row in get_managed_labels(account_id)}:
        return False
    set_managed_label(account_id, label, target_pct=float(target_pct))
    return True


async def _save_label_target(account_id: int, live: Dict[str, Any], label: str,
                             raw) -> None:
    """One label's TARGET % box.

    The over-100 refusal is the wizard's own, moved onto the page with the box:
    ``validate_label_target_edit`` measures the typed value against the OTHER
    labels' stored targets with the engine's tolerance. A refused edit writes
    nothing and the box goes back to the stored number.
    """
    targets = _label_targets(live)
    stored = targets.get(label, 0.0)
    # The WHOLE map, including this label's own entry: ``validate_label_target_edit``
    # drops the entry matching ``label`` itself, and filtering here as well put the
    # same rule in two places -- so a mutation to either survived, because the other
    # covered for it. One guard, in the pure layer, where it is tested.
    edit = validate_label_target_edit(label=label, raw=raw, other_targets=targets)
    if not edit.accepted:
        ui.notify(edit.message, type='warning')
        _restore_value(live['label_inputs'].get(label), stored)
        return
    if edit.value == stored:
        return                      # a no-op, or the echo of a programmatic set
    try:
        saved = await asyncio.to_thread(_write_label_target, account_id, label,
                                        edit.value)
    except Exception as e:
        logger.error(f"Saving target for label '{label}' failed: {e}", exc_info=True)
        ui.notify(f'Could not save target: {e}', type='negative')
        _restore_value(live['label_inputs'].get(label), stored)
        return
    if not saved:
        logger.warning(f"Target for '{label}' ignored: the label is no longer managed "
                       f"by account {account_id}")
        ui.notify(f"'{label}' is no longer managed — refresh the page", type='warning')
        _restore_value(live['label_inputs'].get(label), stored)
        return
    view = live['view_by_label'].get(label)
    if view is not None:
        view.target_pct = edit.value
    _apply_page_figures(live)


async def _save_reserve(account_id: int, live: Dict[str, Any], raw, *, echo_to) -> None:
    """The cash-reserve slider and its bound number box.

    ONE stored field (``unallocated_pct``), two controls: whichever moved writes it
    and then mirrors the other, and the mirror's echo is absorbed by the
    compare-against-stored above. The dollar figure beside them is DERIVED and is
    deliberately not a second input -- see ``format_reserve_caption``.
    """
    edit = validate_reserve_edit(raw)
    if not edit.accepted:
        ui.notify(edit.message, type='warning')
        _restore_value(live['reserve_number'], live['unallocated_pct'])
        _restore_value(live['reserve_slider'], live['unallocated_pct'])
        return
    if edit.value == live['unallocated_pct']:
        return                      # a no-op, or the echo of the mirrored control
    try:
        await asyncio.to_thread(set_allocation_config, account_id,
                                unallocated_pct=float(edit.value))
    except Exception as e:
        logger.error(f"Saving the unallocated reserve failed: {e}", exc_info=True)
        ui.notify(f'Could not save the reserve: {e}', type='negative')
        _restore_value(live['reserve_number'], live['unallocated_pct'])
        _restore_value(live['reserve_slider'], live['unallocated_pct'])
        return
    live['unallocated_pct'] = edit.value
    _restore_value(echo_to, edit.value)
    _apply_reserve(live)


def _focus_label_target(live: Dict[str, Any], label: str) -> None:
    """The pencil beside the chevron: OPEN the label and put the cursor in its box.

    ``value = True``, never a toggle: this is "edit this label", and a pencil that
    closed an already-open section would be doing the chevron's job badly. The
    listener is registered as ``click.stop`` and the expansion carries
    ``expand-icon-toggle``, so neither propagation nor Quasar's default
    click-anywhere-on-the-header can fold it as a side effect.
    """
    expansion = live['expansions'].get(label)
    if expansion is not None:
        expansion.value = True
    number = live['label_inputs'].get(label)
    if number is not None:
        number.run_method('focus')


def _write_label_comment(account_id: int, label: str, value: str) -> bool:
    """Save a managed label's comment. Blocking; returns False when it is STALE.

    ``set_managed_label`` CREATES the row when it is absent, at ``target_pct=0``.
    A page rendered before the label was unmanaged (in the picker, in another tab,
    by an account switch) still has its comment box on screen, and typing in it
    would silently resurrect the label with a zero target -- which the allocation
    engine reads as "hold none of this". So the row has to already exist.
    """
    if label not in {row.label for row in get_managed_labels(account_id)}:
        return False
    set_managed_label(account_id, label, comment=value or "")
    return True


async def _save_label_comment(account_id: int, label: str, value: str) -> None:
    """Comment-box handler: the DB round trip goes to a thread, never the loop."""
    try:
        saved = await asyncio.to_thread(_write_label_comment, account_id, label, value)
    except Exception as e:
        logger.error(f"Saving comment for label '{label}' failed: {e}", exc_info=True)
        ui.notify(f'Could not save comment: {e}', type='negative')
        return
    if not saved:
        logger.warning(f"Comment for '{label}' ignored: the label is no longer managed "
                       f"by account {account_id}")
        ui.notify(f"'{label}' is no longer managed — refresh the page", type='warning')


def _write_symbol_comment(account_id: int, label: str, symbol: str, value: str,
                          label_symbols: List[str]) -> None:
    """Persist a symbol's comment WITHOUT moving its allocation. Blocking.

    ``set_symbol_weight`` is the one writer for this row, and creating a row makes
    the weight EXPLICIT — a bare ``comment=`` write would create it at the model
    default of 0.0 and the engine reads 0 as "hold none of this", so the next
    rebalance would sell a position the user only wrote a note about. So the
    symbol's current EFFECTIVE weight (its stored value, or the even-split default
    it was silently taking) is passed alongside the comment, which pins it at
    exactly the number it already had. ``weight_pct == 0.0`` deliberately stays a
    legitimate explicit zero and is never re-read as "unstored" — doing that would
    re-introduce drift from the engine's ``build_symbol_targets``.

    ``label_symbols`` must be the label's FULL symbol list: the even-split default
    is only correct when every symbol sharing the 100% is known.

    Side effect, accepted: the symbol's weight stops floating with the even split,
    so a symbol added to the label later re-splits only what is left. That is the
    documented meaning of a stored row, and it is strictly better than the zeroing
    it replaces.
    """
    effective = get_symbol_weights(account_id, label, label_symbols)
    set_symbol_weight(account_id, label, symbol,
                      weight_pct=effective.get(symbol), comment=value or "")


async def _save_symbol_comment(account_id: int, label: str, symbol: str, value: str,
                               label_symbols: List[str]) -> None:
    """Comment-cell handler: two DB round trips, both off the event loop."""
    try:
        await asyncio.to_thread(_write_symbol_comment, account_id, label, symbol, value,
                                label_symbols)
    except Exception as e:
        logger.error(f"Saving comment for {label}/{symbol} failed: {e}", exc_info=True)
        ui.notify(f'Could not save comment: {e}', type='negative')


async def _add_symbols_from_input(account_id: int, label: str, raw: str,
                                  on_success) -> None:
    """Parse the typed list, CHECK IT AT THE BROKER, then label what survives.

    ``add_label_to_instruments`` creates an ``Instrument`` row for anything it is
    handed, and instrument rows are GLOBAL: a typo'd 'APPL', or the empty entry a
    trailing comma leaves behind, used to become a permanent row that every account
    and every label picker then saw. ``symbols_exist`` already answers this, so ask
    it, name what was rejected, and add the rest.

    An unusable account is a refusal, not a fallback -- guessing writes the phantom
    row this exists to prevent. ``on_success`` (close the dialog, refresh) runs only
    when something was actually added.
    """
    symbols = [s.strip().upper() for s in (raw or '').split(',') if s.strip()]
    if not symbols:
        ui.notify('Enter at least one symbol', type='warning')
        return
    try:
        known, unknown = await asyncio.to_thread(_validate_symbols, account_id, symbols)
    except Exception as e:
        logger.error(f"Checking {symbols} against the broker failed: {e}", exc_info=True)
        ui.notify(f'Could not check the symbols: {e}', type='negative')
        return
    if unknown:
        logger.warning(f"Account {account_id}: refusing to label unknown symbol(s) "
                       f"{unknown} — no Instrument row is created for them")
        ui.notify(f"Unknown at the broker, not added: {', '.join(unknown)}",
                  type='warning')
    if not known:
        return
    try:
        added = await asyncio.to_thread(add_symbols_to_label, account_id, label, known)
    except Exception as e:
        logger.error(f"Adding {known} to '{label}' failed: {e}", exc_info=True)
        ui.notify(f'Could not add: {e}', type='negative')
        return
    ui.notify(f"Added {added} symbol(s) to '{label}'", type='positive')
    await on_success()


def _open_add_symbol_dialog(account_id: int, label: str, refresh) -> None:
    with ui.dialog() as dialog, ui.card().classes('min-w-[420px]'):
        ui.label(f"Add symbols to '{label}'").classes('text-h6')
        ui.label('Comma-separated. A symbol does not need an open position, but it '
                 'does have to exist at the broker.').classes('text-xs text-secondary-custom')
        entry = ui.input('Symbols', placeholder='AAPL, MSFT').classes('w-full')

        async def _done() -> None:
            dialog.close()
            await refresh()

        with ui.row().classes('w-full justify-end gap-2 mt-4'):
            ui.button('Cancel', on_click=dialog.close).props('flat')
            ui.button('Add', on_click=lambda: _add_symbols_from_input(
                account_id, label, entry.value, _done)).props('color=primary')
    dialog.open()


def _confirm_unmanage(labels: List[str], counts: Dict[str, int],
                      on_confirm, on_cancel) -> None:
    """Ask before an unmanage, because an unmanage DESTROYS stored configuration.

    ``replace_managed_labels`` deletes the label row and every
    ``PortfolioAllocationSymbol`` beneath it -- the target percentage, the label
    comment, and each symbol's weight and comment. There is no undo and no audit
    row. A picker fires on every change event, so one mis-aimed click on a chip's
    ✕ used to be enough.
    """
    with ui.dialog() as dialog, ui.card().classes('min-w-[460px]'):
        ui.label('Stop managing these labels?').classes('text-h6')
        for label in labels:
            stored = counts.get(label, 0)
            ui.label(f"• {label} — deletes its target %, its comment"
                     + (f" and {stored} stored symbol weight/comment row(s)"
                        if stored else "")).classes('text-sm')
        ui.label('This cannot be undone.').classes('text-xs text-secondary-custom')

        async def _yes() -> None:
            dialog.close()
            await on_confirm()

        def _no() -> None:
            dialog.close()
            on_cancel()

        with ui.row().classes('w-full justify-end gap-2 mt-4'):
            ui.button('Cancel', on_click=_no).props('flat')
            ui.button('Unmanage', on_click=_yes).props('color=negative')
    dialog.open()


def _write_label_color(account_id: int, label: str, value: str) -> bool:
    """Save a managed label's swatch. Blocking; returns False when it is STALE.

    Guarded exactly as ``_write_label_comment`` is, and for the same reason:
    ``set_managed_label`` CREATES the row, so recolouring a label that was unmanaged
    in another tab would resurrect it at ``target_pct=0`` -- which the engine reads
    as "hold none of this".

    ``store_color_value`` is what turns the picker's "No colour" into the ``''`` the
    store maps to NULL. Handing the store the ``None`` that
    ``normalise_label_color`` returns would mean LEAVE UNCHANGED, and the swatch
    would be impossible to remove.
    """
    if label not in {row.label for row in get_managed_labels(account_id)}:
        return False
    set_managed_label(account_id, label, color=store_color_value(value))
    return True


async def _save_label_color(account_id: int, label: str, value: str, swatch) -> None:
    """Colour-picker handler: persist, then retint the swatch beside the row."""
    try:
        saved = await asyncio.to_thread(_write_label_color, account_id, label, value)
    except Exception as e:
        logger.error(f"Saving the colour for label '{label}' failed: {e}", exc_info=True)
        ui.notify(f'Could not save the colour: {e}', type='negative')
        return
    if not saved:
        logger.warning(f"Colour for '{label}' ignored: the label is no longer managed "
                       f"by account {account_id}")
        ui.notify(f"'{label}' is no longer managed — refresh the page", type='warning')
        return
    if swatch is not None:
        swatch.style(replace=f'color: {resolve_label_icon_color(value)}')


async def _persist_managed_labels(account_id: int, current: List[str],
                                  selected: List[str], restore) -> None:
    """Apply one picker change: additions immediately, removals only once confirmed.

    ``current`` is the picker's last SAVED selection and is updated in place on a
    successful write. ``restore`` puts the widget back to it -- used when the user
    cancels the confirmation and when the write fails, so the chips on screen never
    claim a state the database does not have.
    """
    to_add, to_remove = diff_managed_labels(current, selected)
    if not to_add and not to_remove:
        return

    async def _write() -> None:
        try:
            await asyncio.to_thread(replace_managed_labels, account_id, selected)
        except Exception as exc:
            logger.error(f"Saving managed labels for account {account_id} failed: {exc}",
                         exc_info=True)
            ui.notify(f'Could not save: {exc}', type='negative')
            restore()
            return
        current[:] = list(selected)
        ui.notify(f'Managed labels updated (+{len(to_add)} / -{len(to_remove)})',
                  type='positive')

    if not to_remove:
        await _write()
        return

    try:
        counts = await asyncio.to_thread(_stored_symbol_counts, account_id, to_remove)
    except Exception as exc:
        logger.error(f"Counting stored weights for {to_remove} failed: {exc}", exc_info=True)
        counts = {}
    _confirm_unmanage(to_remove, counts, _write, restore)


def _open_label_picker(account_id: int, refresh) -> None:
    """Pick which labels this account manages. Additions persist on change."""
    try:
        data = _load_picker_data(account_id)
    except Exception as e:
        logger.error(f"Loading labels for account {account_id} failed: {e}", exc_info=True)
        ui.notify(f'Could not load labels: {e}', type='negative')
        return

    current = list(data['current'])
    colors = dict(data['colors'])
    all_labels = data['all_labels']
    families = data['machine_families']

    with ui.dialog() as dialog, ui.card().classes('min-w-[520px]'):
        ui.label('Managed labels').classes('text-h6')
        ui.label('Machine tags (auto_added, expert_selected, ai_selected, not_found and '
                 'the per-expert <name>-N families) are hidden — a label this account '
                 'already manages is always listed, whatever it is called.'
                 ).classes('text-xs text-secondary-custom')

        def _restore() -> None:
            picker.set_value(list(current))

        picker = ui.select(picker_options(all_labels, current, machine_families=families),
                           value=list(current), multiple=True, label='Labels',
                           on_change=lambda e: _persist_managed_labels(
                               account_id, current, list(e.value or []), _restore)
                           ).props('dense outlined use-chips').classes('w-full')

        ui.switch('Show all labels (including machine tags)',
                  on_change=lambda e: picker.set_options(
                      picker_options(all_labels, current, show_all=bool(e.value),
                                     machine_families=families),
                      value=picker.value))

        if current:
            ui.label('Colours').classes('text-sm font-bold mt-3')
            ui.label('A fixed palette, not a free picker: this UI is dark-themed, so '
                     'an arbitrary colour is one you cannot read back. These seven '
                     'are Okabe & Ito’s colour-universal-design set — chosen to stay '
                     'distinguishable to the common forms of colour blindness.'
                     ).classes('text-xs text-secondary-custom')
        for label in current:
            with ui.row().classes('w-full items-center gap-2'):
                # ``lbl=label`` on BOTH the swatch and the handler: without the
                # default-argument capture every row would recolour the last label.
                swatch = ui.icon('label').style(
                    f'color: {resolve_label_icon_color(colors.get(label))}')
                ui.label(label).classes('flex-grow truncate')
                ui.select(label_color_options(),
                          value=(colors.get(label) or NO_LABEL_COLOR),
                          on_change=lambda e, lbl=label, sw=swatch: _save_label_color(
                              account_id, lbl, e.value, sw)
                          ).props('dense outlined').classes('w-44')

        async def _close() -> None:
            dialog.close()
            await refresh()

        with ui.row().classes('w-full justify-end gap-2 mt-4'):
            ui.button('Close', on_click=_close).props('color=primary')
    dialog.open()


# ---------------------------------------------------------------------------
# Rendering (eyeball-only; all decisions already made above)
# ---------------------------------------------------------------------------

def _render_gate_blocked(gate: GateResult) -> None:
    with ui.card().classes('w-full'):
        with ui.row().classes('items-center gap-2'):
            ui.icon('block').classes('text-accent')
            ui.label('Portfolio Allocation is not available for this selection').classes('text-h6')
        ui.label(gate.message).classes('text-secondary-custom')
        if gate.reason_code != GATE_NO_ACCOUNT:
            with ui.row().classes('mt-2'):
                ui.button('Open Settings', icon='settings',
                          on_click=lambda: ui.navigate.to('/settings')).props('outline')


def _render_label_body(account_id: int, view, refresh, *, live=None) -> None:
    """One managed label's target box, comment box, symbol table and controls.

    ``live`` is the render's figure registry (``_new_live_state``). It is optional
    only so that the body can be drawn in isolation; without it the boxes still
    persist, but there is no base notional to recompute the money against and
    nothing else on the page to keep in step.
    """
    if live is None:
        live = _new_live_state()
    _register_view(live, view)
    label_symbols = [r.symbol for r in view.rows]
    live['weights'][view.label] = {r.symbol: float(r.weight_pct)
                                   for r in view.rows if r.weight_pct is not None}

    with ui.row().classes('w-full items-center gap-2'):
        # THE label target, on the page at last. It used to be reachable only from
        # inside the Allocate wizard, which is why every label on an untouched
        # account sat at 0 while the table below printed a share of nothing.
        target_input = ui.number(
            label='Portfolio target %', value=view.target_pct, min=0, max=100,
            step=0.01, suffix='%',
            on_change=lambda e, lbl=view.label: _save_label_target(
                account_id, live, lbl, e.value)
        ).props(f'dense outlined debounce={TARGET_DEBOUNCE_MS}').classes('w-44')
        live['label_inputs'][view.label] = target_input
        ui.input('Label comment', value=view.comment or '',
                 on_change=lambda e, lbl=view.label: _save_label_comment(account_id, lbl, e.value)
                 ).props(f'dense outlined debounce={COMMENT_DEBOUNCE_MS}').classes('flex-grow')
        ui.button('Add symbol', icon='add',
                  on_click=lambda lbl=view.label: _open_add_symbol_dialog(account_id, lbl, refresh)
                  ).props('outline dense')
    # The sentence that resolves the collision the user tripped over: the box above
    # is a share of the PORTFOLIO, the column below is a share of THIS LABEL.
    ui.label('This label’s share of the whole portfolio. The “Share of label %” '
             'column below divides THIS label’s money between its symbols — a '
             'different denominator, which is why the two can read 0 and 20 at the '
             'same time and both be right.').classes('text-xs text-secondary-custom')

    rows = [{
        'flag': '⚠' if r.multi_label else '',
        'symbol': r.symbol,
        # KEPT although the Labels COLUMN is gone: the ⚠ cell's tooltip is now the
        # only place a symbol's other managed labels are named, and it reads this.
        'labels': ', '.join(r.labels),
        'current_value': round(r.current_value, 2),
        'pct_of_label': round(r.pct_of_label, 2),
        'pct_of_total': round(r.pct_of_total, 2),
        # None, never 0.0: a symbol with no stored weight and a page with no base
        # notional have no target, and 0.00 there would be a claim rather than a gap.
        'weight_pct': None if r.weight_pct is None else round(r.weight_pct, 2),
        'target_value': None if r.target_value is None else round(r.target_value, 2),
        # Bumped when an edit is REFUSED, and used as the ``:key`` of the cell's
        # input so the refusal actually puts the typed text back -- see
        # ``_revert_symbol_cell``.
        'weight_key': 0,
        'quantity': round(r.quantity, 4),
        'cost_basis': round(r.cost_basis, 2),
        'price': None if r.price is None else round(r.price, 4),
        'market_value': None if r.market_value is None else round(r.market_value, 2),
        'comment': r.comment or '',
    } for r in view.rows]

    # The LABELS column is gone. Every row inside a label's own section repeated the
    # same value, and the section header already says which label this is; the one
    # case where the value differs -- a symbol carrying two managed labels -- is
    # named by the ⚠ cell's tooltip, which is why ``labels`` stays in the row DATA.
    # The table is built here and nowhere else (no cross-label or "all symbols" view
    # reuses it), so there is nothing to make the column conditional for.
    columns = [
        {'name': 'flag', 'label': '', 'field': 'flag', 'align': 'center'},
        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
        {'name': 'current_value', 'label': 'Current value', 'field': 'current_value', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_label', 'label': '% of label', 'field': 'pct_of_label', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_total', 'label': '% of total', 'field': 'pct_of_total', 'sortable': True, 'align': 'right'},
        # "Share of label %", not "Target %": the label header above prints a target
        # too, and that one is a share of the PORTFOLIO. Two different quantities
        # under one word is what made "target 0.0%" over a column of 20s look wrong.
        {'name': 'weight_pct', 'label': 'Share of label %', 'field': 'weight_pct', 'sortable': True, 'align': 'right'},
        {'name': 'target_value', 'label': 'Target value', 'field': 'target_value', 'sortable': True, 'align': 'right'},
        {'name': 'quantity', 'label': 'Qty', 'field': 'quantity', 'sortable': True, 'align': 'right'},
        {'name': 'cost_basis', 'label': 'Cost basis', 'field': 'cost_basis', 'sortable': True, 'align': 'right'},
        {'name': 'price', 'label': 'Price', 'field': 'price', 'sortable': True, 'align': 'right'},
        {'name': 'market_value', 'label': 'Market value', 'field': 'market_value', 'sortable': True, 'align': 'right'},
        {'name': 'comment', 'label': 'Comment', 'field': 'comment', 'align': 'left'},
    ]

    table = ui.table(columns=columns, rows=rows, row_key='symbol',
                     selection='multiple').classes('w-full dark-pagination')
    live['tables'][view.label] = table
    table.add_slot('body-cell-flag', r'''
        <q-td :props="props">
            <span v-if="props.value" :title="'Also in: ' + props.row.labels"
                  style="color:#f6ad55;font-weight:600">{{ props.value }}</span>
        </q-td>
    ''')
    # ``:key`` is load-bearing, not decoration: the input is bound to
    # ``props.value``, so Vue's watcher only fires when THAT changes -- and a
    # REFUSED edit leaves it unchanged by definition. Bumping the key remounts the
    # input from the row data, which is how a rejected number gets put back instead
    # of sitting on screen as a value the database does not have.
    table.add_slot('body-cell-weight_pct', r'''
        <q-td :props="props">
            <q-input :key="props.row.weight_key" :model-value="props.value"
                     type="number" dense borderless input-class="text-right"
                     debounce="''' + str(TARGET_DEBOUNCE_MS) + r'''"
                     @update:model-value="(val) => $parent.$emit('weightChange', props.row.symbol, val)" />
        </q-td>
    ''')
    table.on('weightChange',
             lambda e, lbl=view.label, syms=label_symbols: _save_symbol_weight(
                 account_id, live, lbl, e.args[0], e.args[1], syms))
    # ``debounce`` matters here: without it every keystroke fires a round trip that
    # reads the label's whole weight map and writes a row, on the event loop.
    table.add_slot('body-cell-comment', r'''
        <q-td :props="props">
            <q-input :model-value="props.value" dense borderless debounce="''' +
                   str(COMMENT_DEBOUNCE_MS) + r'''"
                     @update:model-value="(val) => $parent.$emit('commentChange', props.row.symbol, val)" />
        </q-td>
    ''')
    table.on('commentChange',
             lambda e, lbl=view.label, syms=label_symbols: _save_symbol_comment(
                 account_id, lbl, e.args[0], e.args[1], syms))

    async def _remove_selected() -> None:
        symbols = [r['symbol'] for r in (table.selected or [])]
        if not symbols:
            ui.notify('Tick one or more symbols first', type='warning')
            return
        try:
            removed = await asyncio.to_thread(
                remove_symbols_from_label, account_id, view.label, symbols)
        except Exception as e:
            logger.error(f"Removing {symbols} from '{view.label}' failed: {e}", exc_info=True)
            ui.notify(f'Could not remove: {e}', type='negative')
            return
        ui.notify(f"Removed {removed} symbol(s) from '{view.label}'", type='positive')
        await refresh()

    with ui.row().classes('w-full justify-end'):
        ui.button('Remove selected from label', icon='delete', on_click=_remove_selected
                  ).props('outline color=negative dense')


def _render_reserve_card(account_id: int, live: Dict[str, Any]) -> None:
    """The editable cash reserve: a slider, a bound number box and the money.

    ONE stored field, ``PortfolioAllocationConfig.unallocated_pct``, and two
    controls over it -- whichever moves writes it and mirrors the other. The DOLLAR
    figure beside them is derived and read-only: the user asked for "a field for the
    value we want to keep", but taking one would need ``pct = dollars / base``,
    which is undefined on the accounts that have no base and would silently disagree
    with the slider on the ones that do. Percent stays the single source of truth.

    The slider steps in whole percent because dragging a 10,000-notch track is
    unusable; the box takes two decimals. Quasar's slider does not re-emit a model
    value it was GIVEN, so setting 12.34 on it from the box cannot snap the stored
    figure back to 12.
    """
    with ui.column().classes('stat-card p-3'):
        ui.label('Unallocated reserve').classes('text-xs text-secondary-custom')
        with ui.row().classes('items-center gap-3 no-wrap'):
            slider = ui.slider(min=0, max=100, step=1,
                               value=live['unallocated_pct']).classes('w-40')
            number = ui.number(value=live['unallocated_pct'], min=0, max=100, step=0.01,
                               suffix='%').props('dense outlined').classes('w-28') \
                .style(TABULAR_NUMS)
        live['reserve_slider'] = slider
        live['reserve_number'] = number
        slider.on_value_change(lambda e: _save_reserve(account_id, live, e.value,
                                                       echo_to=number))
        number.on_value_change(lambda e: _save_reserve(account_id, live, e.value,
                                                       echo_to=slider))
        live['reserve_caption'] = ui.label(
            format_reserve_caption(live['base_notional'], live['unallocated_pct'])
        ).classes('text-xs text-secondary-custom').style(TABULAR_NUMS)


def _render_label_bar_row(account_id: int, live: Dict[str, Any], view, refresh) -> None:
    """One label: the mini-bar header row, and the symbol table it folds open.

    ``expand-icon-toggle`` is deliberate. The header now carries a control (the edit
    pencil), and Quasar's default is that a click ANYWHERE on the header folds the
    section -- so the pencil would open the label and the same click would shut it
    again. With this prop only the chevron toggles, and the pencil's own listener is
    registered as ``click.stop`` on top of that. Belt and braces, because the failure
    is invisible in a unit test and infuriating in a browser.
    """
    expansion = ui.expansion('').classes('w-full').props('expand-icon-toggle')
    live['expansions'][view.label] = expansion
    widgets: Dict[str, Any] = {}
    live['bars'][view.label] = widgets

    with expansion.add_slot('header'):
        with ui.row().classes('w-full items-center gap-3 no-wrap') \
                .style(TABULAR_NUMS).mark(MARKER_BAR_ROW):
            ui.icon('label').style(f'color: {resolve_label_icon_color(view.color)}')
            ui.label(view.label).classes('w-48 truncate font-medium')
            widgets['value'] = ui.label('').classes('w-28 text-right')
            with ui.element('div').classes('flex-grow min-w-[80px]').style(BAR_TRACK_STYLE):
                # Marked, because the two are otherwise indistinguishable bare divs
                # whose whole content is an inline style -- and the geometry IS the
                # information here, so a test has to be able to read it back.
                widgets['fill'] = ui.element('div').mark(MARKER_BAR_FILL)
                widgets['notch'] = ui.element('div').mark(MARKER_BAR_NOTCH)
            widgets['pct'] = ui.label('').classes('w-16 text-right')
            widgets['target'] = ui.label('').classes('w-24 text-right')
            widgets['status'] = ui.label('').classes('w-14')
            # The pencil. It OPENS the label and focuses its target box; it never
            # closes one, because "edit this" is not a toggle.
            ui.icon('edit').classes('cursor-pointer text-secondary-custom') \
                .on('click.stop',
                    lambda _e=None, lbl=view.label: _focus_label_target(live, lbl)) \
                .tooltip('Edit this label’s portfolio target')
            # The ⓘ that took the long clause off the header line. Its tooltip is
            # created ONCE and re-texted on every redraw: calling ``.tooltip()``
            # again would add a second tooltip element, and by the tenth keystroke
            # there would be ten of them stacked on one icon.
            with ui.icon('info_outline').classes('text-secondary-custom') as info:
                widgets['tooltip'] = ui.tooltip('')
            widgets['info'] = info
    # The expansion's own ``label`` prop is kept in step with the header slot: it is
    # what a screen reader and a collapsed-state tooltip read, and it is the one
    # place the whole sentence still exists.
    live['captions'][view.label] = [expansion]

    with expansion:
        _render_label_body(account_id, view, refresh, live=live)


def _render_labels(account_id: int, payload: Dict[str, Any], refresh) -> None:
    # Biggest holding first. The 39.5% row used to sit between two 1-5% rows,
    # because the order was whatever ``sort_order`` happened to be.
    views = sort_label_views(payload['views'])
    if not views:
        with ui.element('div').classes('alert-banner info w-full p-3'):
            ui.label('No labels are managed for this account yet — click "Manage labels".')
        return

    mode = payload['valuation_mode']
    base_notional = payload['base_notional']
    buying_power = payload['available_buying_power']
    live = _new_live_state(base_notional=base_notional,
                           available_buying_power=buying_power,
                           unallocated_pct=payload['unallocated_pct'])
    mode_label = ('cost basis (what you paid)' if mode == VALUATION_MODE_COST
                  else 'market value (qty x price)')
    # NOT sum(v.current_value ...): that counts a symbol once per managed label,
    # while every pct_of_total below was divided by the DISTINCT total.
    total = managed_total_value(views)
    # Every decision here -- which snapshot field, what an unknown reads as,
    # whether the leverage clause can be stated -- is in the pure module. This
    # draws three labels.
    value_card = account_value_card(account_value=payload['account_value'],
                                    managed_value=total)
    with ui.row().classes('w-full gap-4 items-start'):
        with ui.column().classes('stat-card p-3'):
            ui.label(f'Managed value — {mode_label}').classes('text-xs text-secondary-custom')
            ui.label(f'${total:,.2f}').classes('text-lg font-bold').style(TABULAR_NUMS)
        # SECOND, immediately beside the managed value, because the pair is the
        # point: on a margin account the first exceeds this one and the page used
        # to show only the first. Drawn UNCONDITIONALLY, unlike the buying-power
        # card below -- a card that vanishes when the broker will not answer is
        # indistinguishable from a page that never had one.
        with ui.column().classes('stat-card p-3'):
            ui.label(value_card.title).classes('text-xs text-secondary-custom')
            ui.label(value_card.text).classes('text-lg font-bold').style(TABULAR_NUMS)
            if value_card.detail:
                ui.label(value_card.detail).classes('text-xs text-secondary-custom')
        with ui.column().classes('stat-card p-3'):
            ui.label('Managed labels').classes('text-xs text-secondary-custom')
            ui.label(str(len(views))).classes('text-lg font-bold')
        if buying_power is not None:
            with ui.column().classes('stat-card p-3'):
                ui.label('Free buying power').classes('text-xs text-secondary-custom')
                ui.label(f'${buying_power:,.2f}').classes('text-lg font-bold') \
                    .style(TABULAR_NUMS)
        _render_reserve_card(account_id, live)

    # The running over/under-100 check, on the PAGE and without a dry run -- the
    # same advisory the wizard's step 1 has always shown, moved here with the boxes.
    live['total_notice'] = ui.label('').classes('text-xs text-orange-400')

    # DANGER, not warning, since market became the default (W1). This is no longer
    # "your percentages are slightly off": those positions contribute 0 to the
    # allocatable base, so every label's target is understated by its share of the
    # missing money, and both the wizard and ``run_allocation`` now REFUSE to
    # submit against such a base (``held_no_price_block``). The banner has to look
    # like the refusal it is, or the user reads past it and presses Allocate.
    unpriced = missing_quote_symbols(views)
    if unpriced and mode == VALUATION_MODE_MARKET:
        with ui.element('div').classes('alert-banner danger w-full p-3'):
            ui.label(f"No quote for {len(unpriced)} held symbol(s): "
                     f"{', '.join(unpriced)}")
            ui.label('They are valued at $0.00 in market mode, which is NOT the same '
                     'as being flat — switch to cost basis, or retry the quote.'
                     ).classes('text-xs text-secondary-custom')
            ui.label('Allocate is blocked while this is true: those positions are '
                     'missing from the allocatable base, so every target below is '
                     'smaller than it should be.').classes('text-xs')

    ui.label('Prices are the broker feed (Alpaca defaults to delayed_sip — 15 minutes '
             'delayed). Only symbols carrying a managed label are listed. Short '
             'positions carry a negative quantity, cost basis and value.'
             ).classes('text-xs text-secondary-custom')

    # The UNALLOCATED group, FIRST and not expandable: it is the row that says how
    # much of the book is even in play, and below the labels it reads as a footnote.
    # Its target is the STORED reserve; edit it in the Allocate wizard, which is
    # where the change is validated and where the dry run that acts on it lives.
    # Omitted entirely when there is no base: better a missing row than one
    # measured against a guess.
    #
    # ``not base_notional``, NOT ``base_notional is None``. A base of exactly 0.0
    # is a real state -- a brand-new or fully-withdrawn account -- and it is not a
    # denominator, so ``unallocated_row`` reports ``pct_of_base=None`` for it just
    # as ``build_label_views`` treats a 0 base as absent. The old ``is not None``
    # guard let that one value through and the ``:.1f`` below raised on the None,
    # 500-ing the entire page. ``buying_power`` keeps ``is not None``: 0.00 of free
    # buying power is a fact worth drawing, and only None means "unknown".
    reserve_text = format_reserve_row(base_notional=base_notional,
                                      available_buying_power=buying_power,
                                      unallocated_pct=payload['unallocated_pct'])
    if reserve_text is not None:
        with ui.element('div').classes('alert-banner info w-full p-3'):
            # "of base", SAID OUT LOUD, because the label headers below print a
            # target too and theirs is a RELATIVE weight on what this row leaves.
            # In identical grammar the two read as one column and sum past 100 --
            # a 25% reserve above a single label at 100% rendered "target 25.00%"
            # over "target 100.0%" for a book that is exactly 25 + 75.
            live['reserve_row'] = ui.label(reserve_text).style(TABULAR_NUMS)
            # Said out loud, because it is the counter-intuitive half: a reserve on
            # a fully invested book is funded by SELLING, and the dry run is where
            # that becomes visible. Drawn unconditionally now that the reserve is
            # editable in place -- a caption that appears only above 0% is one the
            # user never reads before moving the slider.
            ui.label('The labels below divide what is left after this — raising it '
                     'on a fully invested account will generate sell orders.'
                     ).classes('text-xs text-secondary-custom')

    for view in views:
        # Registered by ``_render_label_body`` inside the row, not here: two calls
        # to ``_register_view`` for one view made its idempotence guard the only
        # thing standing between the page and a doubled bar list, and a guard whose
        # removal changes nothing observable is a guard no test can hold.
        _render_label_bar_row(account_id, live, view, refresh)

    with ui.row().classes('w-full items-center gap-3'):
        live['footer'] = ui.label('').style(TABULAR_NUMS)
        ui.label('███ current    ╎ target notch').classes('text-xs text-secondary-custom')

    # Everything derived, drawn once through the SAME path an edit uses -- so the
    # first paint and every keystroke after it cannot land on different numbers.
    _apply_page_figures(live)


def _market_gate_for(hours):
    """Build the banner's gate from a MarketHours. The ONLY legal caller mapping.

    An UNAVAILABLE answer carries ``is_open=False`` so the money path fails closed,
    but ``is_known`` is False, so the UI must say "unknown" rather than "closed" --
    they have different fixes. ``now`` is passed explicitly; the pure function has no
    clock of its own.
    """
    is_open = None if (hours is None or not hours.is_known) else hours.is_open
    source = hours.source if hours is not None else MARKET_SOURCE_UNAVAILABLE
    return evaluate_market_gate(is_open=is_open,
                                next_open=(hours.next_open if hours is not None else None),
                                source=source, now=datetime.now(timezone.utc))


async def _open_allocation_flow(account_id: int, valuation_mode: str,
                                refresh, *, mode: str = ALLOCATION_MODE_REBALANCE,
                                invest_amount: float = 0.0) -> None:
    """The Allocate button: steps 1-3, then the dry run, then Submit.

    Every blocking call is dispatched through ``asyncio.to_thread``: broker IO on the
    event loop freezes the app for every connected client.
    """
    try:
        (base, labels, allow_fractional, symbol_values, positions,
         unallocated_pct) = await asyncio.to_thread(
            _load_flow_inputs, account_id, valuation_mode)
    except PositionFetchFailed as e:
        logger.error(f"Portfolio allocation: position fetch failed: {e}")
        ui.notify(f'Broker position fetch FAILED: {e} - nothing is planned against a '
                  f'guess', type='negative')
        return
    except Exception as e:
        logger.error(f"Could not open the allocation wizard: {e}", exc_info=True)
        ui.notify(f'Could not open the allocation wizard: {e}', type='negative')
        return
    if not labels:
        ui.notify('No managed labels yet - use "Manage labels" first', type='warning')
        return

    state = {'mode': mode, 'scope_label': None, 'amount': float(invest_amount or 0.0),
             'labels': labels, 'allow_fractional': allow_fractional,
             'unallocated_pct': unallocated_pct,
             'base': base, 'current': {}}

    def _persist_choices(mode: str, labels, allow_fractional: bool,
                         unallocated_pct: float) -> None:
        """Write what this run was launched with. Blocking; one thread hop.

        All three writes happen on CONTINUE, not on Submit, and that is what "last"
        means here: the numbers the user last chose to allocate with, whether or
        not they went through with the orders. The fractional switch has been
        persisted from exactly this point since it shipped
        (``remember_fractional_choice``); the targets and the reserve follow it.

        An INVEST_LABEL run passes ``save_label_targets=False`` and does NOT touch
        the reserve: it spends an explicit amount on one label, so that label's
        percentage played no part, and its ``unallocated_pct`` is a hard 0 that
        would ZERO a reserve the user set on the rebalance side. Its symbol weights
        DID split the money, so they are still saved.
        """
        svc.remember_fractional_choice(account_id, bool(allow_fractional))
        if mode != ALLOCATION_MODE_INVEST_LABEL:
            set_allocation_config(account_id, unallocated_pct=float(unallocated_pct))
        return save_allocation_targets(
            account_id, labels,
            save_label_targets=(mode != ALLOCATION_MODE_INVEST_LABEL))

    async def _save_choices(mode: str, labels, allow_fractional: bool,
                            unallocated_pct: float) -> None:
        try:
            written = await asyncio.to_thread(_persist_choices, mode, labels,
                                              bool(allow_fractional),
                                              float(unallocated_pct))
        except Exception as e:
            # Reported, then out of the way. Persisting is a convenience; SOLVING is
            # what the user pressed Continue for, and refusing to open the dry run
            # over a failed write would be a much worse trade.
            logger.error(f"Saving allocation targets for account {account_id} failed: {e}",
                         exc_info=True)
            ui.notify(f'Targets could not be saved: {e}', type='negative')
            return
        if written['skipped_labels']:
            # Silently dropping these is how a user comes back tomorrow to numbers
            # they did not choose.
            ui.notify(f"{written['skipped_labels']} label(s) are no longer managed "
                      f"and their targets were not saved — refresh the page",
                      type='warning')

    async def _run_dry_run() -> None:
        # Persist BEFORE solving. This is the Continue action, and Continue is what
        # "last" means: the numbers the user last chose to allocate with, whether or
        # not they then went through with the orders.
        await _save_choices(state['mode'], state['labels'], state['allow_fractional'],
                            state['unallocated_pct'])
        try:
            new_base, plan, current, hours = await asyncio.to_thread(
                _solve_plan, account_id, mode=state['mode'], labels=state['labels'],
                scope_label=state['scope_label'], amount=state['amount'],
                allow_fractional=state['allow_fractional'],
                valuation_mode=valuation_mode,
                unallocated_pct=state['unallocated_pct'])
        except PositionFetchFailed as e:
            logger.error(f"Portfolio allocation dry run: position fetch failed: {e}")
            ui.notify(f'Broker position fetch FAILED: {e}', type='negative')
            return
        except Exception as e:
            logger.error(f"Allocation dry run failed: {e}", exc_info=True)
            ui.notify(f'Dry run failed: {e}', type='negative')
            return
        state['base'] = new_base
        state['current'] = current

        def _on_refresh(allow_fractional: bool):
            """Called from the wizard (sync): re-solve, and hand back the CLOCK too.

            The market hours are part of the same solve -- ``_solve_plan`` reads
            them once so the banner and the gate cannot describe different instants
            -- and they used to be thrown away here into ``_``, which left the
            wizard's gate frozen at whatever it was when the dialog opened. The
            broker's cached answer is dropped first: a user pressing Refresh at
            09:31 is asking exactly the question the cache is holding an old answer
            to.
            """
            state['allow_fractional'] = bool(allow_fractional)
            svc.remember_fractional_choice(account_id, bool(allow_fractional))
            fresh_base, fresh_plan, fresh_current, fresh_hours = _solve_plan(
                account_id, mode=state['mode'], labels=state['labels'],
                scope_label=state['scope_label'], amount=state['amount'],
                allow_fractional=bool(allow_fractional), valuation_mode=valuation_mode,
                # The reserve the dialog was CONTINUED with, not a re-read: Refresh
                # re-prices the book, it does not re-open the question.
                unallocated_pct=state['unallocated_pct'],
                force_market_refresh=True)
            state['base'] = fresh_base
            state['current'] = fresh_current
            return fresh_plan, _market_gate_for(fresh_hours)

        def _on_submit(selected_plan) -> None:
            ui.timer(0.1, lambda: _do_submit(selected_plan), once=True)

        open_allocation_wizard(new_base, plan, market=_market_gate_for(hours),
                               on_refresh=_on_refresh, on_submit=_on_submit)

    async def _do_submit(selected_plan) -> None:
        try:
            result = await asyncio.to_thread(
                _submit_plan, account_id, selected_plan, state['current'],
                state['base'], mode=state['mode'], scope_label=state['scope_label'])
        except Exception as e:
            logger.error(f"Allocation submission failed: {e}", exc_info=True)
            ui.notify(f'Submission failed: {e}', type='negative')
            return
        if result['blocked']:
            # The service re-checked the gate on its own, freshly: this dialog can
            # sit open across 16:00 and the banner it was built with is now stale.
            ui.notify(result['blocked_reason'], type='warning')
            return
        render_outcomes(result['outcomes'], run_id=result['run_id'])
        note = working_orders_notice(settled=result['settled'],
                                     working_order_ids=result['working_order_ids'],
                                     refresh_failed=result['refresh_failed'])
        if note is not None:
            ui.notify(note[0], type=note[1])
        await refresh()

    def _on_dry_run(*, mode: str, labels, scope_label, amount: float,
                    allow_fractional: bool, unallocated_pct: float) -> None:
        """Called by the steps dialog (sync). Records the choices, then solves.

        The write itself is in ``_run_dry_run``, off the event loop -- unlike the
        fractional switch this is N labels and M symbols, and the page's own rule
        is that blocking work goes through ``asyncio.to_thread``.
        """
        state.update({'mode': mode, 'labels': labels, 'scope_label': scope_label,
                      'amount': float(amount or 0.0),
                      'allow_fractional': bool(allow_fractional),
                      'unallocated_pct': float(unallocated_pct or 0.0)})
        ui.timer(0.1, _run_dry_run, once=True)

    open_allocation_steps(base, labels, on_dry_run=_on_dry_run,
                          allow_fractional=allow_fractional,
                          mode=state['mode'], invest_amount=state['amount'],
                          symbol_values=symbol_values, positions=positions,
                          unallocated_pct=unallocated_pct)


async def _open_invest_flow(account_id: int, valuation_mode: str, amount: float,
                            refresh) -> None:
    """The income panel's Invest button: the same flow, opened in INVEST_LABEL mode
    and pre-filled with the unallocated income."""
    await _open_allocation_flow(account_id, valuation_mode, refresh,
                                mode=ALLOCATION_MODE_INVEST_LABEL,
                                invest_amount=amount)


async def content() -> None:
    """Entry point for the /portfolioallocation route."""
    account_id = get_selected_account_id()
    logger.debug(f"[PAGE] portfolio_allocation.content() account_id={account_id}")

    with ui.column().classes('w-full gap-4'):
        with ui.row().classes('w-full items-center justify-between'):
            ui.label('📊 Portfolio Allocation').classes('text-h6')
            ui.label('Manually traded accounts only').classes('text-xs text-secondary-custom')

        # Both loaders touch the DB. Unguarded, a DB error here escapes the route
        # handler and NiceGUI answers 500 with nothing on the page to explain it.
        try:
            gate = await asyncio.to_thread(_load_gate, account_id)
        except Exception as e:
            logger.error(f"Portfolio allocation gate failed for account {account_id}: {e}",
                         exc_info=True)
            with ui.element('div').classes('alert-banner danger w-full p-3'):
                ui.label(f'Could not load this account: {e}')
            return
        if not gate.allowed:
            _render_gate_blocked(gate)
            return

        toolbar = ui.row().classes('w-full items-center gap-2')
        body = ui.column().classes('w-full gap-3')
        try:
            mode_state = {'value': await asyncio.to_thread(_load_valuation_mode, account_id)}
        except Exception as e:
            logger.error(f"Portfolio allocation: valuation mode unreadable for account "
                         f"{account_id}: {e}", exc_info=True)
            with ui.element('div').classes('alert-banner danger w-full p-3'):
                ui.label(f'Could not load the valuation mode: {e}')
            return

        async def _refresh() -> None:
            body.clear()
            with body:
                ui.spinner(size='lg').classes('self-center')
            try:
                payload = await asyncio.to_thread(
                    _load_view_payload, account_id, mode_state['value'])
            except PositionFetchFailed as e:
                logger.error(f"Portfolio allocation: position fetch failed: {e}")
                body.clear()
                with body:
                    with ui.element('div').classes('alert-banner danger w-full p-3'):
                        ui.label(f'Broker position fetch FAILED: {e}')
                        ui.label('Nothing is shown until the broker answers — a failed '
                                 'fetch and a flat account are not the same thing.'
                                 ).classes('text-xs text-secondary-custom')
                return
            except Exception as e:
                logger.error(f"Portfolio allocation refresh failed: {e}", exc_info=True)
                body.clear()
                with body:
                    with ui.element('div').classes('alert-banner danger w-full p-3'):
                        ui.label(f'Could not load allocation: {e}')
                return
            body.clear()
            with body:
                _render_labels(account_id, payload, _refresh)
                try:
                    events, open_total, working_note = await asyncio.to_thread(
                        _load_income_panel, account_id)
                except Exception as e:
                    logger.error(f"Income panel failed to load: {e}", exc_info=True)
                    events, open_total, working_note = [], 0.0, None
                    ui.label(f'Income could not be loaded: {e}') \
                        .classes('text-xs text-orange-400')
                render_income_panel(
                    events, open_total, working_note=working_note,
                    on_sync=lambda: ui.timer(0.1, _refresh, once=True),
                    on_invest=lambda amount: ui.timer(
                        0.1, lambda: _open_invest_flow(
                            account_id, mode_state['value'], amount, _refresh),
                        once=True))

        async def _set_mode(event) -> None:
            """Persist the mode EAGERLY and RE-COMPUTE -- never reinterpret silently."""
            chosen = event.value
            if not chosen or chosen == mode_state['value']:
                return
            try:
                await asyncio.to_thread(set_allocation_config, account_id,
                                        valuation_mode=chosen)
            except Exception as e:
                logger.error(f"Saving valuation mode failed: {e}", exc_info=True)
                ui.notify(f'Could not save valuation mode: {e}', type='negative')
                return
            mode_state['value'] = chosen
            ui.notify(f'Valuation mode: {chosen}', type='info')
            await _refresh()

        with toolbar:
            ui.button('Allocate', icon='account_balance',
                      on_click=lambda: _open_allocation_flow(
                          account_id, mode_state['value'], _refresh)) \
                .props('color=primary')
            ui.select({VALUATION_MODE_COST: 'Cost basis',
                       VALUATION_MODE_MARKET: 'Market value'},
                      value=mode_state['value'], label='Valuation',
                      on_change=_set_mode).props('dense outlined').classes('w-44')
            ui.button('Manage labels', icon='pie_chart',
                      on_click=lambda: _open_label_picker(account_id, _refresh)).props('outline')
            ui.button('Refresh', icon='refresh', on_click=_refresh).props('outline')

        await _refresh()
