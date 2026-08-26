"""Portfolio Allocation page — manually traded accounts only.

Shows the account's current allocation, grouped by the instrument labels the user
chose to manage, and IS WHERE THE TARGETS ARE SET. Every decision this page makes
lives in the pure, unit-tested module
``ba2_trade_platform/ui/utils/portfolio_allocation_view.py``; this file only does
IO (broker + DB) and draws widgets.

EVERY TARGET IS SET HERE, AND THAT IS THE POINT
===============================================
Each label's PORTFOLIO target, each symbol's SHARE OF ITS LABEL, the account's
cash RESERVE, the per-row ``last`` target and P&L, and the six buttons over them:
``Even split`` / ``Load last`` across the labels, and ``Fill 100%`` /
``Even split`` / ``Fill rest`` / ``Load last`` / ``Wipe`` within one label. All of
it used to be reachable only from inside the Allocate wizard's step 1 and step 2,
which meant that on an account nobody had run the wizard on every label sat at
``target_pct = 0`` while the symbol table cheerfully printed a 20% share --
``get_symbol_weights``'s even-split default -- resolving to a target value of
$0.00, because 20% of a 0% label is nothing. The page showed a plausible number
that meant nothing.

The main toolbar button is still here, and it is now purely for EXECUTING against
the saved targets: it opens the dry run DIRECTLY -- precheck, review, submit. It is
called ``Review and Submit`` (``REVIEW_BUTTON_LABEL``) rather than ``Allocate``
because that is now literally what it does: nothing is ordered until Submit is
pressed inside the gate. There is no target step in front of it any more, because a second place to type a target is
a second answer to "what am I aiming at", and the two screens derived every one of
those figures independently. ``_load_flow_inputs`` re-reads the stored rows at the
moment the dry run opens, so a fresh inline edit is what the plan is solved with.

The one control that stays at the gate is ``allow fractional shares``: it changes
WHICH ORDERS are produced rather than what is being aimed at, and toggling it
re-solves the plan.

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

WHAT A SYMBOL IS, WITHOUT LEAVING THE PAGE
==========================================
Two entry points into ``ui/components/symbol_info_panel.py`` (holdings,
dividends, total return, chart): the ⓘ on each symbol row, and Compare over the
rows ticked in one label's table. Both go through ``_open_symbol_info``, so the
FMP-key and empty-selection refusals exist once, and the panel is built ON CLICK
-- never at render -- because it fetches over the network.

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
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from nicegui import ui
from sqlmodel import select

from ...config import get_app_setting
from ...core import portfolio_allocation_service as svc
from ...core.db import get_db
from ...core.models import ExpertInstance
from ...core.portfolio_allocation import (
    ALLOCATION_MODE_INVEST_LABEL, ALLOCATION_MODE_REBALANCE,
    VALUATION_MODE_COST, VALUATION_MODE_MARKET,
    LabelTarget, SymbolTarget, build_base_snapshot, compute_allocation,
    compute_base_notional, compute_label_investment, current_value,
    format_unrealised_pnl, unconsumed_income_notice,
)
from ...core.portfolio_allocation_store import (
    add_symbols_to_label, get_allocation_config, get_managed_labels, get_symbol_comments,
    get_previous_symbol_weights, get_symbol_rows, get_symbol_weights,
    remove_symbols_from_label, replace_managed_labels, save_allocation_targets,
    set_allocation_config, set_managed_label, set_symbol_weight,
)
from ...logger import logger
from ..account_filter_context import get_selected_account_id
# The MODULE, deliberately: the panel is absent from ``ui/components/__init__.py``
# (same convention as ``symbol_chart_data`` and ``echart_theme``) so importing it
# does not grow the eager import graph every page already pays for.
from ..components.symbol_info_panel import open_symbol_info
from ..utils.portfolio_allocation_view import (
    BASIS_LEGEND, DEFAULT_LABEL_ICON_COLOR, DEFAULT_MACHINE_LABEL_FAMILIES,
    GATE_NO_ACCOUNT,
    LABEL_COLOR_PALETTE, LABEL_STATUS_CLASSES, LABEL_TARGET_CAPTION,
    LABEL_TOOLTIP_STYLE, label_column_width_ch, label_status_color,
    MARKET_SOURCE_UNAVAILABLE,
    NEUTRAL_TEXT_COLOR,
    NO_LABEL_COLOR, RESERVE_BASIS_NOTE,
    GateResult, ManagedLabel,
    PositionFetchFailed, account_value_from_snapshot,
    build_label_bars, build_label_views, summary_figures,
    collect_managed_symbols, diff_managed_labels,
    describe_label_color,
    evaluate_gate, evaluate_market_gate,
    even_split_symbol_shares, expert_shortname_families,
    fill_label_to_100, fill_rest_symbol_shares,
    format_allocation_footer, format_label_header,
    format_label_target_tooltip,
    format_reserve_caption,
    format_reserve_row, label_color_contrast_warning,
    ALLOCATION_BAR_LEGEND, allocation_bar, band_color, RESERVE_SELL_WARNING,
    SHARE_DEFAULT_NOTE, symbol_total_bar, SYMBOL_TOTAL_BAR_CAPTION,
    LABEL_TOTAL_BAR_CAPTION, LABEL_TOTAL_BAR_LEGEND, LABEL_TOTAL_CLASSES,
    LABEL_TOTAL_COLORS, LABEL_TOTAL_TOOLTIP, class_color_style,
    label_total_readout,
    load_current_symbol_shares, load_last_symbol_shares, managed_total_value,
    important_color_style,
    missing_quote_symbols, picker_options, pnl_classes, pnl_color,
    positions_by_symbol,
    resolve_label_icon_color, resolve_symbol_weights,
    sort_label_views, store_color_value,
    symbol_target_values,
    validate_label_target_edit, validate_reserve_edit, validate_symbol_weight_edit,
    wipe_symbol_shares, working_orders_notice,
)
from .portfolio_allocation_wizard import (
    open_allocation_wizard, open_invest_scope, render_income_panel, render_outcomes,
)

#: The toolbar's main button. It was called ``Allocate``, and that name outlived
#: what it does: the three-step wizard behind it was the TARGET EDITOR, so pressing
#: it really was the act of allocating. Every one of those steps is on this page
#: now and the button opens the dry run -- a review-and-commit gate where nothing
#: is written until Submit. "Allocate" promised an action the button no longer
#: takes, which is the worst thing a button caption can do on a page that spends
#: real money.
REVIEW_BUTTON_LABEL = 'Review and Submit'

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

    # ``previous_target_pct`` travels with the label, NULL and all: it is what the
    # row prints as "last N%" and what the page's Load-last reads, and a ``or 0.0``
    # anywhere on this path would turn "never allocated" into "allocated nothing".
    managed = [ManagedLabel(label=row.label, target_pct=row.target_pct,
                            comment=row.comment, color=row.color,
                            previous_target_pct=row.previous_target_pct)
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
    previous_weights: Dict[str, Dict[str, Optional[float]]] = {}
    for entry in managed:
        for symbol, text in get_symbol_comments(account_id, entry.label).items():
            comments[(entry.label, symbol)] = text
        members = symbols_by_label.get(entry.label, [])
        # THE SAVED ROWS ONLY -- ``get_symbol_rows``, not ``get_symbol_weights``.
        # That one fills an absent row with the FAIR SHARE, which is precisely the
        # default being removed: nine symbols in a label all reading 11.11 while
        # their real shares were 26.78 / 22.19 / ... The effective share is decided
        # in ``resolve_symbol_weights`` (saved wins, else the actual share, else
        # blank), and it needs to be told which rows are genuinely saved.
        weights[entry.label] = {symbol: float(row.weight_pct)
                                for symbol, row
                                in get_symbol_rows(account_id, entry.label).items()
                                if symbol in members}
        # A SEPARATE reader from ``get_symbol_weights`` on purpose: that one fills
        # an absent row with the even-split default, and there is no default for a
        # weight nobody has ever allocated with.
        previous_weights[entry.label] = get_previous_symbol_weights(
            account_id, entry.label, members)

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
                                   symbol_previous_weights=previous_weights,
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


def _is_unmeasurable_holding(state, valuation_mode: str) -> bool:
    """Held, but with no price -- so its value cannot be measured AT ALL.

    The page's own copy of this lives inside ``build_label_views`` (which is handed
    a separate price map rather than priced states); this one is for the SOLVE
    path, where ``build_position_states`` has already stamped the quote onto the
    state. Both answer the same question the same way, and both matter: an
    unmeasurable holding has no knowable share of its label, and 0% is not the
    answer -- 0% means sell it all.

    MARKET mode only. In COST mode the value IS the cost basis, which the broker
    always publishes. A FLAT symbol is measurable too: it is worth exactly nothing.
    """
    if valuation_mode != VALUATION_MODE_MARKET or state is None:
        return False
    return bool(state.quantity) and state.price is None


def _load_valuation_mode(account_id: int) -> str:
    """The account's stored valuation mode, creating the config row on first use."""
    return get_allocation_config(account_id).valuation_mode


