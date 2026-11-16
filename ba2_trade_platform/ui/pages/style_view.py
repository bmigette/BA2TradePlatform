"""Read-only O_STYLE view: one card per member of a composed option expert, in priority order."""
from nicegui import ui

from ...core.style_view import load_ruleset_style_views, load_style_summary
from ...logger import logger

_GROUP_LABELS = [("signal", "Signal gates"), ("iv", "IV gates"), ("market", "Market gates"),
                 ("other", "Other gates")]


def _chips(items, color):
    with ui.row().classes('gap-1 flex-wrap'):
        for text in items:
            ui.chip(text, color=color).props('dense outline square')


def _render_exit(exit_rule):
    with ui.row().classes('items-center gap-2 flex-wrap'):
        ui.label(exit_rule['name']).classes('text-xs opacity-70')
        _chips(exit_rule['gates'] or ['(always)'], 'orange')
        ui.icon('arrow_forward').classes('text-sm')
        ui.label(', '.join(exit_rule['actions']) or '(no action)').classes('text-sm font-mono')
    for raw in exit_rule['unreadable']:
        ui.label(f'unreadable: {raw}').classes('text-xs text-negative')


def _render_member(member):
    with ui.card().classes('w-full'):
        with ui.row().classes('items-center gap-3'):
            ui.badge(f"#{member['rank']}", color='primary')
            ui.label(member['structure']).classes('text-h6')
            ui.label(f"entry tag {member['tag']}").classes('text-xs opacity-70')
        ui.label('Entry rules (first match wins)').classes('text-subtitle2 mt-2')
        for entry in member['entries']:
            with ui.card().props('flat bordered').classes('w-full'):
                with ui.row().classes('items-center gap-2'):
                    ui.label(f"rule {entry['precedence']}: {entry['name']}").classes('font-medium')
                    if entry['continue_processing']:
                        ui.badge('continues', color='warning')
                with ui.row().classes('gap-4 flex-wrap'):
                    for p in entry['params']:
                        ui.label(f"{p['label']}: {p['value']}").classes('text-sm font-mono')
                for group, label in _GROUP_LABELS:
                    if entry['gates'][group]:
                        ui.label(label).classes('text-xs opacity-70')
                        _chips(entry['gates'][group], 'primary')
                for raw in entry['unreadable']:
                    ui.label(f'unreadable: {raw}').classes('text-xs text-negative')
        ui.label('Exits for this member').classes('text-subtitle2 mt-2')
        if not member['exits']:
            ui.label('none (only the all-members exits apply)').classes('text-sm opacity-70')
        for exit_rule in member['exits']:
            _render_exit(exit_rule)


def _render_summary(summary):
    for member in summary['members']:
        _render_member(member)
    with ui.card().classes('w-full'):
        ui.label('Exits for all members').classes('text-subtitle2')
        if not summary['all_member_exits']:
            ui.label('none').classes('text-sm opacity-70')
        for exit_rule in summary['all_member_exits']:
            _render_exit(exit_rule)
    if summary['orphan_exits']:
        with ui.card().classes('w-full'):
            ui.label('Exits scoped to a tag with no entry rule (never fire)').classes('text-subtitle2')
            for exit_rule in summary['orphan_exits']:
                ui.label(f"tag {exit_rule['tag']}").classes('text-xs opacity-70')
                _render_exit(exit_rule)


def _nothing_to_show(text):
    with ui.card().classes('w-full'):
        ui.label('Nothing to show').classes('text-h6')
        ui.label(text).classes('text-body1')
        ui.link('Back to settings', '/settings')


def ruleset_content(ruleset_id: int):
    """Render /style/ruleset/<ruleset_id>: the option structures of an entry ruleset."""
    try:
        data = load_ruleset_style_views(ruleset_id)
    except Exception as e:
        logger.error(f'Structure view for ruleset {ruleset_id} failed: {e}', exc_info=True)
        ui.label(f'Could not load ruleset {ruleset_id}: {e}').classes('text-negative')
        return
    if data is None:
        _nothing_to_show(f'Ruleset {ruleset_id} does not exist or opens no option structure under a '
                         f'member tag. This page lists the structures of the entry ruleset of a '
                         f'composed option expert.')
        return
    ui.label(f"Entry ruleset #{data['ruleset']['id']}: {data['ruleset']['name']}").classes('text-h5')
    n = len(data['views'][0]['summary']['members'])
    ui.label(f'{n} members, first match wins in the order shown').classes('text-sm opacity-80')
    for view in data['views']:
        with ui.card().classes('w-full'):
            if view['exit_ruleset'] is None:
                ui.label('No open-positions ruleset is paired with this entry ruleset by an expert: '
                         'entries only, no exits to show.').classes('text-sm text-warning')
            else:
                ui.label(f"Exits from open-positions ruleset #{view['exit_ruleset']['id']}: "
                         f"{view['exit_ruleset']['name']}").classes('text-subtitle2')
            if view['experts']:
                _chips([f"#{e['id']} {e['alias'] or e['expert']}"
                        + ('' if e['enabled'] else ' (disabled)') for e in view['experts']], 'primary')
        _render_summary(view['summary'])


def content(instance_id: int):
    """Render /style/<instance_id>. (No awaits, so page CSS cannot be dropped.)"""
    try:
        summary = load_style_summary(instance_id)
    except Exception as e:
        logger.error(f'Style view for expert {instance_id} failed: {e}', exc_info=True)
        ui.label(f'Could not load expert {instance_id}: {e}').classes('text-negative')
        return
    if summary is None:
        _nothing_to_show(f'Expert instance {instance_id} does not exist or is not a style expert '
                         f'(no entry rule carries a member tag).')
        return
    h = summary['header']
    with ui.row().classes('items-center gap-4'):
        ui.label(f"Expert #{h['id']}: {h['alias'] or h['expert']}").classes('text-h5')
        ui.badge('enabled' if h['enabled'] else 'disabled',
                 color='positive' if h['enabled'] else 'negative')
    ui.label(f"Account: {h['account']}   |   Virtual equity: {h['virtual_equity_pct']:g}%   |   "
             f"{len(summary['members'])} members, first match wins in the order shown"
             ).classes('text-sm opacity-80')
    _render_summary(summary)
