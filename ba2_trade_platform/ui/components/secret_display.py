"""Read-only secret displays: masked on every render, an eye button reveals ONE view.

The reveal state lives in the closure of the rendered element only -- never in storage, never
logged -- so every page load / dialog open starts hidden.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from nicegui import ui

from ..utils.secret_mask import mask_secrets_in_data

#: 40 px square: a thumb-sized tap target on a phone.
_EYE_PROPS = 'flat round dense'
_EYE_STYLE = 'min-width:40px;min-height:40px'


def masked_json_view(data: Any, definitions: Optional[Mapping[str, Mapping[str, Any]]] = None,
                     classes: str = 'w-full', read_only: bool = True) -> None:
    """A JSON viewer with secret-keyed values masked; the eye button re-renders it unmasked."""
    state = {'shown': False}
    with ui.column().classes('w-full gap-1'):
        with ui.row().classes('items-center gap-1'):
            button = ui.button(icon='visibility').props(_EYE_PROPS).style(_EYE_STYLE)
            ui.label('Secret values are hidden').classes('text-xs text-grey-6')
        holder = ui.column().classes('w-full')

        def render():
            holder.clear()
            shown = data if state['shown'] else mask_secrets_in_data(data, definitions)
            with holder:
                editor = ui.json_editor({'content': {'json': shown}}).classes(classes)
                if read_only:
                    editor.props('read-only')

        def toggle():
            state['shown'] = not state['shown']
            button.props(f"icon={'visibility_off' if state['shown'] else 'visibility'}")
            render()

        button.on_click(toggle)
        render()