def _load_flow_inputs(account_id: int, valuation_mode: str):
    """Everything the flow needs to solve, in one thread hop. Blocking.

    Returns:
        Tuple: ``(base, labels, allow_fractional, unallocated_pct)`` -- the frozen
        base snapshot, the managed labels with their symbol weights AND the
        previous generation of both, the account's remembered fractional choice,
        and the stored cash reserve the plan is solved against.

        It used to hand back two more maps -- ``symbol_values`` and ``positions``
        -- purely so the wizard's step-1/step-2 captions could draw a "now" figure
        and an unrealised P&L. Those captions are on the PAGE now, built from
        ``_load_view_payload``'s own read, so the two maps had no consumer left. A
        display-only value threaded through a solve path is exactly the kind of
        passenger that later gets mistaken for an input.

        THE STORED ROWS ARE RE-READ HERE, at the moment the dry run opens. That is
        what makes a fresh inline edit the thing the plan is solved against: the
        page persists on change, and this reads the same columns back.

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

    # THE BOOK FIRST: the solve path resolves an unsaved share the same way the
    # page displays it -- saved wins, else the symbol's ACTUAL share of its label
    # -- and that needs the positions and their prices, so they are read before
    # the labels rather than after.
    current = svc.build_position_states(account, symbols)

    labels = []
    for row in managed:
        members = symbols_by_label.get(row.label, [])
        saved = {symbol: float(stored.weight_pct)
                 for symbol, stored in get_symbol_rows(account_id, row.label).items()
                 if symbol in members}
        # THE SAME resolver the page's table is built from, so the number on
        # screen and the number the plan solves against cannot be two different
        # defaults. ``fair_share`` is the last resort and only reachable for an
        # UNMEASURABLE symbol -- see below.
        resolved = resolve_symbol_weights(
            members, saved=saved,
            values={s: current_value(current.get(s), valuation_mode) for s in members},
            unmeasurable=[s for s in members
                          if _is_unmeasurable_holding(current.get(s), valuation_mode)])
        # ``SymbolTarget.weight_pct`` is a float and the engine reads 0 as "hold
        # none of this", so an UNKNOWN share may not travel as 0 -- that would sell
        # the position out. It falls back to the historical fair share instead,
        # which is what this path did for every unsaved symbol until now; and the
        # only way to reach it is a held symbol with no price, which
        # ``held_no_price_block`` already refuses to SUBMIT against.
        fair_share = get_symbol_weights(account_id, row.label, members)
        # NULL stays None all the way to the dialog: "there is no last" is what
        # disables the Load-last button, and it is a different fact from 0.0.
        previous_weights = get_previous_symbol_weights(account_id, row.label, members)
        labels.append(LabelTarget(
            label=row.label, target_pct=float(row.target_pct or 0.0),
            symbols=[SymbolTarget(
                symbol=s,
                weight_pct=(float(fair_share.get(s, 0.0))
                            if resolved[s].weight_pct is None
                            else float(resolved[s].weight_pct)),
                previous_weight_pct=previous_weights.get(s)) for s in members],
            comment=row.comment,
            previous_target_pct=row.previous_target_pct))

    base = build_base_snapshot(account.get_account_snapshot(), current, symbols,
                               valuation_mode=valuation_mode)
    config = get_allocation_config(account_id)
    return (base, labels, bool(config.allow_fractional),
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
#:
#: The COLOUR comes from ``LABEL_TOTAL_COLORS``, not from these class names --
#: neither ``.text-orange-400`` nor ``.text-red-400`` exists on this build, so the
#: footer's whole warning has been rendering in ordinary white. The classes stay:
#: they are what the DOM reads as and what the tests locate this line by.
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
#: The per-label SYMBOL-SHARE total bar and the UNALLOCATED bar. Same track, same
#: fill, same notch, same builder as the label header bar above -- three markers
#: only because a test has to be able to say WHICH bar it is reading.
MARKER_SYMBOL_BAR_ROW = 'pf-symbol-bar-row'
MARKER_SYMBOL_BAR_FILL = 'pf-symbol-bar-fill'
MARKER_SYMBOL_BAR_NOTCH = 'pf-symbol-bar-notch'
#: The reserve card's ALLOCATION bar -- it fills with what is allocated and
#: leaves the reserve as the gap at the right-hand end. Named for the card it
#: lives in rather than for the fill, because the card is what a test looks for.
MARKER_RESERVE_BAR_ROW = 'pf-reserve-bar-row'
MARKER_RESERVE_BAR_FILL = 'pf-reserve-bar-fill'
MARKER_RESERVE_BAR_NOTCH = 'pf-reserve-bar-notch'
#: The whole label header row, so a test can assert on the row rather than on a
#: count of everything that happens to share a style.
MARKER_BAR_ROW = 'pf-bar-row'
#: The label row's "last N%" and unrealised-P&L captions, migrated off the Allocate
#: wizard's step 1. By marker: both are bare percentages/money on a row that
#: already carries four of each, so a text search could not say which cell it
#: found. They are SEPARATE elements rather than one caption because the P&L is the
#: only figure on the line whose sign carries a verdict -- so it is the only one
#: that can be coloured, and NiceGUI colours whole elements.
MARKER_LABEL_LAST = 'pf-label-last'
MARKER_LABEL_PNL = 'pf-label-pnl'
#: The tag icon LEFT OF THE LABEL NAME. Marked because it is one ``ui.icon`` among
#: several on the row and its whole content is an inline colour -- and because the
#: user's complaint was precisely that it disagreed with the bar beside it.
MARKER_LABEL_ICON = 'pf-label-icon'
#: One preset colour chip. Bare divs whose entire content is a background colour.
MARKER_COLOR_SWATCH = 'pf-color-swatch'
#: The "no colour" chip, which is NOT a colour and therefore not one of the above.
MARKER_COLOR_CLEAR = 'pf-color-clear'
#: The custom-colour input.
MARKER_COLOR_CUSTOM = 'pf-color-custom'
#: The summary stat-card row, and the reserve card that no longer sits inside it.
MARKER_SUMMARY_ROW = 'pf-summary-row'
MARKER_RESERVE_CARD = 'pf-reserve-card'
#: The MERGED money card -- managed value, account value and free buying power --
#: and each of its three figures. Marked because the card must be provable to exist
#: whatever the broker answered, and because "$0.00" and "unknown" both appear
#: elsewhere on the page, so a text search cannot say which line it found. Rendered
#: in the order ``summary_figures`` returns, which is the order they are read in.
MARKER_MONEY_CARD = 'pf-money-card'
MARKER_MONEY_FIGURE = 'pf-money-figure'
#: The label-total readout: a FILL BAR inside the "Managed labels" card, under the
#: count, so one card answers "how many labels" and "how much of the pool" at
#: once. It was briefly a card of its own beside that one. The DETAIL is marked
#: separately because its whole signal is its colour, which a text search cannot
#: read.
MARKER_TOTAL_BAR_ROW = 'pf-total-bar-row'
MARKER_TOTAL_BAR_FILL = 'pf-total-bar-fill'
MARKER_TOTAL_BAR_NOTCH = 'pf-total-bar-notch'
MARKER_TOTAL_BAR_DETAIL = 'pf-total-bar-detail'
#: The totals footer under the label list. Marked because the card above it now
#: opens with the same three words -- "Label targets total" -- so a text search
#: finds whichever the renderer drew first, which is the card.
MARKER_ALLOCATION_FOOTER = 'pf-allocation-footer'

# ---- the migrated button groups --------------------------------------------
#
# Located by MARKER and never by caption. "Even split" and "Load last" exist at
# BOTH scopes now -- once over the labels, once per label over its symbols -- so a
# caption match would find whichever the renderer happened to draw first, which is
# precisely the mis-aimed-write bug these markers make testable.

#: The per-label group, in the row that already held Fill 100% and Compare.
#:
#: There is no LABEL-LEVEL group any more. It carried Even split and Load last
#: over every label's target at once; the user asked for both to be per label
#: ("we don't need to have this globally"), so the row is gone. What went with it
#: is stated in ``even_split_label_targets``' docstring, which is kept for it.
MARKER_FILL_100 = 'pf-fill-100'
MARKER_EVEN_SPLIT_SYMBOLS = 'pf-even-split-symbols'
MARKER_FILL_REST_SYMBOLS = 'pf-fill-rest-symbols'
MARKER_LOAD_LAST_SYMBOLS = 'pf-load-last-symbols'
MARKER_LOAD_CURRENT_SYMBOLS = 'pf-load-current-symbols'
MARKER_WIPE_SYMBOLS = 'pf-wipe-symbols'

#: The mini-bar track. Height and radius only -- the FILL's colour comes from the
#: label's own palette entry, which is what makes the bars tell labels apart.
BAR_TRACK_STYLE = ('position:relative;height:10px;border-radius:3px;'
                   'background:rgba(255,255,255,0.08);')
#: The target notch. White and 2px, i.e. deliberately not one of the palette hues:
#: it has to read against every fill colour, including the pale yellow.
BAR_NOTCH_STYLE = ('position:absolute;top:-3px;bottom:-3px;width:2px;'
                   'background:#FFFFFF;opacity:0.85;')

#: One preset colour chip. A circle of the colour ITSELF -- "Make a color picker
#: then": naming a colour is not showing it, and "Bluish green" beside "Vermillion"
#: told the user nothing about what either would look like.
COLOR_SWATCH_STYLE = ('width:20px;height:20px;border-radius:50%;cursor:pointer;'
                      'border:1px solid rgba(255,255,255,0.35);')
#: Quasar debounce (ms) on the custom-colour input. A colour picker emits a model
#: value on every pixel of a drag; without this each one is a SELECT + UPDATE +
#: commit on the event loop.
COLOR_DEBOUNCE_MS = 400


def _render_mini_bar(*, fill_marker: str, notch_marker: str,
                     classes: str = 'flex-grow min-w-[80px]') -> Dict[str, Any]:
    """Draw ONE bar track and hand back its two positioned divs.

    THE bar component. Every bar on this page -- the label header's, the per-label
    symbol-share total, the unallocated row's -- is this function and is painted by
    ``_paint_mini_bar``, so they cannot drift into three visual languages. The two
    divs carry no content at all: the geometry IS the information, which is why
    they are marked rather than found by text.
    """
    with ui.element('div').classes(classes).style(BAR_TRACK_STYLE):
        fill = ui.element('div').mark(fill_marker)
        notch = ui.element('div').mark(notch_marker)
    return {'fill': fill, 'notch': notch}


def _paint_mini_bar(widgets: Dict[str, Any], *, fraction: float,
                    notch_fraction: Optional[float], color: str) -> None:
    """Put one bar's geometry on screen. In place. THE only writer.

    A notch with nowhere to be is HIDDEN rather than parked at zero: a notch at 0%
    is a target of zero, which is a statement, and "there is no target to place"
    is not one.
    """
    widgets['fill'].style(replace=(
        f'position:absolute;left:0;top:0;bottom:0;border-radius:3px;'
        f'width:{fraction * 100.0:.2f}%;background:{color};'))
    if notch_fraction is None:
        widgets['notch'].set_visibility(False)
    else:
        widgets['notch'].set_visibility(True)
        widgets['notch'].style(
            replace=BAR_NOTCH_STYLE + f'left:{notch_fraction * 100.0:.2f}%;')


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
        'symbol_bars': {},      # label -> the symbol-share total bar's elements
        'reserve_bar': None,    # the unallocated row's bar
        'reserve_row': None,    # the "Unallocated (free buying power)" line
        'reserve_caption': None,
        'reserve_number': None,
        'reserve_slider': None,
        'total_bar': None,      # the label-total fill bar's mutable elements
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

    It walks every row, and after the no-recalc change that is not the same thing as
    MOVING every row. The cells are written from ``live['weights'][label]``, and an
    accepted symbol edit now mutates exactly the one key it edited -- so a sibling's
    cell is rewritten with the number it already had. What still legitimately moves
    all of them is a LABEL TARGET or the RESERVE, which re-bases every row's money;
    those two come through here as well, and that is why the loop is over the set
    rather than over the edited symbol.
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
    # The label's own share-of-100 bar, from the SAME map the cells were written
    # from -- so the picture and the column cannot disagree about the total.
    _apply_symbol_bar(live, label)


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
        _paint_mini_bar(widgets, fraction=bar.bar_fraction,
                        notch_fraction=bar.notch_fraction, color=bar.color)
        # The tag icon is retinted HERE, in the same loop and from the same
        # ``bar.color``, rather than at render time from ``view.color``. That is
        # what makes "the label icon left to the title" and the bar incapable of
        # disagreeing: there is one value and one writer, so a recolour cannot
        # reach one of them and miss the other.
        icon = widgets.get('icon')
        if icon is not None:
            # INLINE ``!important``, and it is load-bearing rather than habit.
            # ``styles.css`` carries ``.q-expansion-item .q-icon { color: #a0aec0
            # !important }``; this icon sits in the expansion HEADER, and a
            # stylesheet ``!important`` beats a plain inline style. Without this
            # the icon renders grey beside a correctly coloured bar -- reported
            # twice, and invisible to every Python test because the element
            # carried the right value the whole time.
            icon.style(replace=important_color_style(bar.color))
        widgets['value'].set_text(f'${bar.current_value:,.2f}')
        # Every string below is the PURE layer's, not this module's: the
        # denominator rule, the "(real N%)" parenthetical and the over/under
        # sentence are decisions, and decisions do not live in the renderer.
        widgets['pct'].set_text(bar.current_text)
        widgets['target'].set_text(bar.target_text)
        widgets['delta'].set_text(bar.delta_text)
        # The CLASS stays -- it is what the DOM reads as and what several tests
        # locate this by -- but the inline colour is what paints. "over" is the
        # actionable warning and is the only verdict given a colour.
        widgets['delta'].classes(replace='text-xs ' + LABEL_STATUS_CLASSES[bar.status])
        widgets['delta'].style(replace=important_color_style(
            label_status_color(bar.status)))
        # Neither of these moves on a keystroke -- the previous generation only
        # advances on a run, and the P&L is measured off the positions and quotes
        # this render opened with. They are rewritten in the same loop anyway, so
        # nothing has to remember which of the row's figures are live.
        widgets['last'].set_text(bar.last_text)
        widgets['pnl'].set_text(bar.pnl_text)
        widgets['pnl'].classes(replace=pnl_classes(bar.pnl))
        # Green up, red down, neutral inside the epsilon band -- and painted, not
        # merely classed, for the reason above.
        widgets['pnl'].style(replace=important_color_style(pnl_color(bar.pnl)))
        widgets['tooltip'].set_text(format_label_target_tooltip(
            target_pct=bar.target_pct, base_notional=live['base_notional'],
            unallocated_pct=live['unallocated_pct']))
        for element in live['captions'].get(bar.label, []):
            element.set_text(format_label_header(
                label=bar.label,
                current_value=live['view_by_label'][bar.label].current_value,
                target_pct=bar.target_pct,
                pct_of_investable=bar.current_pct,
                pct_of_total=live['view_by_label'][bar.label].pct_of_total,
                delta_text=bar.delta_text,
                unallocated_pct=live['unallocated_pct']))


def _apply_symbol_bar(live: Dict[str, Any], label: str) -> None:
    """Redraw ONE label's symbol-share total bar. In place.

    Scoped to the label, and that scope is the whole point: this is the sum of the
    shares INSIDE it, against their own 100. Summing across labels, or reading the
    label's share of the portfolio here, would be answering the question the panel
    header above already answers -- on a different denominator.

    Like every other live figure on this page it only REDRAWS. It moves no share
    and it solves nothing.
    """
    widgets = live['symbol_bars'].get(label)
    if widgets is None:
        return
    bar = symbol_total_bar(live['weights'].get(label) or {})
    _paint_mini_bar(widgets, fraction=bar.fraction,
                    notch_fraction=bar.notch_fraction, color=widgets['color'])
    widgets['pct'].set_text(bar.current_text)
    widgets['delta'].set_text(bar.delta_text)
    widgets['delta'].classes(replace='text-xs ' + LABEL_STATUS_CLASSES[bar.status])
    # PAINTED, like the label row's delta and the reserve row's -- and this one was
    # the odd bar out: it wore the status class and nothing painted it, so its
    # verdict rendered in plain white while the two identical readouts either side
    # of it were coloured.
    widgets['delta'].style(replace=important_color_style(
        label_status_color(bar.status)))


def _apply_reserve_row(live: Dict[str, Any]) -> None:
    """The reserve card's money line. In place.

    ``format_reserve_row`` returns ``None`` when there is no base to divide by --
    a brand-new or fully-withdrawn account -- and the line is then left empty
    rather than filled with a share of a number nobody has.
    """
    if live['reserve_row'] is None:
        return
    text = format_reserve_row(base_notional=live['base_notional'],
                              available_buying_power=live['available_buying_power'],
                              unallocated_pct=live['unallocated_pct'])
    live['reserve_row'].set_text(text or '')


def _apply_reserve_bar(live: Dict[str, Any]) -> None:
    """Redraw the unallocated row's bar. In place.

    It fills with what is ALLOCATED and leaves the reserve as the gap at the right
    -- and the target tick is inverted with it, so a 10% reserve target sits at 90%
    along. Both come off ``allocation_bar``, which reads the same
    ``unallocated_row`` the sentence beside it prints, so the picture and the words
    cannot disagree.
    """
    widgets = live['reserve_bar']
    if widgets is None:
        return
    bar = allocation_bar(base_notional=live['base_notional'],
                         available_buying_power=live['available_buying_power'],
                         unallocated_pct=live['unallocated_pct'])
    if bar is None:
        return
    # GREEN inside the band, YELLOW out -- the same ``band_color`` the label-total
    # bar asks, against this bar's own target. "Above target, or more than 20
    # points below it" is one rule at two scopes; two copies would drift, and
    # these two bars sit on one screen where that would be unexplainable.
    _paint_mini_bar(widgets, fraction=bar.fraction,
                    notch_fraction=bar.notch_fraction,
                    color=band_color(bar.current_pct, bar.target_pct))
    widgets['pct'].set_text(bar.current_text)
    widgets['target'].set_text(bar.target_text)
    widgets['delta'].set_text(bar.delta_text)
    widgets['delta'].classes(replace='text-xs ' + LABEL_STATUS_CLASSES[bar.status])
    widgets['delta'].style(replace=important_color_style(
        label_status_color(bar.status)))


def _apply_total_notice(live: Dict[str, Any]) -> None:
    """Refresh the label-total CARD and the totals footer.

    This is the page's own over/under-100 check -- no dry run needed, exactly as in
    the wizard's step 1. Both readouts come from the same targets through the same
    ``judge_label_total``, so they cannot say different things.

    REAL TIME, and only that. It rewrites what is DISPLAYED; it moves no target, it
    solves nothing and it writes nothing. Auto-recalculation was rejected outright
    earlier in this project and a redraw that quietly rebalanced the siblings to
    make the total come out at 100 would be exactly it, wearing a card.
    """
    targets = _label_targets(live)
    widgets = live['total_bar']
    if widgets is not None:
        decided = label_total_readout(targets)
        # The SAME band rule as the reserve bar, against a target of 100: green
        # from 80 to 100 inclusive, yellow outside. ``decided.severity`` is driven
        # by that same predicate, so the bar's colour and the sentence under it
        # cannot disagree.
        _paint_mini_bar(widgets, fraction=decided.bar.fraction,
                        notch_fraction=decided.bar.notch_fraction,
                        color=band_color(decided.bar.current_pct,
                                         decided.bar.target_pct))
        widgets['value'].set_text(decided.text)
        widgets['detail'].set_text(decided.detail)
        widgets['detail'].classes(replace=LABEL_TOTAL_CLASSES[decided.severity])
        widgets['detail'].style(replace=important_color_style(
            LABEL_TOTAL_COLORS[decided.severity]))
    footer = live['footer']
    if footer is not None:
        text, severity = format_allocation_footer(targets, live['unallocated_pct'])
        footer.set_text(text)
        footer.classes(replace=FOOTER_CLASSES[severity])
        # ADDED, not ``replace=``: the footer also carries ``TABULAR_NUMS``, and
        # wiping that would unalign the percentages it exists to line up. Every
        # severity has a colour (including 'ok') precisely because this is
        # rewritten in place -- an omitted declaration would leave the previous
        # severity's orange behind when the total comes back to 100.
        footer.style(important_color_style(LABEL_TOTAL_COLORS[severity]))


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
    _apply_reserve_row(live)
    _apply_reserve_bar(live)
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


def _write_symbol_weight(account_id: int, label: str, symbol: str,
                         weight_pct: float) -> bool:
    """Persist ONE symbol's target weight. Blocking; False when it is STALE.

    Returns False -- a refusal -- when the label is no longer managed. A page
    rendered before the label was unmanaged (in the picker, in another tab, by an
    account switch) still has its table on screen, and ``set_symbol_weight`` creates
    rows unconditionally, so typing in it would leave an orphan allocation row under
    a label the account does not manage. Same guard, same reason, as
    ``_write_label_comment``.

    It NO LONGER READS THE LABEL'S MAP BACK, and that is the whole of the no-recalc
    change. ``get_symbol_weights`` shares whatever is left of 100 among the symbols
    with no stored row, so re-reading it after a write moved every sibling on screen
    -- the user edited one number and the rest of the row rearranged itself. The
    caller now patches the one key it changed and the siblings keep the values they
    are showing. "Fill 100%" is the deliberate way to put the set back to 100.

    ``comment`` is deliberately NOT passed: ``None`` leaves it alone, and a ``''``
    here would wipe the symbol's note on every accepted keystroke.
    """
    if label not in {row.label for row in get_managed_labels(account_id)}:
        return False
    set_symbol_weight(account_id, label, symbol, weight_pct=float(weight_pct))
    return True


async def _save_symbol_weight(account_id: int, live: Dict[str, Any], label: str,
                              symbol: str, raw, label_symbols: List[str]) -> None:
    """One TARGET % cell. Validate, persist, then redraw THIS SYMBOL's figures.

    ``label_symbols`` is kept in the signature although the write no longer needs
    it: the table's event wiring passes it, and it is what the caller would need
    again the moment anything here has to reason about the label's whole set.
    """
    edit = validate_symbol_weight_edit(label=label, symbol=symbol, raw=raw)
    if not edit.accepted:
        ui.notify(edit.message, type='warning')
        _revert_symbol_cell(live, label, symbol)
        return
    try:
        saved = await asyncio.to_thread(_write_symbol_weight, account_id, label,
                                        symbol, edit.value)
    except Exception as e:
        logger.error(f"Saving target for {label}/{symbol} failed: {e}", exc_info=True)
        ui.notify(f'Could not save target: {e}', type='negative')
        _revert_symbol_cell(live, label, symbol)
        return
    if not saved:
        logger.warning(f"Target for '{label}'/{symbol} ignored: the label is no longer "
                       f"managed by account {account_id}")
        ui.notify(f"'{label}' is no longer managed — refresh the page", type='warning')
        _revert_symbol_cell(live, label, symbol)
        return
    # ONE key. The live map is the page's only record of what the row is showing,
    # and it is also what "Fill 100%" reads -- so patching exactly the edited symbol
    # is both "do not change other numbers" and the guarantee that the button sees
    # the set the user is actually looking at.
    live['weights'].setdefault(label, {})[symbol] = float(edit.value)
    _apply_symbol_figures(live, label)


def _write_symbol_weights(account_id: int, label: str, weights) -> bool:
    """Persist a whole label's symbol weights. Blocking; False when it is STALE.

    The "Fill 100%" writer, guarded exactly as ``_write_symbol_weight`` is and for
    the same reason. It writes EVERY symbol in the map rather than only the ones
    whose value changed: a fill's whole promise is that the stored set totals 100,
    and leaving a symbol un-stored would leave it resolving to the even-split default
    again -- which is the recalculation this feature exists to remove.
    """
    if label not in {row.label for row in get_managed_labels(account_id)}:
        return False
    for symbol, pct in (weights or {}).items():
        set_symbol_weight(account_id, label, symbol, weight_pct=float(pct))
    return True


def _symbol_values(live: Dict[str, Any], label: str) -> Dict[str, float]:
    """``{symbol: current value}`` for one label, off the rendered view.

    The SAME figures the table's "Current value" column shows, so "Load current"
    cannot load something the user is not looking at.
    """
    view = live['view_by_label'].get(label)
    if view is None:
        return {}
    return {row.symbol: row.current_value for row in view.rows}


def _unmeasurable_symbols(live: Dict[str, Any], label: str) -> List[str]:
    """The label's members whose value could not be measured at all.

    Read off ``SymbolRow.measurable`` and NOT off ``weight_source``. A saved target
    wins over the default, so a saved row whose price is missing reports
    ``WEIGHT_SOURCE_SAVED`` and would look perfectly measurable -- and "Load
    current" would then restate every share in the label against a total nobody
    knows. Measurability is a fact about the HOLDING, not about where its target
    came from.
    """
    view = live['view_by_label'].get(label)
    if view is None:
        return []
    return [row.symbol for row in view.rows if not row.measurable]


def _previous_symbol_weights(live: Dict[str, Any], label: str):
    """``{symbol: previous_weight_pct}`` for one label -- what "Load last" reads.

    Off the rendered ``LabelView``, which carried it straight from
    ``get_previous_symbol_weights``. That is a SEPARATE read from
    ``get_symbol_weights``: this one leaves an absent row at ``None`` rather than
    filling it with the even-split default, because there is no default for a share
    nobody has ever allocated with.
    """
    view = live['view_by_label'].get(label)
    if view is None:
        return {}
    return {row.symbol: row.previous_weight_pct for row in view.rows}


async def _run_symbol_weights_button(account_id: int, live: Dict[str, Any],
                                     label: str, result, *, what: str) -> None:
    """Persist and draw one decided per-label press. THE handler, for all five.

    ``result`` is a ``WeightsUpdate`` (or the identically shaped ``FillToHundred``)
    from the pure layer, which has already decided what the new set is and what to
    say when there is nothing to do. ``what`` names the action in the log line only.

    The live map is replaced WHOLESALE and ``_apply_symbol_figures`` reads it back,
    so the cells on screen and the rows in the database are written from one value.
    """
    if not result.changed:
        # Said out loud. A button that does nothing when pressed is
        # indistinguishable from a broken one.
        ui.notify(result.message, type='info')
        return
    try:
        saved = await asyncio.to_thread(_write_symbol_weights, account_id, label,
                                        result.weights)
    except Exception as e:
        logger.error(f"{what} for '{label}' failed: {e}", exc_info=True)
        ui.notify(f'Could not save: {e}', type='negative')
        return
    if not saved:
        logger.warning(f"{what} for '{label}' ignored: the label is no longer "
                       f"managed by account {account_id}")
        ui.notify(f"'{label}' is no longer managed — refresh the page", type='warning')
        return
    live['weights'][label] = dict(result.weights)
    _apply_symbol_figures(live, label)
    ui.notify(result.message, type='positive')


async def _fill_label_to_100(account_id: int, live: Dict[str, Any],
                             label: str) -> None:
    """The per-label "Fill 100%" button. Every decision is in the pure layer.

    Reads ``live['weights'][label]`` -- the same map ``_save_symbol_weight`` patches
    one key of -- and never the store. That is what makes the button and the
    no-recalc rule agree about which symbols are empty: they are looking at the same
    numbers the user is.
    """
    await _run_symbol_weights_button(
        account_id, live, label,
        fill_label_to_100(label, live['weights'].get(label) or {}),
        what='Fill 100%')


# ---------------------------------------------------------------------------
# THE PER-LABEL BUTTON GROUP -- migrated off the wizard's step 2
#
# All four read ``live['weights'][label]``, the SAME map the inline edit patches
# one key of, and never the store: a button blind to the edit the user just made
# would contradict the number they are looking at. All four are scoped to ONE
# label -- ``lbl=view.label`` is captured at the call site -- because the shares
# inside a label divide that label's money and nothing else's.
# ---------------------------------------------------------------------------

async def _even_split_symbols(account_id: int, live: Dict[str, Any],
                              label: str) -> None:
    """Give every symbol in ONE label an equal share of that label's 100%."""
    await _run_symbol_weights_button(
        account_id, live, label,
        even_split_symbol_shares(label, live['weights'].get(label) or {}),
        what='Even split')


