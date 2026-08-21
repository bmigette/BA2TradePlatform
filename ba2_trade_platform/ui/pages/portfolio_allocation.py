"""Portfolio Allocation page — manually traded accounts only.

Shows the account's current allocation, grouped by the instrument labels the user
chose to manage, and lets those labels, their symbols and their comments be
edited. Every decision this page makes lives in the pure, unit-tested module
``ba2_trade_platform/ui/utils/portfolio_allocation_view.py``; this file only does
IO (broker + DB) and draws widgets.

Everything persists ON CHANGE, not behind a Save button: switching the global
account calls ``ui.run_javascript('window.location.reload()')``
(``ui/layout.py``), so the page never gets a chance to flush a pending edit.
Symbols can be added to or removed from a label whether or not they have an open
position (but they do have to exist at the broker — ``symbols_exist``, or a typo
becomes a permanent global ``Instrument`` row), and a symbol carrying two managed
labels is allowed — it just gets a warning icon.

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
    compute_label_investment, unconsumed_income_notice,
)
from ...core.portfolio_allocation_store import (
    add_symbols_to_label, get_allocation_config, get_managed_labels, get_symbol_comments,
    get_symbol_rows, get_symbol_weights, remove_symbols_from_label, replace_managed_labels,
    set_allocation_config, set_managed_label, set_symbol_weight,
)
from ...logger import logger
from ..account_filter_context import get_selected_account_id
from ..utils.portfolio_allocation_view import (
    DEFAULT_MACHINE_LABEL_FAMILIES, GATE_NO_ACCOUNT, MARKET_SOURCE_UNAVAILABLE,
    GateResult, ManagedLabel,
    PositionFetchFailed, build_label_views, collect_managed_symbols, diff_managed_labels,
    evaluate_gate, evaluate_market_gate, expert_shortname_families, managed_total_value,
    missing_quote_symbols, picker_options, positions_by_symbol, working_orders_notice,
)
from .portfolio_allocation_wizard import (
    open_allocation_steps, open_allocation_wizard, render_income_panel, render_outcomes,
)

#: Quasar debounce (ms) for the comment inputs. Every keystroke used to run a
#: SELECT + UPDATE + commit + refresh on the NiceGUI event loop; the page's own
#: convention is that blocking work goes through ``asyncio.to_thread``, and this
#: keeps the number of those round trips down to one per pause in typing.
COMMENT_DEBOUNCE_MS = 600


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

    Raises:
        PositionFetchFailed: the broker position fetch failed (NOT a flat account).
        RuntimeError: the account could not be instantiated.
    """
    from ...core.utils import get_account_instance_from_id, get_symbols_by_label

    managed = [ManagedLabel(label=row.label, target_pct=row.target_pct, comment=row.comment)
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

    comments: Dict[tuple, str] = {}
    for entry in managed:
        for symbol, text in get_symbol_comments(account_id, entry.label).items():
            comments[(entry.label, symbol)] = text

    return {
        'views': build_label_views(managed, symbols_by_label, positions, prices, comments,
                                   valuation_mode=valuation_mode),
        'symbols_by_label': symbols_by_label,
        'valuation_mode': valuation_mode,
    }


def _load_valuation_mode(account_id: int) -> str:
    """The account's stored valuation mode, creating the config row on first use."""
    return get_allocation_config(account_id).valuation_mode


def _load_flow_inputs(account_id: int, valuation_mode: str):
    """Everything the wizard needs to OPEN, in one thread hop. Blocking.

    Returns:
        Tuple: ``(base, labels, allow_fractional)`` -- the frozen base snapshot, the
        managed labels with their symbol weights, and the account's remembered
        fractional choice.

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
        labels.append(LabelTarget(
            label=row.label, target_pct=float(row.target_pct or 0.0),
            symbols=[SymbolTarget(symbol=s, weight_pct=float(weights.get(s, 0.0)))
                     for s in members],
            comment=row.comment))

    current = svc.build_position_states(account, symbols)
    base = build_base_snapshot(account.get_account_snapshot(), current, symbols,
                               valuation_mode=valuation_mode)
    return base, labels, bool(get_allocation_config(account_id).allow_fractional)


def _solve_plan(account_id: int, *, mode: str, labels, scope_label, amount: float,
                allow_fractional: bool, valuation_mode: str):
    """Solve one dry run against FRESH positions, prices and margin info. Blocking.

    Re-reads everything rather than reusing the open dialog's snapshot: Refresh
    exists precisely because the numbers move, and a plan solved against a stale
    price is a plan submitted at the wrong size.

    Returns:
        Tuple: ``(base, plan, current, hours)``. ``hours`` is the broker's
        ``MarketHours`` or ``None``; ONE read feeds both the banner and the gate.
    """
    from ...core.utils import get_account_instance_from_id

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")
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
            default_bp_factor=base.default_bp_factor, valuation_mode=valuation_mode)
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

    return {
        'current': [row.label for row in get_managed_labels(account_id)],
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


def _render_label_body(account_id: int, view, refresh) -> None:
    """One managed label's comment box, symbol table and add/remove controls."""
    label_symbols = [r.symbol for r in view.rows]

    with ui.row().classes('w-full items-center gap-2'):
        ui.input('Label comment', value=view.comment or '',
                 on_change=lambda e, lbl=view.label: _save_label_comment(account_id, lbl, e.value)
                 ).props(f'dense outlined debounce={COMMENT_DEBOUNCE_MS}').classes('flex-grow')
        ui.button('Add symbol', icon='add',
                  on_click=lambda lbl=view.label: _open_add_symbol_dialog(account_id, lbl, refresh)
                  ).props('outline dense')

    rows = [{
        'flag': '⚠' if r.multi_label else '',
        'symbol': r.symbol,
        'labels': ', '.join(r.labels),
        'current_value': round(r.current_value, 2),
        'pct_of_label': round(r.pct_of_label, 2),
        'pct_of_total': round(r.pct_of_total, 2),
        'quantity': round(r.quantity, 4),
        'cost_basis': round(r.cost_basis, 2),
        'price': None if r.price is None else round(r.price, 4),
        'market_value': None if r.market_value is None else round(r.market_value, 2),
        'comment': r.comment or '',
    } for r in view.rows]

    columns = [
        {'name': 'flag', 'label': '', 'field': 'flag', 'align': 'center'},
        {'name': 'symbol', 'label': 'Symbol', 'field': 'symbol', 'sortable': True, 'align': 'left'},
        {'name': 'labels', 'label': 'Labels', 'field': 'labels', 'align': 'left'},
        {'name': 'current_value', 'label': 'Current value', 'field': 'current_value', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_label', 'label': '% of label', 'field': 'pct_of_label', 'sortable': True, 'align': 'right'},
        {'name': 'pct_of_total', 'label': '% of total', 'field': 'pct_of_total', 'sortable': True, 'align': 'right'},
        {'name': 'quantity', 'label': 'Qty', 'field': 'quantity', 'sortable': True, 'align': 'right'},
        {'name': 'cost_basis', 'label': 'Cost basis', 'field': 'cost_basis', 'sortable': True, 'align': 'right'},
        {'name': 'price', 'label': 'Price', 'field': 'price', 'sortable': True, 'align': 'right'},
        {'name': 'market_value', 'label': 'Market value', 'field': 'market_value', 'sortable': True, 'align': 'right'},
        {'name': 'comment', 'label': 'Comment', 'field': 'comment', 'align': 'left'},
    ]

    table = ui.table(columns=columns, rows=rows, row_key='symbol',
                     selection='multiple').classes('w-full dark-pagination')
    table.add_slot('body-cell-flag', r'''
        <q-td :props="props">
            <span v-if="props.value" :title="'Also in: ' + props.row.labels"
                  style="color:#f6ad55;font-weight:600">{{ props.value }}</span>
        </q-td>
    ''')
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


def _render_labels(account_id: int, payload: Dict[str, Any], refresh) -> None:
    views = payload['views']
    if not views:
        with ui.element('div').classes('alert-banner info w-full p-3'):
            ui.label('No labels are managed for this account yet — click "Manage labels".')
        return

    mode = payload['valuation_mode']
    mode_label = ('cost basis (what you paid)' if mode == VALUATION_MODE_COST
                  else 'market value (qty x price)')
    # NOT sum(v.current_value ...): that counts a symbol once per managed label,
    # while every pct_of_total below was divided by the DISTINCT total.
    total = managed_total_value(views)
    with ui.row().classes('w-full gap-4'):
        with ui.column().classes('stat-card p-3'):
            ui.label(f'Managed value — {mode_label}').classes('text-xs text-secondary-custom')
            ui.label(f'${total:,.2f}').classes('text-lg font-bold')
        with ui.column().classes('stat-card p-3'):
            ui.label('Managed labels').classes('text-xs text-secondary-custom')
            ui.label(str(len(views))).classes('text-lg font-bold')

    unpriced = missing_quote_symbols(views)
    if unpriced and mode == VALUATION_MODE_MARKET:
        with ui.element('div').classes('alert-banner warning w-full p-3'):
            ui.label(f"No quote for {len(unpriced)} held symbol(s): "
                     f"{', '.join(unpriced)}")
            ui.label('They are valued at $0.00 in market mode, which is NOT the same '
                     'as being flat — switch to cost basis, or retry the quote.'
                     ).classes('text-xs text-secondary-custom')

    ui.label('Prices are the broker feed (Alpaca defaults to delayed_sip — 15 minutes '
             'delayed). Only symbols carrying a managed label are listed. Short '
             'positions carry a negative quantity, cost basis and value.'
             ).classes('text-xs text-secondary-custom')

    for view in views:
        header = (f'{view.label} — ${view.current_value:,.2f} '
                  f'({view.pct_of_total:.1f}% of managed, target {view.target_pct:.1f}%)')
        with ui.expansion(header, icon='label').classes('w-full'):
            _render_label_body(account_id, view, refresh)


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
        base, labels, allow_fractional = await asyncio.to_thread(
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
             'base': base, 'current': {}}

    async def _run_dry_run() -> None:
        try:
            new_base, plan, current, hours = await asyncio.to_thread(
                _solve_plan, account_id, mode=state['mode'], labels=state['labels'],
                scope_label=state['scope_label'], amount=state['amount'],
                allow_fractional=state['allow_fractional'],
                valuation_mode=valuation_mode)
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
            """Called from the wizard (sync) -- re-solve and hand back the new plan."""
            state['allow_fractional'] = bool(allow_fractional)
            svc.remember_fractional_choice(account_id, bool(allow_fractional))
            fresh_base, fresh_plan, fresh_current, _ = _solve_plan(
                account_id, mode=state['mode'], labels=state['labels'],
                scope_label=state['scope_label'], amount=state['amount'],
                allow_fractional=bool(allow_fractional), valuation_mode=valuation_mode)
            state['base'] = fresh_base
            state['current'] = fresh_current
            return fresh_plan

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
                                     working_order_ids=result['working_order_ids'])
        if note is not None:
            ui.notify(note[0], type=note[1])
        await refresh()

    def _on_dry_run(*, mode: str, labels, scope_label, amount: float,
                    allow_fractional: bool) -> None:
        """Called by the steps dialog (sync). Persist the choice, then solve."""
        state.update({'mode': mode, 'labels': labels, 'scope_label': scope_label,
                      'amount': float(amount or 0.0),
                      'allow_fractional': bool(allow_fractional)})
        # Persisted on every dry run, not only on submit: a user who plans, closes
        # the dialog and comes back tomorrow keeps the switch they chose.
        svc.remember_fractional_choice(account_id, bool(allow_fractional))
        ui.timer(0.1, _run_dry_run, once=True)

    open_allocation_steps(base, labels, on_dry_run=_on_dry_run,
                          allow_fractional=allow_fractional,
                          mode=state['mode'], invest_amount=state['amount'])


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
