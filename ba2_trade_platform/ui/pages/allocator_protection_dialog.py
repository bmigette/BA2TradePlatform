"""The "Set TP/SL" dialog of the Portfolio Allocation page (TastyTrade accounts).

One stop-loss PRICE, one or more take-profit targets (price + share of the position), live
validation, a preview of the OCO orders that would be placed, and Save / Switch off / Re-place /
Re-enable. Everything that DECIDES is in ``core/allocator_protection`` (pure) and every broker or
DB action is ``core/allocator_protection_service``; this module only draws and wires.

Real orders are placed by Save, Re-place and the allocator run: every one of those paths runs in
``asyncio.to_thread`` behind a busy latch (a double click must not place a second set), and every
refusal is shown in the dialog, never only in a toast.

CSS lives in ``portfolio_allocation.page_phone_css()`` (installed before the page's first await);
this module adds none.
"""
import asyncio
from typing import Any, Dict, List, Optional

from nicegui import ui

from ...core import allocator_protection_service as aps
from ...core.allocator_protection import (
    PRESETS, STATUS_OFF, ProtectionStatus, TpTarget, apply_preset, effective_targets, preview_orders,
    runner_fraction, validate_protection, whole_shares, fractional_remainder,
)
from ...core.allocator_protection_models import AllocatorProtection, AllocatorProtectionOrder
from ...logger import logger
from ..utils.allocator_protection_view import (
    STATUS_HEX, even_percentages, fractions_to_percentages, parse_target_rows,
)

DIALOG_CLASS = 'pf-prot-dialog'
TP_ROW_CLASS = 'pf-tp-row'
MARKER_SAVE = 'pf-prot-save'
MARKER_SWITCH_OFF = 'pf-prot-off'
MARKER_FORGET = 'pf-prot-forget'
MARKER_REARM = 'pf-prot-rearm'
MARKER_CHECK = 'pf-prot-check'
MARKER_USE_ACCEPTED = 'pf-prot-accepted'
MARKER_USE_SUGGESTED = 'pf-prot-suggested'
MARKER_REPLACE = 'pf-prot-replace'
MARKER_ADD_TARGET = 'pf-prot-add'
MARKER_PRESET_PREFIX = 'pf-prot-preset-'


def load_dialog_data(account_id: int, symbol: str) -> Dict[str, Any]:
    """Everything the dialog needs, in ONE thread hop. Blocking (broker reads).

    Raises ``BrokerReadError`` (a position or price that could not be read: the dialog must not
    open on a guess) or whatever the account lookup raises.
    """
    from ...core.utils import get_account_instance_from_id

    account = get_account_instance_from_id(account_id)
    if account is None:
        raise RuntimeError(f"Account {account_id} could not be instantiated")
    if getattr(account, 'supports_allocator_protection', False) is not True:
        raise RuntimeError("This account type does not support TP/SL protection")
    symbol = symbol.strip().upper()
    quantity, is_long = aps.read_position(account, symbol)
    price = aps.read_price(account, symbol)
    average_cost = aps.read_average_cost(account, symbol)
    ticks = aps.read_tick_sizes(account, symbol)
    protection = aps.get_protection(account_id, symbol)
    slices = aps.get_slices(protection.id) if protection else []
    return {'account': account, 'symbol': symbol, 'quantity': quantity, 'is_long': is_long,
            'price': price, 'average_cost': average_cost, 'ticks': ticks,
            'protection': protection, 'slices': slices,
            'status': aps.status_for(protection, quantity, slices)}


def initial_rows(protection: Optional[AllocatorProtection]) -> List[Dict[str, Any]]:
    """The TP rows the dialog opens with: the stored ones (possibly none: a stop-only
    protection), else one blank row at 100%."""
    if protection is not None and (protection.enabled or protection.tp_targets):
        # The targets still to be taken, at their ORIGINAL shares; the ones already taken are listed apart
        # (``initial_kept``) with a Re-arm control.
        items = [t for t in (protection.tp_targets or []) if not t.get('filled')]
        pcts = fractions_to_percentages([float(t['fraction']) for t in items])
        rows = []
        for t, pct in zip(items, pcts):
            row = {'price': t['price'], 'pct': pct}
            if t.get('taken'):
                row['taken'] = float(t['taken'])                # a partly taken target stays partly taken
                row['orig'] = (t['price'], pct)
            rows.append(row)
        return rows
    return [{'price': None, 'pct': 100.0}]