async def _fill_rest_symbols(account_id: int, live: Dict[str, Any],
                             label: str) -> None:
    """Share what is left of ONE label's 100% across the symbols still at zero.

    NOT ``Fill 100%``: this one never scales, so a weight the user typed survives
    exactly as typed and an over-allocated label is refused rather than trimmed.
    """
    await _run_symbol_weights_button(
        account_id, live, label,
        fill_rest_symbol_shares(label, live['weights'].get(label) or {}),
        what='Fill rest')


async def _load_last_symbols(account_id: int, live: Dict[str, Any],
                             label: str) -> None:
    """Restore ONE label's shares from the last run. The label's target does not move."""
    await _run_symbol_weights_button(
        account_id, live, label,
        load_last_symbol_shares(label, live['weights'].get(label) or {},
                                _previous_symbol_weights(live, label)),
        what='Load last')


async def _load_current_symbols(account_id: int, live: Dict[str, Any],
                                label: str) -> None:
    """Rewrite ONE label's shares to what is held right now.

    The on-demand form of the default, and it differs from it in one way that is
    the whole point of the button: a SAVED target does not win here. It is
    refused outright when nothing can be measured, because writing 0% there would
    be a price outage instructing the plan to sell.
    """
    await _run_symbol_weights_button(
        account_id, live, label,
        load_current_symbol_shares(label, live['weights'].get(label) or {},
                                   _symbol_values(live, label),
                                   unmeasurable=_unmeasurable_symbols(live, label)),
        what='Load current')


