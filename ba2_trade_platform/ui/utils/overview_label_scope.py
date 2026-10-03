"""Page-level label scope and chart-dropdown selections for the Overview growth tab.

Why the DATABASE and not ``app.storage.user`` (which ``growth_label_storage``
uses): the operator reads the page from a phone and a PC against the same server
and expects the same choices on both. ``app.storage.user`` is per browser. The
choices are therefore ``AppSetting`` rows (``value_str`` holding JSON), keyed per
ACCOUNT, so switching accounts shows that account's own choices.

Everything that decides anything is a pure function here (no DB, no NiceGUI), so
it is unit-tested directly. The ``*_overview_setting`` wrappers are thin and
guarded; database access is safe from worker threads.

Concepts
--------
* Scope: the labels the whole page considers. Charts' option lists are
  intersected with it.
* Default scope (nothing saved): every label of every symbol the account has
  traded. It deliberately INCLUDES ``auto_added`` -- the scope only decides which
  labels are offered; the charts' own default selection still drops
  ``auto_added`` (``growth_label_storage.resolve_growth_labels``), so a user who
  never touches anything sees exactly today's charts.
* Follow portfolio manager: while on, the scope IS the account's managed
  labels, re-read on every render (so edits in the pf manager follow). If the
  account manages nothing the stored/default scope is used instead.
* All accounts: every account's own scope (and chart selection) is resolved
  separately and the results are unioned.
"""
import json
from typing import Any, Iterable, List, Optional, Sequence

from ...logger import logger
from .growth_label_storage import resolve_growth_labels

#: Chart ids used in the per-chart keys.
CHART_GROWTH = 'growth'
CHART_MONTHLY = 'monthly_profit'
CHART_POSITION_LABEL = 'position_label'
CHART_POSITION_SYMBOL = 'position_symbol'


def scope_key(account_id: int) -> str:
    return f'overview_labels_scope_{account_id}'


def follow_pf_key(account_id: int) -> str:
    return f'overview_labels_scope_follow_pf_{account_id}'


def chart_key(chart: str, account_id: int) -> str:
    """Multi-select chart selection: ``overview_<chart>_labels_<account>``."""
    return f'overview_{chart}_labels_{account_id}'


def single_key(chart: str, account_id: int) -> str:
    """Single-select chart value: ``overview_<chart>_<account>``."""
    return f'overview_{chart}_{account_id}'


# --------------------------------------------------------------------------
# pure rules
# --------------------------------------------------------------------------

def _clean(labels: Optional[Iterable[Any]]) -> List[str]:
    """De-duplicated, blank-free, order-preserving list of strings."""
    seen, out = set(), []
    for l in labels or []:
        s = str(l).strip() if l is not None else ''
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def default_scope(traded_labels: Optional[Iterable[str]]) -> List[str]:
    """Labels of every traded symbol (sorted). Includes ``auto_added`` -- see module doc."""
    return sorted(_clean(traded_labels))


def resolve_scope(stored: Optional[Sequence[str]], traded_labels: Optional[Iterable[str]],
                  follow_pf: bool = False,
                  managed: Optional[Iterable[str]] = None) -> List[str]:
    """One account's in-scope labels.

    * follow on and the account has managed labels -> the managed labels;
    * else a stored selection (``[]`` is respected: "none" is a real choice);
    * else (never saved) the default: every traded label.
    """
    managed_clean = _clean(managed)
    if follow_pf and managed_clean:
        return sorted(managed_clean)
    if stored is not None:
        return sorted(_clean(stored))
    return default_scope(traded_labels)


def union_scopes(scopes: Iterable[Iterable[str]]) -> List[str]:
    """Union of several accounts' scopes (sorted)."""
    out = set()
    for s in scopes:
        out.update(_clean(s))
    return sorted(out)


def apply_scope(labels: Optional[Iterable[str]], scope: Optional[Iterable[str]]) -> List[str]:
    """``labels`` restricted to the scope, in ``labels`` order. ``scope=None`` = no restriction."""
    items = _clean(labels)
    if scope is None:
        return items
    allowed = set(_clean(scope))
    return [l for l in items if l in allowed]


def resolve_chart_selection(stored_by_account: Sequence[Optional[Sequence[str]]],
                            options: Sequence[str]) -> List[str]:
    """A multi-select chart's selection.

    Each account's stored choice is resolved against ``options`` with the existing
    rule (``resolve_growth_labels``: never saved = default minus ``auto_added``,
    ``[]`` respected, deleted labels dropped), then the accounts are unioned, in
    ``options`` order. One account = that account's own choice.
    """
    opts = _clean(options)
    chosen = set()
    for stored in (stored_by_account or [None]):
        chosen.update(resolve_growth_labels(None if stored is None else list(stored), opts))
    return [o for o in opts if o in chosen]


def pick_single(stored_values: Sequence[Optional[str]], options: Sequence[str],
                default: Optional[str]) -> Optional[str]:
    """A single-select's value: the first stored value (account order) still an option, else ``default``."""
    opts = set(options or [])
    for v in stored_values or []:
        if v is not None and v in opts:
            return v
    return default


def select_all(options: Iterable[str]) -> List[str]:
    return _clean(options)


def select_none() -> List[str]:
    return []


# --------------------------------------------------------------------------
# thin guarded DB wrappers (AppSetting.value_str holds JSON)
# --------------------------------------------------------------------------

def read_overview_setting(key: str) -> Optional[Any]:
    """The JSON-decoded stored value, or ``None`` when absent / unreadable."""
    try:
        from ...core.db import get_db
        from ...core.models import AppSetting
        from sqlmodel import select
        with get_db() as session:
            row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
            raw = row.value_str if row else None
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001 -- a view preference must not break the page
        logger.warning(f"Overview scope: could not read '{key}': {e}")
        return None


def write_overview_setting(key: str, value: Any) -> bool:
    """Upsert the JSON-encoded value. Returns False (logged) on failure."""
    try:
        from ...core.db import get_db
        from ...core.models import AppSetting
        from sqlmodel import select
        text = json.dumps(value)
        with get_db() as session:
            row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
            if row:
                row.value_str = text
                session.add(row)
            else:
                session.add(AppSetting(key=key, value_str=text))
            session.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Overview scope: could not persist '{key}': {e}")
        return False


def read_stored_list(key: str, legacy_key: Optional[str] = None) -> Optional[List[str]]:
    """DB list for ``key``; if absent and ``legacy_key`` is given, the old per-browser
    ``app.storage.user`` value once, written through to the DB so it is not lost.
    UI-thread only when ``legacy_key`` is used (it touches NiceGUI storage)."""
    value = read_overview_setting(key)
    if isinstance(value, list):
        return [str(v) for v in value]
    if legacy_key is not None:
        from .growth_label_storage import read_growth_labels
        legacy = read_growth_labels(legacy_key)
        if legacy is not None:
            write_overview_setting(key, legacy)
            return legacy
    return None


def read_follow_pf(account_id: int) -> bool:
    return read_overview_setting(follow_pf_key(account_id)) is True