def initial_kept(protection: Optional[AllocatorProtection]) -> List[Dict[str, Any]]:
    """The targets that already FILLED (shown greyed, each with a Re-arm checkbox)."""
    if protection is None:
        return []
    items = [t for t in (protection.tp_targets or []) if t.get('filled')]
    return [{'price': t['price'], 'pct': round(float(t['fraction']) * 100.0, 4)} for t in items]


def slice_rows(slices: List[AllocatorProtectionOrder]) -> List[Dict[str, Any]]:
    """Read-only rows for the 'orders at the broker' table."""
    rows = []
    for s in slices:
        if s.closed_at is not None and s.state in ('CANCELLED_BY_US',):
            continue
        rows.append({'n': s.slice_index + 1, 'qty': s.quantity,
                     'tp': s.tp_price if s.tp_price is not None else '(stop only)', 'sl': s.sl_price,
                     'state': s.state, 'order': s.complex_order_id or '', 'stop': s.sl_order_id or '',
                     'tag': s.external_tag or '',
                     'gtc': (f"{s.gtc_date:%Y-%m-%d}" + ('*' if s.gtc_date_assumed else ''))
                     if s.gtc_date else ''})
    return rows


async def open_protection_dialog(account_id: int, symbol: str, refresh) -> None:
    """Open the dialog for ``symbol``. ``refresh`` redraws the page once something changed."""
    try:
        data = await asyncio.to_thread(load_dialog_data, account_id, symbol)
    except Exception as e:  # noqa: BLE001 -- shown, not swallowed
        logger.error(f"TP/SL dialog for {symbol} could not load: {e}", exc_info=True)
        ui.notify(f"Cannot open TP/SL for {symbol}: {e}", type='negative')
        return
    _build_dialog(account_id, data, refresh)