async def _wipe_symbols(account_id: int, live: Dict[str, Any], label: str) -> None:
    """Clear ONE label's shares so the user can start it over.

    NO confirmation, deliberately, and the contrast is with ``_confirm_unmanage``:
    that one destroys stored weights AND comments AND the label's target with no
    undo. This writes zeros over one label's weights, leaves the previous
    generation untouched, and "Load last" beside it puts them straight back.
    """
    await _run_symbol_weights_button(
        account_id, live, label,
        wipe_symbol_shares(label, live['weights'].get(label) or {}),
        what='Wipe')


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


async def _save_label_color(account_id: int, label: str, value: str,
                            on_saved=None) -> None:
    """THE colour writer. One of them, for the dialog and for the label row alike.

    ``on_saved`` is handed ``(resolved_hex, stored_value)`` -- never the raw widget
    value. The first is ``resolve_label_icon_color``'s answer, i.e. exactly what the
    next render will draw, so a swatch cannot start lying the moment it is clicked.
    The second is what went into the database (``''`` for no colour), which is the
    only thing that distinguishes "cleared" from "the user chose a grey that happens
    to equal the fallback".

    Three things happen in order and the order matters. The value is PARSED first,
    so a string that is not a colour is refused with a message instead of raising
    inside a worker thread. It is then written. Only then is its CONTRAST reported,
    as a warning rather than a refusal: the user read the palette argument and asked
    for a picker anyway, and it is their UI.
    """
    try:
        normalised = store_color_value(value)
    except ValueError as e:
        # The parse refusal, and the only one. This is the guard that keeps a CSS
        # ``style`` attribute from being handed something that is not a colour.
        ui.notify(str(e), type='warning')
        return
    try:
        saved = await asyncio.to_thread(_write_label_color, account_id, label,
                                        normalised)
    except Exception as e:
        logger.error(f"Saving the colour for label '{label}' failed: {e}", exc_info=True)
        ui.notify(f'Could not save the colour: {e}', type='negative')
        return
    if not saved:
        logger.warning(f"Colour for '{label}' ignored: the label is no longer managed "
                       f"by account {account_id}")
        ui.notify(f"'{label}' is no longer managed — refresh the page", type='warning')
        return
    warning = label_color_contrast_warning(normalised)
    if warning:
        ui.notify(warning, type='warning')
    if on_saved is not None:
        on_saved(resolve_label_icon_color(normalised), normalised)


