"""Session persistence for the Overview label charts' "labels shown" selection.

Session storage, NOT the database: this is a per-user view preference, not
allocation state that changes money. ``ui/pages/symbol360.py`` is the precedent
for both the ``app.storage.user`` pattern and its one hard constraint --
``app.storage.user`` raises ``RuntimeError`` outside a UI context, so it must be
guarded and must never be touched from a thread pool.

``resolve_growth_labels`` is pure and carries every rule; the two helpers around
it do nothing but read and write, guarded.

Two charts on the Overview tab own such a selector and each keeps its own key:
"Growth by Label" (:data:`GROWTH_LABELS_STORAGE_KEY`) and "Monthly Closed Profit
+ Dividends by Label" (:data:`MONTHLY_PROFIT_LABELS_STORAGE_KEY`). They are
deliberately separate so ticking a label in one does not move the other.
"""
from typing import List, Optional

from ...logger import logger

#: app.storage.user key holding the "Growth by Label" chart's label selection.
GROWTH_LABELS_STORAGE_KEY = 'overview_growth_labels'

#: app.storage.user key for the "Monthly Closed Profit + Dividends by Label" chart.
MONTHLY_PROFIT_LABELS_STORAGE_KEY = 'overview_monthly_profit_labels'

#: Excluded from the DEFAULT selection (it is the machine tag on almost every
#: instrument, so including it would drown the chart). A user who deliberately
#: ticks it still gets it -- this only shapes the fallback.
GROWTH_LABELS_DEFAULT_EXCLUDED = ('auto_added',)


def resolve_growth_labels(stored: Optional[List[str]],
                          available: List[str]) -> List[str]:
    """The labels the chart should show, given what was stored and what exists now.

    Rules, in order:
      * ``stored is None`` (never saved) -> the historical default: everything
        except ``auto_added``, or everything when that would be empty.
      * ``stored == []`` -> respected. Un-ticking every label is a real choice.
      * otherwise -> the stored labels that STILL EXIST, in ``available`` order so
        the chart's series order is stable. A deleted label cannot break it.
      * if that intersection is empty but ``stored`` was not, fall back to the
        default rather than drawing an empty chart.

    Pure: no storage, no NiceGUI.
    """
    options = [l for l in (available or []) if l]
    default = [l for l in options if l not in GROWTH_LABELS_DEFAULT_EXCLUDED] or list(options)

    if stored is None:
        return default
    if not stored:
        return []

    wanted = {s for s in stored if s}
    kept = [l for l in options if l in wanted]
    return kept if kept else default


def read_growth_labels(key: str = GROWTH_LABELS_STORAGE_KEY) -> Optional[List[str]]:
    """The stored selection, or ``None`` when nothing is stored / storage is unavailable.

    UI-thread only. ``app.storage.user`` raises ``RuntimeError`` outside a UI
    context (e.g. from an ``asyncio.to_thread`` worker), which is caught here so
    the chart still draws with its default. A value that is not a list is treated
    as absent rather than iterated -- stale or hand-edited session data must not
    turn into a per-character "selection".
    """
    try:
        from nicegui import app
        value = app.storage.user.get(key)
    except (RuntimeError, AttributeError) as e:
        logger.debug(f"Growth labels: storage unavailable for read of '{key}': {e}")
        return None
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        logger.warning(f"Growth labels: ignoring non-list stored value for '{key}': {value!r}")
        return None
    return [str(v) for v in value]


def write_growth_labels(labels: List[str],
                        key: str = GROWTH_LABELS_STORAGE_KEY) -> None:
    """Persist the selection. UI-thread only -- see :func:`read_growth_labels`.

    A storage failure is logged and swallowed: losing a view preference must never
    break the page.
    """
    try:
        from nicegui import app
        app.storage.user[key] = [str(l) for l in (labels or [])]
    except (RuntimeError, AttributeError) as e:
        logger.warning(f"Growth labels: could not persist the selection for '{key}': {e}")
