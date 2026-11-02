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

#: Not engine statuses either: what a row that was IN a submit reads when the run ends
#: without having reported on it. ``NOT_SENT``: the run finished (or was refused by the
#: gate re-check) and this row simply has no order -- neutral, not a problem.
#: ``UNKNOWN_CHECK_BROKER``: the run died part-way (an exception), so an order for this
#: row may or may not have reached the broker -- amber, and it says to go and look.
STATUS_NOT_SENT = 'not_sent'
STATUS_UNKNOWN_CHECK_BROKER = 'unknown_check_broker'

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
    STATUS_NOT_SENT: OutcomeIcon('radio_button_unchecked', GREY, 'not sent'),
    STATUS_UNKNOWN_CHECK_BROKER: OutcomeIcon('help', AMBER, 'unknown - check the broker'),
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
    if status in (OUTCOME_UNACTIONABLE, OUTCOME_WASHTRADE_LOCKED, STATUS_UNKNOWN_CHECK_BROKER):
        return MARK_ALERT
    return None


#: Worst first. A symbol can be reported more than once in a run (a close, then a new
#: order); the row must show the WORST of them, never the last -- a FAILED close
#: followed by a sent order must not read green.
_SEVERITY_ORDER = (
    OUTCOME_FAILED, STATUS_UNKNOWN_CHECK_BROKER, OUTCOME_UNACTIONABLE,
    OUTCOME_WASHTRADE_LOCKED, OUTCOME_PARTIAL, OUTCOME_SUBMITTED, OUTCOME_SKIPPED,
    STATUS_NOT_SENT, STATUS_PENDING,
)


def status_severity(status: Optional[str]) -> int:
    """Higher is worse. TOTAL: a status this module has never heard of ranks just below
    FAILED -- an outcome nobody understands must not be outranked by a green tick."""
    try:
        return len(_SEVERITY_ORDER) - _SEVERITY_ORDER.index(status)
    except ValueError:
        return len(_SEVERITY_ORDER) - 1


def worst_outcome(existing: Any, new: Any) -> Any:
    """Which of two outcomes for the SAME symbol the row should show. ``new`` wins a
    tie (the later report of an equally bad state is the fresher one); ``None`` never
    beats a real outcome. Pure."""
    if existing is None:
        return new
    if new is None:
        return existing
    if status_severity(getattr(existing, 'status', None)) > \
            status_severity(getattr(new, 'status', None)):
        return existing
    return new


def row_mark_class(status: Optional[str]) -> str:
    """The CSS class for ``row_marking``, or ''. Pure."""
    mark = row_marking(status)
    return MARK_CLASSES[mark] if mark else ''


def _qty(value: Any, *, none: str = '-') -> str:
    return none if value is None else f'{float(value):,.4f}'


def _ids(values: Optional[Sequence[Any]]) -> str:
    return ', '.join(str(v) for v in values) if values else '-'


def outcome_details(outcome: Any, *, symbol: str, run_id: Optional[int],
                    when: Optional[datetime] = None,
                    status: Optional[str] = None) -> List[Tuple[str, str]]:
    """``[(label, value), ...]`` for the details view behind a row's icon. Pure.

    NEVER raises: ``outcome`` may be missing (the icon was tapped while the order was
    still being sent), and any field may be empty. ``filled_quantity is None`` means
    the broker reported nothing, and is drawn ``-`` -- never 0, which would read as
    "nothing filled" for an order that is still working.
    """
    when_text = when.strftime('%Y-%m-%d %H:%M:%S') if when is not None else '-'
    run_text = '-' if run_id is None else str(run_id)
    if outcome is None:
        shown = status or STATUS_PENDING
        message = {
            STATUS_NOT_SENT: 'No order was sent for this row in this run.',
            STATUS_UNKNOWN_CHECK_BROKER: (
                'The run stopped with an error before it reported on this row. An '
                'order may or may not have reached the broker - check the broker.'),
        }.get(shown, 'No outcome has been reported for this row yet.')
        return [('Symbol', symbol or '-'),
                ('Status', outcome_icon(shown).label),
                ('Message', message),
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


def copy_click_js(text: str) -> str:
    """The JS ``click`` handler of the Copy button. Pure.

    It runs IN THE BROWSER, synchronously inside the tap -- iOS Safari only honours a
    copy made within the user gesture, and a server round trip (``run_javascript``)
    is no longer inside it. ``execCommand("copy")`` on a read-only textarea is tried
    first because it works over plain http, which is how this app is served on the LAN
    (``http://192.168.x.x:8081``) and where ``navigator.clipboard`` is undefined; the
    async clipboard API is the fallback in a secure context. The verdict goes back to
    the server with ``emit({ok})`` so the toast can be honest about it.
    """
    import json
    literal = json.dumps(text)
    return (
        '(e) => { const t = ' + literal + '; '
        # The textarea goes INSIDE the dialog card: Quasar's dialog pulls focus back to
        # itself, so a textarea appended to <body> never keeps focus or a selection --
        # and execCommand("copy") then still returns true having copied nothing.
        'const host = (e && e.target && e.target.closest && e.target.closest(".q-card")) '
        '|| document.body; '
        'const a = document.createElement("textarea"); a.value = t; '
        'a.setAttribute("readonly", ""); a.style.position = "fixed"; '
        'a.style.opacity = "0"; a.style.left = "0"; a.style.top = "0"; '
        'host.appendChild(a); a.focus(); a.select(); a.setSelectionRange(0, t.length); '
        # Only trust execCommand when the textarea really holds focus and a selection.
        'let ok = false; '
        'if (document.activeElement === a && a.selectionEnd - a.selectionStart === t.length) '
        '{ try { ok = document.execCommand("copy"); } catch (x) {} } '
        'host.removeChild(a); '
        'if (!ok && navigator.clipboard && window.isSecureContext) { '
        'navigator.clipboard.writeText(t).then(() => emit({ok: true}), '
        '() => emit({ok: false})); } else { emit({ok: ok}); } }'
    )


def copy_succeeded(args: Any) -> bool:
    """Whether the browser reported the copy worked. Tolerant of the shapes NiceGUI
    delivers (``[{"ok": true}]``, ``{"ok": true}``, ``true``); anything else, or
    nothing, is a failure -- never a silent success. Pure."""
    if isinstance(args, (list, tuple)):
        args = args[0] if args else None
    if isinstance(args, dict):
        args = args.get('ok')
    return args is True