def _render_color_choices(account_id: int, label: str, current, on_saved, *,
                          inline: bool = False) -> None:
    """The colour chooser: seven preset SWATCHES, a clear, and a custom picker.

    Drawn by BOTH the Manage-labels dialog and the tag icon on the label row, which
    is the point -- "the colour picker already exists in Manage labels, the user
    simply could not find it" was answered by making it reachable from the row, not
    by building a second one. Everything here funnels into ``_save_label_color``.

    SWATCHES, not names. The old control was a ``ui.select`` of "Orange" / "Sky
    blue" / "Bluish green" / "Vermillion", and you cannot see what any of those look
    like -- which was the actual complaint behind "Make a color picker then". The
    seven are still the Okabe & Ito colour-universal-design set; the REASONING for
    that lives in the comments here and on ``LABEL_COLOR_PALETTE``, and no longer
    on screen. It was a five-line paragraph rendered once PER LABEL, so a dozen
    managed labels made the dialog mostly that paragraph -- and the behaviour it
    described (a hard-to-see custom colour is flagged, not refused) is self-evident
    the moment the flag appears.

    ``inline`` lays the swatch row and the Custom field on ONE line instead of
    stacking them under a caption. The dialog uses it so that every label's block
    -- name, swatches, clear, Custom -- sits on one grid; the menu on the label row
    keeps the stacked form, where it hangs under an icon and has no siblings to
    line up with.
    """
    container = (ui.row().classes('items-center gap-3 no-wrap') if inline
                 else ui.column().classes('gap-2 p-2'))
    with container:
        if not inline:
            ui.label('Label colour').classes('text-xs text-secondary-custom')
        with ui.row().classes('items-center gap-2 no-wrap'):
            for name, hex_value in LABEL_COLOR_PALETTE:
                # ``hx=hex_value`` on the handler: without the default-argument
                # capture every chip would save the last colour in the tuple.
                ui.element('div').mark(MARKER_COLOR_SWATCH).style(
                    COLOR_SWATCH_STYLE + f'background:{resolve_label_icon_color(hex_value)};'
                ).tooltip(name).on(
                    'click.stop',
                    lambda _e=None, hx=hex_value: _save_label_color(
                        account_id, label, hx, on_saved))
            # The way BACK. A palette with no exit means a colour, once set, can
            # never be removed -- and "no colour" is a different fact from black.
            ui.icon('format_color_reset').mark(MARKER_COLOR_CLEAR).classes(
                'cursor-pointer text-secondary-custom').tooltip('No colour').on(
                'click.stop',
                lambda _e=None: _save_label_color(account_id, label,
                                                  NO_LABEL_COLOR, on_saved))
        # ``w-44`` and NOT shrinking: the field shows a live ``#RRGGBB`` beside its
        # preview swatch, and a flex row that squeezed it would clip exactly the
        # six characters the control exists to show.
        ui.color_input(
            label='Custom', value=(current or ''), preview=True,
            on_change=lambda e: _save_label_color(account_id, label, e.value, on_saved)
        ).props(f'dense outlined debounce={COLOR_DEBOUNCE_MS}') \
            .classes('w-44 shrink-0').mark(MARKER_COLOR_CUSTOM)


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

    # WIDE ENOUGH FOR ITS CONTENT. At 520px the name column, seven swatches, the
    # clear toggle and the Custom field did not fit on one line, so each label's
    # block wrapped by a different amount depending on how long its name was --
    # which is what made the swatch rows start at different x-offsets and read as
    # broken. ``max-w-[95vw]`` so the wider dialog still fits a laptop.
    name_width = label_column_width_ch(current)
    with ui.dialog() as dialog, \
            ui.card().classes('min-w-[860px] max-w-[95vw]'):
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
            ui.label('The same chooser sits on the tag icon of every label row, so '
                     'it no longer has to be found in here.'
                     ).classes('text-xs text-secondary-custom')
        for label in current:
            with ui.row().classes('w-full items-center gap-3 no-wrap'):
                # ``lbl=label`` on BOTH the swatch and the handler: without the
                # default-argument capture every row would recolour the last label.
                #
                # ONE resolution, TWO consumers: the icon and the tooltip below are
                # written from the same ``resolved``, exactly as the label row's
                # icon and bar are both written from ``LabelBar.color``. Two lookups
                # of one stored value is how a yellow bar ends up beside a grey
                # icon with no way to tell which is right.
                resolved = resolve_label_icon_color(colors.get(label))
                swatch = ui.icon('label').classes('shrink-0') \
                    .style(important_color_style(resolved))
                swatch.tooltip(describe_label_color(colors.get(label)))
                # A FIXED column, so every block below starts at the same x. See
                # ``label_column_width_ch``: floored so two short names still line
                # up, capped so one absurd one cannot push the swatches off screen.
                ui.label(label).classes('truncate shrink-0') \
                    .style(f'width: {name_width}ch')
                # ONE chooser, shared with the label row. ``resolve_label_icon_color``
                # is the only thing that answers "what colour is this label", here
                # and on the row and in the bar.
                _render_color_choices(
                    account_id, label, colors.get(label),
                    lambda hexed, stored, sw=swatch: sw.style(
                        replace=important_color_style(hexed)),
                    inline=True)

        async def _close() -> None:
            dialog.close()
            await refresh()

        with ui.row().classes('w-full justify-end gap-2 mt-4'):
            ui.button('Close', on_click=_close).props('color=primary')
    dialog.open()


# ---------------------------------------------------------------------------
# The symbol info panel (``ui/components/symbol_info_panel.py``)
#
# Two ways in -- the ⓘ on a symbol row, and Compare over a ticked selection --
# and ONE function behind both, so the guards below exist exactly once.
# ---------------------------------------------------------------------------

def _selected_symbols(table) -> List[str]:
    """The symbols TICKED in one label's table, in the order the table reports them.

    The symbol table has carried ``selection='multiple'`` since it was built and
    "Remove selected from label" has always read ``table.selected``; this is that
    same read, named once, so Compare and Remove cannot come to disagree about what
    "selected" means. A second selection mechanism beside a working one is how a
    button ends up acting on rows the user did not tick.

    Order is preserved deliberately: the comparison lays its columns out left to
    right in exactly the order it is handed.
    """
    return [row['symbol'] for row in (table.selected or [])]


