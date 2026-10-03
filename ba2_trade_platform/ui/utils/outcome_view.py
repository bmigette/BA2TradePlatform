"""What a submitted row looks like in the dry-run table: its icon, its marking and
its details. Pure -- no NiceGUI -- so every decision is unit-testable.

The dry-run dialog stays open through a Submit and each row reports itself in its
Result column (see ``AllocationWizard.set_row_outcome``). There is no second
"results" dialog: the table the user just read IS the result. Three things make that
enough, and they are decided here:

* ``outcome_icon``      -- one icon + colour + label per status, TOTAL: an unknown
  status gets a neutral warning icon, never a crash or a blank;
* ``row_marking``       -- which rows are painted as a failure (red) and which as
  "not sent, look at me" (amber). A failure is never confused with a row that simply
  was not sent;
* ``outcome_details``   -- the rows of the details view behind the icon, with a
  missing outcome and empty fields drawn as what they are (``None`` filled quantity is
  "-", never 0).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple

from ...core.portfolio_allocation_service import (
    OUTCOME_FAILED, OUTCOME_PARTIAL, OUTCOME_SKIPPED, OUTCOME_SUBMITTED,
    OUTCOME_UNACTIONABLE, OUTCOME_WASHTRADE_LOCKED,
)

#: Not an engine status: the state of a row between "Submit pressed" and "its order
#: came back". Drawn as a spinner by the wizard.
STATUS_PENDING = 'pending'

#: Row markings. ``MARK_FAILED`` is for a refused order; ``MARK_ALERT`` is for rows
#: that were NOT sent and need a human, which is a different thing and must not read
#: as the same red.
MARK_FAILED = 'failed'
MARK_ALERT = 'alert'
MARK_CLASSES = {MARK_FAILED: 'pf-row-failed', MARK_ALERT: 'pf-row-alert'}

#: Palette. Hexes rather than Tailwind classes: the colour utilities do not paint on
#: this build (see ``class_color_style``), so the wizard applies these inline.
GREEN = '#22c55e'
RED = '#ef4444'
AMBER = '#f59e0b'
GREY = '#9ca3af'
BLUE = '#60a5fa'
UNKNOWN_GREY = '#cbd5e1'


@dataclass(frozen=True)
class OutcomeIcon:
    """``icon`` is a Material icon name; ``label`` is what a tooltip / the details
    header says."""
    icon: str
    colour: str
    label: str

    @property
    def quasar_color(self) -> str:
        """The Quasar palette name for ``colour``, for a ``q-btn`` (whose icon takes
        its colour from the ``color`` prop, not from an inline style)."""
        return _QUASAR_COLORS.get(self.colour, 'grey-5')


_QUASAR_COLORS = {GREEN: 'positive', RED: 'negative', AMBER: 'warning', GREY: 'grey-5',
                  BLUE: 'info', UNKNOWN_GREY: 'grey-4'}

_ICONS = {
    STATUS_PENDING: OutcomeIcon('hourglass_empty', GREY, 'sending'),
    OUTCOME_SUBMITTED: OutcomeIcon('check_circle', GREEN, 'sent'),
    OUTCOME_PARTIAL: OutcomeIcon('timelapse', AMBER, 'partially filled'),
    OUTCOME_FAILED: OutcomeIcon('error', RED, 'FAILED'),
    OUTCOME_WASHTRADE_LOCKED: OutcomeIcon('lock_clock', AMBER,
                                          'wash-trade block, retried automatically'),
    OUTCOME_UNACTIONABLE: OutcomeIcon('block', AMBER, 'not sent - needs a human'),
    OUTCOME_SKIPPED: OutcomeIcon('remove_circle_outline', GREY, 'skipped - nothing to do'),
}

#: Every status the engine can produce, plus the pending pseudo-status. Tests pin
#: that ``_ICONS`` covers exactly this and that it still covers the service's own
#: constants.
KNOWN_STATUSES = tuple(_ICONS)


def outcome_icon(status: Optional[str]) -> OutcomeIcon:
    """The icon for ``status``. TOTAL: an unknown (or missing) status is a neutral
    warning icon that names the status, so the engine growing a status this table
    has never heard of is visible rather than blank."""
    found = _ICONS.get(status or '')
    if found is not None:
        return found
    return OutcomeIcon('help_outline', UNKNOWN_GREY, f'unknown status: {status or "(none)"}')


def row_marking(status: Optional[str]) -> Optional[str]:
    """``MARK_FAILED`` for a refused order, ``MARK_ALERT`` for a row that was not
    sent and needs a human (unactionable, wash-trade locked), else ``None``.

    FAILED only is red: skipped and sent rows are never painted as a problem."""
    if status == OUTCOME_FAILED:
        return MARK_FAILED
    if status in (OUTCOME_UNACTIONABLE, OUTCOME_WASHTRADE_LOCKED):
        return MARK_ALERT
    return None


def row_mark_class(status: Optional[str]) -> str:
    """The CSS class for ``row_marking``, or ''. Pure."""
    mark = row_marking(status)
    return MARK_CLASSES[mark] if mark else ''


def _qty(value: Any, *, none: str = '-') -> str:
    return none if value is None else f'{float(value):,.4f}'


def _ids(values: Optional[Sequence[Any]]) -> str:
    return ', '.join(str(v) for v in values) if values else '-'


def outcome_details(outcome: Any, *, symbol: str, run_id: Optional[int],
                    when: Optional[datetime] = None) -> List[Tuple[str, str]]:
    """``[(label, value), ...]`` for the details view behind a row's icon. Pure.

    NEVER raises: ``outcome`` may be missing (the icon was tapped while the order was
    still being sent), and any field may be empty. ``filled_quantity is None`` means
    the broker reported nothing, and is drawn ``-`` -- never 0, which would read as
    "nothing filled" for an order that is still working.
    """
    when_text = when.strftime('%Y-%m-%d %H:%M:%S') if when is not None else '-'
    run_text = '-' if run_id is None else str(run_id)
    if outcome is None:
        return [('Symbol', symbol or '-'),
                ('Status', outcome_icon(STATUS_PENDING).label),
                ('Message', 'No outcome has been reported for this row yet.'),
                ('Run', run_text), ('Time', when_text)]
    status = getattr(outcome, 'status', '') or ''
    return [
        ('Symbol', getattr(outcome, 'symbol', '') or symbol or '-'),
        ('Action', getattr(outcome, 'action', '') or '-'),
        ('Status', f'{outcome_icon(status).label} ({status or "none"})'),
        ('Planned qty', _qty(getattr(outcome, 'quantity', None))),
        ('Filled qty', _qty(getattr(outcome, 'filled_quantity', None))),
        ('Path', getattr(outcome, 'path', '') or '-'),
        ('Order id(s)', _ids(getattr(outcome, 'order_ids', None))),
        ('Transaction id(s)', _ids(getattr(outcome, 'transaction_ids', None))),
        ('Message', getattr(outcome, 'message', '') or '-'),
        ('Run', run_text),
        ('Time', when_text),
    ]


def outcome_details_text(details: Sequence[Tuple[str, str]]) -> str:
    """The details as plain text, one ``label: value`` per line, for Copy. Pure."""
    return '\n'.join(f'{label}: {value}' for label, value in details)


def copy_to_clipboard_js(text: str) -> str:
    """JS that copies ``text`` to the clipboard. Pure.

    ``navigator.clipboard`` only exists in a SECURE context, and this app is served
    over plain http on the LAN (``http://192.168.x.x:8081``), where it is undefined
    -- on exactly the phone this was written for. So: use it when it is there, and
    fall back to a hidden textarea and ``execCommand('copy')`` when it is not.
    Resolves to ``true`` / ``false`` so the caller can say whether it worked.
    """
    import json
    literal = json.dumps(text)
    return (
        '(async () => { const t = ' + literal + '; '
        'try { if (navigator.clipboard && window.isSecureContext) '
        '{ await navigator.clipboard.writeText(t); return true; } } catch (e) {} '
        'const a = document.createElement("textarea"); a.value = t; '
        'a.style.position = "fixed"; a.style.opacity = "0"; '
        'document.body.appendChild(a); a.focus(); a.select(); '
        'let ok = false; try { ok = document.execCommand("copy"); } catch (e) {} '
        'document.body.removeChild(a); return ok; })()'
    )