def _build_dialog(account_id: int, data: Dict[str, Any], refresh) -> None:
    account = data['account']
    symbol = data['symbol']
    price = data['price']
    average_cost = data.get('average_cost')
    quantity = data['quantity']
    ticks = data['ticks']
    protection: Optional[AllocatorProtection] = data['protection']
    status: ProtectionStatus = data['status']
    shares = whole_shares(quantity)
    rows_state: List[Dict[str, Any]] = initial_rows(protection)
    kept_state: List[Dict[str, Any]] = initial_kept(protection)
    changed = {'v': False}
    busy = {'v': False}

    with ui.dialog() as dialog, ui.card().classes(f'w-full max-w-[680px] {DIALOG_CLASS}'):
        ui.label(f'TP/SL protection: {symbol}').classes('text-h6')
        with ui.row().classes('w-full items-center gap-4 pf-wrap-row'):
            ui.label(f'Price {price:,.4f}').classes('text-body2')
            ui.label(f'Held {quantity:g} sh').classes('text-body2')
            ui.label(f'Protectable {shares} whole sh').classes('text-body2')
        if fractional_remainder(quantity) > 1e-9:
            ui.label(f'{fractional_remainder(quantity):g} fractional share(s) cannot carry a limit '
                     f'or stop on TastyTrade and stay UNPROTECTED.'
                     ).classes('text-xs').style('color:#fbbf24')
        if not data['is_long'] and quantity > 0:
            ui.label('This is a SHORT position: TP/SL protection supports long equity only.'
                     ).classes('text-body2').style(f'color:{STATUS_HEX["negative"]}')

        ui.label(f'Status: {status.label}').classes('text-body2 text-weight-bold').style(
            f'color:{STATUS_HEX.get(status.color, STATUS_HEX["grey"])}')
        if status.tooltip and status.code != STATUS_OFF:
            ui.label(status.tooltip).classes('text-xs text-secondary-custom')
        if protection is not None and protection.alert_message:
            ui.label(f'ALERT {protection.alert_code}: {protection.alert_message}'
                     ).classes('text-body2').style(f'color:{STATUS_HEX["negative"]}')
        if protection is not None and protection.last_error and not protection.alert_message:
            ui.label(f'Last refusal: {protection.last_error}').classes('text-xs').style(
                f'color:{STATUS_HEX["negative"]}')

        if protection is not None and protection.last_fill_note:
            ui.label(f'Last fill: {protection.last_fill_note}').classes('text-xs').style(
                f'color:{STATUS_HEX["info"]}')
        ui.label('The allocator rebalances a protected symbol normally: it cancels these orders '
                 '(waiting for the broker to confirm), trades, and re-places them at the new '
                 'quantity with the same stop and the same target prices and shares. Excluding the '
                 'symbol from allocation does not cancel or change them.'
                 ).classes('text-xs text-secondary-custom')
        if data['slices']:
            ui.label('Orders at the broker').classes('text-caption text-secondary-custom')
            ui.table(columns=[
                {'name': 'n', 'label': '#', 'field': 'n', 'align': 'left'},
                {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'align': 'right'},
                {'name': 'tp', 'label': 'Take-profit', 'field': 'tp', 'align': 'right'},
                {'name': 'sl', 'label': 'Stop', 'field': 'sl', 'align': 'right'},
                {'name': 'state', 'label': 'State', 'field': 'state', 'align': 'left'},
                {'name': 'order', 'label': 'OCO order', 'field': 'order', 'align': 'right'},
                {'name': 'stop', 'label': 'Stop order', 'field': 'stop', 'align': 'right'},
                {'name': 'tag', 'label': 'Tag (find it on the TT site)', 'field': 'tag', 'align': 'left'},
                {'name': 'gtc', 'label': 'GTC until', 'field': 'gtc', 'align': 'right'},
            ], rows=slice_rows(data['slices']), row_key='n').props('dense flat').classes('w-full')

        # ------------------------------------------------------------ inputs
        sl_input = ui.number('Stop-loss price', value=(protection.sl_price if protection and protection.sl_price else None),
                             min=0, step=0.01, format='%.4f').props('dense outlined').classes('w-full')
        ui.label('One stop price for the whole position. A plain stop (market order once '
                 'triggered), split across the take-profit orders.'
                 ).classes('text-xs text-secondary-custom')
        ui.label('Presets (computed from your average cost'
                 + (f' {average_cost:,.4f}' if average_cost else ': UNKNOWN, presets unavailable')
                 + '; every number stays editable)').classes('text-caption text-secondary-custom')
        preset_row = ui.row().classes('w-full gap-2 pf-wrap-row pf-actions')
        ui.label('Take-profit targets').classes('text-caption text-secondary-custom')
        targets_box = ui.column().classes('w-full gap-1')
        kept_box = ui.column().classes('w-full gap-1')
        problems_box = ui.column().classes('w-full gap-0')
        preview_box = ui.column().classes('w-full gap-0')
        check_box = ui.column().classes('w-full gap-0')
        result_box = ui.column().classes('w-full gap-0')

        def _collect() -> Dict[str, Any]:
            targets, parse_errors = parse_target_rows(rows_state)
            kept = [TpTarget(price=float(k['price']), fraction=float(k['pct']) / 100.0) for k in kept_state]
            sl_raw = sl_input.value
            errors = list(parse_errors)
            errors.extend(validate_protection(
                sl_price=None if sl_raw in (None, '') else float(sl_raw), targets=targets,
                last_price=price, position_quantity=quantity, tick_sizes=ticks))
            if sum(t.fraction for t in targets) + sum(t.fraction for t in kept) > 1.0 + 1e-9:
                errors.append('The take-profit shares, together with the targets already taken, exceed 100%.')
            marks = None
            if not parse_errors:
                marks = [row.get('taken') if row.get('taken') and (row['price'], row['pct']) == row.get('orig')
                         else None for row in rows_state]
            seen, unique = set(), []
            for e in errors:                                    # parse + validate can repeat
                if e not in seen:
                    seen.add(e)
                    unique.append(e)
            return {'targets': targets, 'kept': kept, 'marks': marks, 'errors': unique,
                    'sl': None if sl_raw in (None, '') else float(sl_raw)}

        async def _check() -> None:
            """'Check with broker': DRY RUNS only (nothing is placed), on the operator's click, never on load."""
            info = _collect()
            if info['errors']:
                return
            check_box.clear()
            with check_box:
                ui.spinner(size='sm')
            try:
                report = await asyncio.to_thread(aps.check_with_broker, account, symbol, info['sl'],
                                                 info['targets'], info['kept'], info['marks'])
            except Exception as e:  # noqa: BLE001 -- shown, not swallowed
                logger.error(f'TP/SL check for {symbol} failed: {e}', exc_info=True)
                check_box.clear()
                with check_box:
                    ui.label(f'The broker check failed: {e}').classes('text-xs').style(
                        f'color:{STATUS_HEX["negative"]}')
                return
            check_box.clear()
            with check_box:
                ui.label('Broker check (dry run, nothing placed):').classes('text-caption text-secondary-custom')
                for item in report.slices:
                    what = (f'STOP {item.quantity} sh @ {item.sl_price:g}' if item.tp_price is None else
                            f'OCO {item.quantity} sh TP {item.tp_price:g} / SL {item.sl_price:g}')
                    if item.ok:
                        change = ('' if item.bp_change is None else f', buying power {item.bp_change:+,.2f}')
                        ui.label(f'{what}: accepted{change}').classes('text-xs').style(
                            f'color:{STATUS_HEX["positive"]}')
                    else:
                        ui.label(f'{what}: REFUSED. {item.message}').classes('text-xs').style(
                            f'color:{STATUS_HEX["negative"]}')
                if report.available is not None:
                    ui.label(f'Available buying power: ${report.available:,.2f}').classes('text-xs')
                if report.warning:
                    ui.label(report.warning).classes('text-body2 text-weight-bold').style(
                        f'color:{STATUS_HEX["warning"]};white-space:normal')
                    if report.suggested_stop is not None:
                        ui.button(f'Use ~${report.suggested_stop:,.2f}', icon='tune',
                                  on_click=lambda: sl_input.set_value(report.suggested_stop)
                                  ).props('outline dense').mark(MARKER_USE_SUGGESTED)
                elif report.all_ok:
                    ui.label('The broker accepts this plan.').classes('text-xs').style(
                        f'color:{STATUS_HEX["positive"]}')

        def _recompute() -> None:
            info = _collect()
            check_box.clear()                                    # a stale verdict must not outlive an edit
            problems_box.clear()
            preview_box.clear()
            with problems_box:
                for message in info['errors']:
                    ui.label(message).classes('text-xs').style(f'color:{STATUS_HEX["negative"]}')
            if not info['errors']:
                with preview_box:
                    ui.label('Orders that will be placed (GTC):').classes('text-caption text-secondary-custom')
                    raw = [dict(t.to_dict(), taken=m) if m else t.to_dict()
                           for t, m in zip(info['targets'], info['marks'] or [None] * len(info['targets']))]
                    raw += [dict(t.to_dict(), filled=True) for t in info['kept']]
                    # What is PLACED: the targets already taken are left out and the rest spread over what is left.
                    for line in preview_orders(shares=shares, position_quantity=quantity,
                                               targets=effective_targets(raw, price, ticks).usable,
                                               sl_price=info['sl'],
                                               last_price=price, tick_sizes=ticks):
                        ui.label(line).classes('text-xs')
            save_button.set_enabled(not info['errors'] and not busy['v'] and data['is_long'])

        def _draw_rows() -> None:
            targets_box.clear()
            with targets_box:
                if not rows_state:
                    ui.label('No take-profit: the whole position gets a stop only.'
                             ).classes('text-xs text-secondary-custom')
                for index, row in enumerate(rows_state):
                    with ui.row().classes(f'w-full items-center gap-2 no-wrap {TP_ROW_CLASS}'):
                        ui.number(f'TP {index + 1} price', value=row['price'], min=0, step=0.01,
                                  format='%.4f',
                                  on_change=lambda e, r=row: (r.__setitem__('price', e.value), _recompute())
                                  ).props('dense outlined').classes('flex-grow')
                        ui.number('% of position', value=row['pct'], min=0, max=100, step=1,
                                  format='%.2f', suffix='%',
                                  on_change=lambda e, r=row: (r.__setitem__('pct', e.value), _recompute())
                                  ).props('dense outlined').classes('w-32')
                        ui.button(icon='delete', on_click=lambda r=row: _remove_row(r)
                                  ).props('flat dense round').tooltip('Remove this target'
                                  ).set_enabled(True)

        def _draw_kept() -> None:
            kept_box.clear()
            with kept_box:
                if kept_state:
                    ui.label('Targets already taken (not placed again unless re-armed)'
                             ).classes('text-caption text-secondary-custom')
                for entry in list(kept_state):
                    with ui.row().classes('w-full items-center gap-2 no-wrap'):
                        ui.label(f"TP {float(entry['price']):g}  {float(entry['pct']):g}%  taken"
                                 ).classes('text-body2 flex-grow').style('opacity:0.55')
                        ui.checkbox('Re-arm', value=False,
                                    on_change=lambda e, en=entry: _rearm(en) if e.value else None
                                    ).props('dense').mark(MARKER_REARM)

        def _rearm(entry: Dict[str, Any]) -> None:
            """The operator wants this target taken again: it becomes an ordinary editable row."""
            if entry in kept_state:
                kept_state.remove(entry)
                rows_state.append({'price': entry['price'], 'pct': entry['pct']})
            _draw_kept()
            _draw_rows()
            _recompute()

        def _remove_row(row: Dict[str, Any]) -> None:
            if row in rows_state:
                rows_state.remove(row)
            _redistribute()

        def _add_row() -> None:
            rows_state.append({'price': None, 'pct': None})
            _redistribute()

        def _redistribute() -> None:
            """After an add/remove: when the boxes were an even split of 100% (or incomplete) re-split
            them evenly, so a stale 100% next to a new row is not a validation error the operator did
            not cause. When they total LESS than 100% on purpose (a runner) leave them alone and give
            a new row what is left. They can type any split afterwards."""
            entered = [r['pct'] for r in rows_state if r['pct'] is not None]
            deliberate_runner = (len(entered) == len(rows_state) and rows_state
                                 and sum(entered) < 99.99 - 1e-9)
            if rows_state and not deliberate_runner:
                for row, pct in zip(rows_state, even_percentages(len(rows_state))):
                    row['pct'] = pct
            else:
                left = round(100.0 - sum(r['pct'] or 0.0 for r in rows_state[:-1]), 2) if rows_state else 0.0
                if rows_state and rows_state[-1]['pct'] is None:
                    rows_state[-1]['pct'] = max(0.0, left) or None
            _draw_rows()
            _recompute()

        def _apply_preset(key: str) -> None:
            try:
                result = apply_preset(key, average_cost, ticks)
            except ValueError as e:
                ui.notify(str(e), type='negative')
                return
            sl_input.set_value(result.sl_price)
            pcts = fractions_to_percentages([t.fraction for t in result.targets])
            rows_state[:] = [{'price': t.price, 'pct': pct} for t, pct in zip(result.targets, pcts)]
            _draw_rows()
            _recompute()

        with preset_row:
            for spec in PRESETS:
                button = ui.button(spec.label, on_click=lambda k=spec.key: _apply_preset(k)
                                   ).props('outline dense no-caps').mark(MARKER_PRESET_PREFIX + spec.key)
                button.tooltip(spec.description)
                button.set_enabled(bool(average_cost) and data['is_long'])
        _draw_rows()
        _draw_kept()
        add_button = ui.button('Add take-profit target', icon='add', on_click=_add_row
                               ).props('outline dense').mark(MARKER_ADD_TARGET)
        sl_input.on_value_change(lambda e: _recompute())

        # ------------------------------------------------------------ actions
        async def _run(label: str, work, *, close_on_ok: bool = True) -> None:
            if busy['v']:
                ui.notify('Another TP/SL action is still running', type='warning')
                return
            busy['v'] = True
            save_button.set_enabled(False)
            result_box.clear()
            with result_box:
                ui.spinner(size='sm')
            try:
                result = await asyncio.to_thread(work)
            except Exception as e:  # noqa: BLE001 -- surfaced in the dialog
                logger.error(f"TP/SL {label} failed for {symbol}: {e}", exc_info=True)
                result = aps.ActionResult(False, f'{label} failed: {e}')
            finally:
                busy['v'] = False
            changed['v'] = True
            result_box.clear()
            with result_box:
                ui.label(result.message).classes('text-body2 text-weight-bold').style(
                    f'color:{STATUS_HEX["positive" if result.ok else "negative"]}')
                for line in result.errors:
                    ui.label(line).classes('text-xs').style(f'color:{STATUS_HEX["negative"]}')
                for line in result.notes:
                    ui.label(line).classes('text-xs text-secondary-custom')
            ui.notify(result.message, type='positive' if result.ok else 'negative',
                      multi_line=True)
            if result.ok and close_on_ok:
                dialog.close()
            else:
                _recompute()

        def _save_work():
            info = _collect()
            if info['errors']:
                return aps.ActionResult(False, 'The TP/SL is not valid.', errors=info['errors'])
            return aps.save_protection(account, symbol, info['sl'], info['targets'],
                                       kept_filled=info['kept'], taken=info['marks'])

        def _confirm(text: str, label: str, work, *, close_on_ok: bool = True) -> None:
            with ui.dialog() as sure, ui.card():
                ui.label(text).classes('text-body2')
                with ui.row().classes('w-full justify-end gap-2'):
                    ui.button('Cancel', on_click=sure.close).props('flat')

                    async def _yes():
                        sure.close()
                        await _run(label, work, close_on_ok=close_on_ok)
                    ui.button('Yes', on_click=_yes).props('color=negative')
            sure.open()

        with ui.row().classes('w-full justify-end gap-2 mt-2 pf-actions'):
            ui.button('Close', on_click=dialog.close).props('flat')
            if protection is not None and protection.enabled:
                ui.button('Resize protection', icon='autorenew',
                          on_click=lambda: _confirm(
                              f'Cancel the resting TP/SL orders of {symbol} and place a fresh set '
                              f'from the saved numbers at the quantity held now? The position has '
                              f'no protective orders for a few seconds in between. This also renews '
                              f'the GTC lifetime.',
                              'Resize', lambda: aps.replace_protection(account, symbol))
                          ).props('outline').mark(MARKER_REPLACE)
            if protection is not None and protection.enabled and protection.alert_message \
                    and 'far below the market' in protection.alert_message:
                async def _use_accepted() -> None:
                    """Re-plan with a stop the broker accepts: an explicit action, never silent."""
                    stored = list(protection.tp_targets or [])
                    active = [TpTarget(price=float(t['price']), fraction=float(t['fraction']))
                              for t in stored if not t.get('filled')]
                    kept = [TpTarget(price=float(t['price']), fraction=float(t['fraction']))
                            for t in stored if t.get('filled')]
                    marks = [t.get('taken') for t in stored if not t.get('filled')]
                    try:
                        report = await asyncio.to_thread(aps.check_with_broker, account, symbol,
                                                         protection.sl_price, active, kept, marks)
                    except Exception as e:  # noqa: BLE001 -- shown, not swallowed
                        ui.notify(f'The broker check failed: {e}', type='negative')
                        return
                    if report.suggested_stop is None:
                        ui.notify('No stop the broker accepts could be found: free buying power instead.',
                                  type='warning')
                        return
                    _confirm(f'Change the stop of {symbol} from {protection.sl_price:g} to about '
                             f'{report.suggested_stop:,.2f} (approx.) and re-place the protective orders? '
                             f'The take-profit targets stay as they are.', 'Change stop',
                             lambda: aps.change_stop_and_replace(account, symbol, report.suggested_stop))
                ui.button('Use a stop the broker accepts', icon='tune', on_click=_use_accepted
                          ).props('outline').mark(MARKER_USE_ACCEPTED)
            if protection is not None and (protection.enabled or data['slices']):
                ui.button('Switch off', icon='shield_moon',
                          on_click=lambda: _confirm(
                              f'Cancel every TP/SL order of {symbol} and switch protection off? '
                              f'The position will have NO protective orders.', 'Switch off',
                              lambda: aps.disable_protection(account, symbol))
                          ).props('outline color=negative').mark(MARKER_SWITCH_OFF)
            if any(s.state == 'UNKNOWN' and s.closed_at is None and not (s.complex_order_id or s.sl_order_id)
                   for s in data['slices']):
                ui.button('Forget unresolved order', icon='help_center',
                          on_click=lambda: _confirm(
                              f'Only possible when the tag search itself cannot be completed AND the slice is '
                              f'at least {aps.UNKNOWN_MIN_AGE_SECONDS // 60} minutes old; otherwise the refresh '
                              f'resolves it by itself. Do it only after you LOOKED at the TastyTrade site and '
                              f'found no resting order for {symbol} that this platform placed (or cancelled '
                              f'it yourself): the slice is closed and no longer tracked, and an order that does '
                              f'exist would keep reserving shares untracked. Forget it?',
                              'Forget', lambda: aps.forget_unknown_slices(account, symbol),
                              close_on_ok=False)
                          ).props('outline color=negative').mark(MARKER_FORGET)
            check_button = ui.button('Check with broker', icon='fact_check', on_click=_check
                                     ).props('outline').mark(MARKER_CHECK)
            save_button = ui.button('Save and place orders', icon='shield',
                                    on_click=lambda: _run('Save', _save_work)
                                    ).props('color=primary').classes('pf-primary-action').mark(MARKER_SAVE)

        _recompute()

    async def _on_hide() -> None:
        if changed['v']:
            await refresh()
    dialog.on('hide', _on_hide)
    dialog.open()