def _open_symbol_info(symbols) -> None:
    """Open the symbol-info dialog for ``symbols``. THE entry point, for both callers.

    Neither guard is cosmetic:

    * Every figure the panel draws comes from FMP, so with no key there is nothing
      to show. An empty dialog would read as a failed fetch rather than as
      unfinished configuration, and doing nothing at all would read as a broken
      button -- so it says which setting is missing and where it lives.
    * An empty selection would open a dialog titled "Symbol info — " over an empty
      comparison, which tells the same lie.

    The clock is passed EXPLICITLY. The panel has no clock of its own by design, so
    that nothing can quietly compare two symbols as of two different days.

    Not a coroutine: NiceGUI runs a sync click handler ON the event loop, which is
    the running loop ``open_symbol_info`` needs for the task it starts. The fetch
    itself is the panel's own ``asyncio.to_thread``, so nothing blocks here.
    """
    api_key = get_app_setting('FMP_API_KEY')
    if not api_key:
        ui.notify('FMP API key not configured. Please set FMP_API_KEY in '
                  'Settings > App Settings.', type='warning')
        return
    if not symbols:
        ui.notify('Select at least one symbol first', type='warning')
        return
    open_symbol_info(symbols, api_key=api_key, as_of=date.today())


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
    # is a share of the INVESTABLE POOL, the column below is a share of THIS LABEL.
    # Its first half used to say "of the whole portfolio", which is false whenever a
    # reserve is set -- and the row beside it said so, which is how it was caught.
    ui.label(LABEL_TARGET_CAPTION).classes('text-xs text-secondary-custom')

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
        # The share the LAST run used, and this row's unrealised profit. Both moved
        # off the Allocate wizard's step 2, which was the only screen that could
        # answer either question -- and which no longer holds the weight boxes.
        # ``None``, never 0.0: a symbol that has never been allocated has no last.
        'previous_weight_pct': (None if r.previous_weight_pct is None
                                else round(r.previous_weight_pct, 2)),
        # The ENGINE's wording, formatted here rather than in the browser: every
        # "may this be shown at all" branch -- blank versus 0.00, a percentage
        # versus "no cost basis" -- is a decision, and decisions are not Quasar's.
        'pnl': format_unrealised_pnl(r.pnl),
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
        # The ⓘ, beside the symbol it describes rather than at the far end of
        # eleven columns. ``field`` is required by Quasar and is never printed --
        # the slot below draws a button over it.
        {'name': 'info', 'label': '', 'field': 'symbol', 'align': 'center'},
        {'name': 'current_value', 'label': 'Current value', 'field': 'current_value', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_label', 'label': '% of label', 'field': 'pct_of_label', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_total', 'label': '% of total', 'field': 'pct_of_total', 'sortable': True, 'align': 'right'},
        # "Share of label %", not "Target %": the label header above prints a target
        # too, and that one is a share of the PORTFOLIO. Two different quantities
        # under one word is what made "target 0.0%" over a column of 20s look wrong.
        {'name': 'weight_pct', 'label': 'Share of label %', 'field': 'weight_pct', 'sortable': True, 'align': 'right'},
        # "Last %", immediately after the box it is the history OF. Its denominator
        # is the same one -- a share of THIS label -- so it needs no clause of its
        # own; a blank cell means the symbol has never been allocated.
        {'name': 'previous_weight_pct', 'label': 'Last %', 'field': 'previous_weight_pct', 'sortable': True, 'align': 'right'},
        {'name': 'target_value', 'label': 'Target value', 'field': 'target_value', 'sortable': True, 'align': 'right'},
        {'name': 'quantity', 'label': 'Qty', 'field': 'quantity', 'sortable': True, 'align': 'right'},
        {'name': 'cost_basis', 'label': 'Cost basis', 'field': 'cost_basis', 'sortable': True, 'align': 'right'},
        {'name': 'price', 'label': 'Price', 'field': 'price', 'sortable': True, 'align': 'right'},
        {'name': 'market_value', 'label': 'Market value', 'field': 'market_value', 'sortable': True, 'align': 'right'},
        # Unrealised P&L, money and percent in one pre-formatted string. It is a
        # STRING and not two numeric columns because half its values are not
        # numbers at all -- "-", "- (no price)", "no cost basis" -- and rendering
        # those as 0.00 is the failure mode this platform has actually paid for.
        {'name': 'pnl', 'label': 'P&L', 'field': 'pnl', 'align': 'right'},
        {'name': 'comment', 'label': 'Comment', 'field': 'comment', 'align': 'left'},
    ]

    table = ui.table(columns=columns, rows=rows, row_key='symbol',
                     selection='multiple').classes('w-full dark-pagination')
    live['tables'][view.label] = table
    # WHAT AN UNSET SHARE IS SHOWING. The default used to be the fair share, which
    # would have BOUGHT a newly added symbol; it is the symbol's actual share now,
    # so a symbol the account does not hold sits at 0% and will not be. That is
    # correct and it is surprising, which is exactly the pair that needs a caption.
    ui.label(SHARE_DEFAULT_NOTE).classes('text-xs text-secondary-custom')
    # HOW FAR THE SHARES ABOVE GET TO 100. The same component as the panel header's
    # bar, one denominator down: that one is this label's share of the investable
    # pool, this one is its symbols' shares of the label. The caption on each says
    # which, because two bars two inches apart on two denominators is exactly the
    # collision this page has been unpicking.
    with ui.row().classes('w-full items-center gap-3 no-wrap').style(TABULAR_NUMS) \
            .mark(MARKER_SYMBOL_BAR_ROW):
        ui.label(SYMBOL_TOTAL_BAR_CAPTION).classes('w-40 text-xs text-secondary-custom')
        symbol_bar_widgets = _render_mini_bar(
            fill_marker=MARKER_SYMBOL_BAR_FILL, notch_marker=MARKER_SYMBOL_BAR_NOTCH)
        symbol_bar_widgets['pct'] = ui.label('').classes('w-16 text-right')
        symbol_bar_widgets['delta'] = ui.label('').classes('w-44')
        # The LABEL's own colour, resolved through the one helper the icon and the
        # header bar use, so a recolour reaches all three or none.
        symbol_bar_widgets['color'] = resolve_label_icon_color(view.color)
    live['symbol_bars'][view.label] = symbol_bar_widgets
    _apply_symbol_bar(live, view.label)
    table.add_slot('body-cell-flag', r'''
        <q-td :props="props">
            <span v-if="props.value" :title="'Also in: ' + props.row.labels"
                  style="color:#f6ad55;font-weight:600">{{ props.value }}</span>
        </q-td>
    ''')
    # The row's OWN symbol, read off ``props.row`` in the template and carried by
    # the emit. A NiceGUI cell slot is rendered ONCE and reused by Quasar for every
    # row, so a Python ``ui.button`` per row is not available inside a ``ui.table``
    # -- and the identity has to travel with the click or every row opens the same
    # symbol. That is the same wiring ``weightChange`` and ``commentChange`` below
    # use, and it leaves no loop variable for a handler to close over.
    table.add_slot('body-cell-info', r'''
        <q-td :props="props">
            <q-btn dense flat round size="sm" icon="info" color="grey-5"
                   @click="() => $parent.$emit('symbolInfo', props.row.symbol)">
                <q-tooltip>Holdings, dividends and total return for
                    {{ props.row.symbol }}</q-tooltip>
            </q-btn>
        </q-td>
    ''')
    table.on('symbolInfo', lambda e: _open_symbol_info([e.args[0]]))
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
        symbols = _selected_symbols(table)
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

    # THE GROUP, now holding what it was sized for. Fill 100% was here first and the
    # wizard's step-2 four -- Even split, Fill rest, Load last, Wipe -- have joined
    # it; Compare closes the constructive run.
    #
    # Constructive first, destructive last, with the ``ui.space()`` between them:
    # everything to its left either writes numbers or only reads the ticked rows,
    # and the single button that deletes something sits alone on the right where a
    # mis-aimed click cannot reach it.
    #
    # ``lbl=view.label`` is load-bearing in EVERY lambda. Without the
    # default-argument capture every button on the page would rewrite the LAST
    # label drawn -- the classic NiceGUI closure bug, and here it would silently
    # empty a basket the user was not looking at.
    #
    # None of them is DISABLED when it has nothing to do. The wizard greyed them
    # out; this page reports the no-op in words instead, which is the convention
    # Fill 100% set and which costs no per-keystroke sweep over every row.
    with ui.row().classes('w-full items-center gap-2 mt-2'):
        ui.button('Fill 100%', icon='functions',
                  on_click=lambda lbl=view.label: _fill_label_to_100(
                      account_id, live, lbl)
                  ).props('outline dense').mark(MARKER_FILL_100) \
            .tooltip('Make these shares total 100%: fill the empty ones if there '
                     'are any, otherwise scale them all proportionally.')
        ui.button('Even split', icon='balance',
                  on_click=lambda lbl=view.label: _even_split_symbols(
                      account_id, live, lbl)
                  ).props('outline dense').mark(MARKER_EVEN_SPLIT_SYMBOLS) \
            .tooltip('Give every symbol in this label an equal share of its 100%.')
        ui.button('Fill rest', icon='format_color_fill',
                  on_click=lambda lbl=view.label: _fill_rest_symbols(
                      account_id, live, lbl)
                  ).props('outline dense').mark(MARKER_FILL_REST_SYMBOLS) \
            .tooltip('Share what is left of the 100% between the symbols still at '
                     '0%. Unlike "Fill 100%" this never scales — what you typed '
                     'stays exactly as typed.')
        ui.button('Load last', icon='history',
                  on_click=lambda lbl=view.label: _load_last_symbols(
                      account_id, live, lbl)
                  ).props('outline dense').mark(MARKER_LOAD_LAST_SYMBOLS) \
            .tooltip('Put back the shares the last allocation run used. A symbol '
                     'that has never run keeps the share it has.')
        ui.button('Load current', icon='sync_alt',
                  on_click=lambda lbl=view.label: _load_current_symbols(
                      account_id, live, lbl)
                  ).props('outline dense').mark(MARKER_LOAD_CURRENT_SYMBOLS) \
            .tooltip('Set every share to the symbol\u2019s ACTUAL share of this '
                     'label right now. Unlike the default, this overwrites the '
                     'shares you have already saved.')
        ui.button('Wipe', icon='clear_all',
                  on_click=lambda lbl=view.label: _wipe_symbols(
                      account_id, live, lbl)
                  ).props('outline dense').mark(MARKER_WIPE_SYMBOLS) \
            .tooltip('Clear every share in this label to 0% so it can be redone. '
                     '"Load last" undoes it — the history is not touched.')
        # LEFT of the space, with the harmless buttons. Compare reads the same
        # ticked rows Remove does, but it only READS them; sitting it against the
        # one button on this row that deletes things is how a mis-aimed click stops
        # being harmless.
        ui.button('Compare', icon='compare_arrows',
                  on_click=lambda t=table: _open_symbol_info(_selected_symbols(t))
                  ).props('outline dense') \
            .tooltip('Holdings, dividends and total return for the ticked '
                     'symbols, side by side.')
        ui.space()
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
    with ui.column().classes('stat-card p-3 w-full').mark(MARKER_RESERVE_CARD):
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
        # The consequence, kept NEXT TO THE SLIDER that causes it rather than in a
        # caption: it is the one sentence saying that dragging this up is not free.
        # PAINTED, not merely classed: ``.text-orange-400`` is not in
        # ``styles.css`` and nothing on this build generates it, so the one
        # sentence warning that dragging this slider up SELLS has always been
        # rendering in the same white as the captions around it.
        ui.label(RESERVE_SELL_WARNING).classes('text-xs text-orange-400') \
            .style(class_color_style('text-orange-400'))
        live['reserve_caption'] = ui.label(
            format_reserve_caption(live['base_notional'], live['unallocated_pct'])
        ).classes('text-xs text-secondary-custom').style(TABULAR_NUMS)
        # THE ALLOCATION BAR, folded into the card that already owns the concept --
        # the slider is the TARGET, the row below is the ACTUAL, and the bar is the
        # gap between them. It used to be a separate blue callout under the labels.
        #
        # The card is titled "Unallocated reserve" and this bar fills with
        # ALLOCATED money, which is a contradiction unless the legend says which is
        # which -- so it does.
        live['reserve_row'] = ui.label('').style(TABULAR_NUMS)
        ui.label(ALLOCATION_BAR_LEGEND).classes('text-xs text-secondary-custom')
        with ui.row().classes('w-full items-center gap-3 no-wrap') \
                .style(TABULAR_NUMS).mark(MARKER_RESERVE_BAR_ROW):
            reserve_bar_widgets = _render_mini_bar(
                fill_marker=MARKER_RESERVE_BAR_FILL,
                notch_marker=MARKER_RESERVE_BAR_NOTCH)
            reserve_bar_widgets['pct'] = ui.label('').classes('w-16 text-right')
            reserve_bar_widgets['target'] = ui.label('').classes('w-28 text-right')
            reserve_bar_widgets['delta'] = ui.label('').classes('w-52')
            reserve_bar_widgets['color'] = DEFAULT_LABEL_ICON_COLOR
        live['reserve_bar'] = reserve_bar_widgets
        # The one denominator change on the page, said where the control is.
        ui.label(RESERVE_BASIS_NOTE).classes('text-xs text-secondary-custom')
        _apply_reserve_row(live)
        _apply_reserve_bar(live)


def _recolour_label(live: Dict[str, Any], label: str, stored: str) -> None:
    """A colour was saved from the label row: put it in the registry and redraw.

    It writes ``LabelView.color`` -- the STORED value, so ``''`` becomes the ``None``
    that means "no colour chosen" -- and then lets ``_apply_bars`` do the painting,
    rather than restyling the icon here. That is deliberate: the icon and the bar
    fill are both written from ``LabelBar.color`` inside that one loop, so routing
    through the redraw is what makes it IMPOSSIBLE for the two to end up different.
    That is the exact defect being fixed ("make the label icon left to the title
    same colour as the bar").
    """
    view = live['view_by_label'].get(label)
    if view is None:
        return
    view.color = stored or None
    _apply_bars(live)


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
            # THE tag icon, drawn UNCOLOURED. ``_apply_bars`` paints it from
            # ``bar.color``, in the same loop and from the same value as the fill
            # below -- the two used to be set in different places and the user
            # watched a yellow bar sit beside a grey icon. Tinting it here as well
            # would put the rule in two places, and the second copy would be
            # unfalsifiable: the redraw at the end of ``_render_labels`` overwrites
            # it before anything can read it, so a wrong colour here is invisible.
            # ONE WRITER, and it is the loop.
            #
            # The icon is also the way IN to the colour chooser: clicking it opens
            # the same widget the Manage-labels dialog draws, which is the smallest
            # thing that makes the feature findable.
            # DRAWN UNCOLOURED, and tinted by ``_apply_bars`` from ``bar.color`` --
            # the SAME value the fill below is painted with, in the same loop. The
            # tooltip names which of the three colour states it is in, because "no
            # colour chosen" and "the stored value is not a colour I will render"
            # both resolve to the same neutral grey and only the second is
            # something the user can put right.
            with ui.icon('label').mark(MARKER_LABEL_ICON) \
                    .classes('cursor-pointer') as icon:
                ui.tooltip(describe_label_color(view.color))
                with ui.menu().props('auto-close'):
                    _render_color_choices(
                        account_id, view.label, view.color,
                        lambda hexed, stored, lbl=view.label: _recolour_label(
                        live, lbl, stored))
            widgets['icon'] = icon
            ui.label(view.label).classes('w-48 truncate font-medium')
            widgets['value'] = ui.label('').classes('w-28 text-right')
            # THE bar component, shared with the per-label symbol-share total and
            # the unallocated row so the three read as one visual language.
            widgets.update(_render_mini_bar(fill_marker=MARKER_BAR_FILL,
                                            notch_marker=MARKER_BAR_NOTCH))
            widgets['pct'] = ui.label('').classes('w-16 text-right')
            widgets['target'] = ui.label('').classes('w-36 text-right')
            # THE number that says what to do. It replaced the bare status word --
            # "over" beside a bar already sitting past its notch said nothing the
            # geometry had not -- and it keeps that word's COLOUR, so the row still
            # scans at a glance without printing the same fact three times.
            widgets['delta'] = ui.label('').classes('w-52')
            # The wizard's step-1 caption, minus its denominator clause. "last" is
            # the target the previous RUN used, and it is already on the investable
            # basis (it is a stored target), so unlike the wizard's "% of base"
            # wording it needs no restating -- see ``LAST_TARGET_FMT``.
            widgets['last'] = ui.label('').classes('w-28 text-xs text-secondary-custom') \
                .mark(MARKER_LABEL_LAST)
            widgets['pnl'] = ui.label('').classes('w-52').mark(MARKER_LABEL_PNL)
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
                # ``LABEL_TOOLTIP_STYLE`` is the expert cards' own
                # ``DETAIL_TOOLTIP_STYLE`` plus a legible size: "The info text is too
                # small", and a tooltip is HTML, so without the max-width the
                # sentence renders as one line wider than the viewport.
                widgets['tooltip'] = ui.tooltip('').style(LABEL_TOOLTIP_STYLE)
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
    # NOT sum(v.current_value ...): that counts a symbol once per managed label,
    # while every pct_of_total below was divided by the DISTINCT total.
    total = managed_total_value(views)
    # THE THREE MONEY FIGURES, decided together in the pure module: which snapshot
    # field, what an unknown reads as, whether the leverage clause can be stated,
    # and -- new -- that there are always exactly three of them. The loop below has
    # no branch that can skip one.
    figures = summary_figures(managed_value=total, valuation_mode=mode,
                              account_value=payload['account_value'],
                              available_buying_power=buying_power)
    # TWO cards now, EQUAL HEIGHT, and they WRAP.
    #
    # ``items-stretch`` rather than ``items-start``: the money card carries a
    # leverage caption and the reserve card carried a slider, so the row rendered as
    # a ragged set of different-height boxes rather than as one row of cards.
    # ``flex-wrap`` with ``flex-1 min-w-[11rem]`` on each: five cards no longer fit
    # at this width and the last one was CLIPPED at the edge of the viewport. A card
    # that runs off the screen is worse than a card on the next line.
    #
    # ``justify-between`` inside each card so that, once the boxes are the same
    # height, the figures still sit on the same baseline instead of floating.
    card_classes = 'stat-card p-3 flex-1 min-w-[11rem] justify-between'
    with ui.row().classes('w-full gap-4 items-stretch flex-wrap') \
            .mark(MARKER_SUMMARY_ROW):
        # ONE CARD FOR THE MONEY. Managed value, account value and free buying
        # power were three boxes; each sized itself to its own caption, so the
        # three figures a reader compares left to right sat at three different
        # heights. Worse, the buying-power box was drawn inside an ``if`` and
        # DISAPPEARED when the broker would not answer -- unknown-as-zero in the
        # form the user cannot even notice, because there is nothing on screen to
        # notice. This card is unconditional and so is every line in it.
        with ui.column().classes(card_classes).mark(MARKER_MONEY_CARD):
            for figure in figures:
                # EACH FIGURE IN ITS OWN TIGHT GROUP. The card carries
                # ``justify-between`` (that is what keeps the row of cards on one
                # baseline), and without this the three captions would be pushed
                # away from the three figures they name -- a caption belongs to the
                # number under it, not to the whitespace.
                with ui.column().classes('gap-0'):
                    ui.label(figure.title).classes('text-xs text-secondary-custom')
                    # UNKNOWN is drawn in the same neutral grey the captions use
                    # rather than in the money's white: "no answer" is not a
                    # number, and setting it in the numeral style invites the eye
                    # to read it as one. Painted with ``important_color_style``
                    # because ``styles.css`` has eaten plain inline colour on this
                    # page before -- three separate "fixes" looked right in Python
                    # and never reached the screen.
                    value = ui.label(figure.text).classes('text-lg font-bold') \
                        .style(TABULAR_NUMS).mark(MARKER_MONEY_FIGURE)
                    if not figure.available:
                        value.style(add=important_color_style(NEUTRAL_TEXT_COLOR))
                    if figure.detail:
                        ui.label(figure.detail).classes(
                            'text-xs text-secondary-custom')
        # ONE CARD, BOTH HALVES OF ONE QUESTION: how many labels, and how much of
        # the investable pool they add up to. The running total was a conditional
        # sentence under this row -- present only when the set was WRONG, so
        # missing at exactly the moment the user was typing towards 100 -- and then
        # briefly a card of its own. It is a fill bar under the count now, on the
        # SAME ``build_share_bar`` and the same track as the per-label symbol-share
        # bar and the unallocated bar: three scopes of one idea.
        with ui.column().classes(card_classes) as labels_card:
            ui.label('Managed labels').classes('text-xs text-secondary-custom')
            ui.label(str(len(views))).classes('text-lg font-bold')
            # WHAT THE BAR IS. The card is titled "Managed labels" and shows a
            # COUNT, so a bar under it with no name is a rectangle to be guessed
            # at -- the same objection the reserve card's legend answers.
            ui.label(LABEL_TOTAL_BAR_LEGEND).classes('text-xs text-secondary-custom')
            with ui.row().classes('w-full items-center gap-2 no-wrap') \
                    .style(TABULAR_NUMS).mark(MARKER_TOTAL_BAR_ROW):
                total_widgets = _render_mini_bar(
                    fill_marker=MARKER_TOTAL_BAR_FILL,
                    notch_marker=MARKER_TOTAL_BAR_NOTCH)
                total_widgets['value'] = ui.label('').classes('text-xs text-right')
            # The denominator, said out loud: this page carries three of these bars
            # and they divide three different things.
            ui.label(LABEL_TOTAL_BAR_CAPTION).classes('text-xs text-secondary-custom')
            total_widgets['detail'] = ui.label('').mark(MARKER_TOTAL_BAR_DETAIL)
            total_widgets['color'] = DEFAULT_LABEL_ICON_COLOR
            # At EVERY state, because the "use the Unallocated box" guidance rides
            # inside the SHORTFALL sentence and vanishes the moment the set is
            # right -- which is exactly when a user decides to leave a gap.
            labels_card.tooltip(LABEL_TOTAL_TOOLTIP)
        live['total_bar'] = total_widgets

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
    # Its target is the STORED reserve, edited in the card directly above it and
    # validated there. Omitted entirely when there is no base: better a missing row
    # than one measured against a guess.
    #
    # ``not base_notional``, NOT ``base_notional is None``. A base of exactly 0.0
    # is a real state -- a brand-new or fully-withdrawn account -- and it is not a
    # denominator, so ``unallocated_row`` reports ``pct_of_base=None`` for it just
    # as ``build_label_views`` treats a 0 base as absent. The old ``is not None``
    # guard let that one value through and the ``:.1f`` below raised on the None,
    # 500-ing the entire page. ``buying_power`` keeps ``is not None``: 0.00 of free
    # buying power is a fact worth drawing, and only None means "unknown".
    # THE RESERVE, on its own line and NOT in the card row above.
    #
    # It was the fifth card in the summary row and it did not belong: it is the
    # only CONTROL among four read-only stats, it is the widest thing there (a
    # slider, an input, a live dollar caption and now a bar), and it was the one
    # being clipped. Here it sits directly above the list it governs -- the labels
    # divide what it leaves -- which is also where a reader looks when they wonder
    # why the targets moved.
    #
    # It carries the WHOLE reserve story now: the slider is the target, the money
    # line is the actual, the bar is the gap, and the two notes are the
    # denominator and the consequence. There is no separate blue callout under the
    # labels any anymore -- one widget owning one concept.
    _render_reserve_card(account_id, live)

    for view in views:
        # Registered by ``_render_label_body`` inside the row, not here: two calls
        # to ``_register_view`` for one view made its idempotence guard the only
        # thing standing between the page and a doubled bar list, and a guard whose
        # removal changes nothing observable is a guard no test can hold.
        _render_label_bar_row(account_id, live, view, refresh)

    with ui.row().classes('w-full items-center gap-3'):
        live['footer'] = ui.label('').style(TABULAR_NUMS) \
            .mark(MARKER_ALLOCATION_FOOTER)
        ui.label('███ current    ╎ target notch').classes('text-xs text-secondary-custom')
    # The denominators, named ONCE for the whole page. The rows are terse
    # ("tgt 15.0% (real 13.5%)") precisely because this line exists; spelling them
    # out per row is what made the old header a hundred characters long.
    ui.label(BASIS_LEGEND).classes('text-xs text-secondary-custom')

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
    """The Review-and-Submit button: the dry run, then Submit. NO target step any more.

    A REBALANCE goes STRAIGHT to the dry run. The three-step dialog it used to open
    first was the target editor, and the targets are typed on this page now -- a
    second place to type them is a second answer to "what am I aiming at".
    ``_load_flow_inputs`` re-reads the stored rows at this moment, so what the dry
    run solves against is exactly what the page last persisted.

    An INVEST run still opens ``open_invest_scope`` first, and that is not a target
    editor: it spends a specific amount on a single label, so the run has to be told
    which label and how much, and neither is a stored target of anything.

    Every blocking call is dispatched through ``asyncio.to_thread``: broker IO on the
    event loop freezes the app for every connected client.
    """
    try:
        base, labels, allow_fractional, unallocated_pct = await asyncio.to_thread(
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

        All three writes happen when the DRY RUN is opened, not on Submit, and that
        is what "last" means here: the numbers the user last chose to allocate with,
        whether or not they went through with the orders. The fractional switch has
        been persisted from exactly this point since it shipped
        (``remember_fractional_choice``); the targets and the reserve follow it.

        On a REBALANCE the page has already persisted every one of these inline, so
        this is normally a re-write of what is already stored -- and
        ``save_allocation_targets`` shifts the previous generation only on a CHANGE,
        so a run that changes nothing does not consume a generation. What it still
        earns is the symbol rows: a symbol silently taking the even-split default
        gets an explicit one, because the user has just allocated real money with
        that number.

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
        """Called by the invest-scope dialog (sync). Records the choices, then solves.

        The write itself is in ``_run_dry_run``, off the event loop -- unlike the
        fractional switch this is N labels and M symbols, and the page's own rule
        is that blocking work goes through ``asyncio.to_thread``.
        """
        state.update({'mode': mode, 'labels': labels, 'scope_label': scope_label,
                      'amount': float(amount or 0.0),
                      'allow_fractional': bool(allow_fractional),
                      'unallocated_pct': float(unallocated_pct or 0.0)})
        ui.timer(0.1, _run_dry_run, once=True)

    if mode == ALLOCATION_MODE_INVEST_LABEL:
        open_invest_scope(base, labels, on_dry_run=_on_dry_run,
                          allow_fractional=allow_fractional,
                          invest_amount=state['amount'])
        return
    # A REBALANCE opens NO dialog first. The targets, the shares and the reserve
    # were all typed on this page and are already stored; ``_load_flow_inputs``
    # read them back a few lines above, so there is nothing left to ask.
    await _run_dry_run()


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
                        .classes('text-xs text-orange-400') \
                        .style(class_color_style('text-orange-400'))
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
            ui.button(REVIEW_BUTTON_LABEL, icon='fact_check',
                      on_click=lambda: _open_allocation_flow(
                          account_id, mode_state['value'], _refresh)) \
                .props('color=primary') \
                .tooltip('Solve the plan against the broker and show it for review. '
                         'Nothing is ordered until you press Submit in the dry run.')
            ui.select({VALUATION_MODE_COST: 'Cost basis',
                       VALUATION_MODE_MARKET: 'Market value'},
                      value=mode_state['value'], label='Valuation',
                      on_change=_set_mode).props('dense outlined').classes('w-44')
            ui.button('Manage labels', icon='pie_chart',
                      on_click=lambda: _open_label_picker(account_id, _refresh)).props('outline')
            ui.button('Refresh', icon='refresh', on_click=_refresh).props('outline')

        await _refresh()
