"""Stored copies of the slow reads behind the Account Growth tab and the Portfolio Allocation page.

Stale-while-revalidate: a page paints at once from the last good read stored here, then refreshes from the broker in
FULL (a back-dated booking is still caught) and stores the result again. Tables: ``core/growth_snapshot_models.py``.

THE CODEC NEVER STORES A LOSSY COPY. A broker row is a flat dict whose values are ``str`` / ``int`` / ``float`` /
``bool`` / ``None`` / ``datetime.date`` / ``datetime.datetime`` (naive or aware); the type and the tzinfo come back
exactly (a ``date`` stays a ``date``, ``TzInfo(UTC)`` from pydantic stays that class). Anything else -- a nested
dict, a ``Decimal``, an enum, a tzinfo the codec does not know -- is refused with a ``TypeError`` naming the key and
the type. Positions are ``Position`` SQLModel objects, stored field by field and restored as ``Position`` objects;
their enum fields are restored too (``OrderDirection`` side, Alpaca's exchange / asset class), from a fixed list.

LOADERS NEVER RAISE: a row that cannot be read (database error, another schema version, a payload the codec cannot
decode) is ABSENT and the reason is logged at WARNING, and the page then does the blocking broker fetch it always
did. A missing row is not a warning (the first load of an account has none). SAVERS RAISE (the codec's
``TypeError``, a database error); the callers log a WARNING and go on, a failed save never fails a page load.
"""
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlmodel import select

from ..core.db import get_db
from ..core.growth_snapshot_models import (
    DATASET_DIVIDENDS, DATASET_GROWTH, DATASET_SHARE_MOVEMENTS, GROWTH_SNAPSHOT_SCHEMA_VERSION, AccountGrowthSnapshot,
    SymbolDividendHistory)
from ..core.models import Position
from ..core.types import OrderDirection
from ..logger import logger

#: A stored FMP dividend history older than this is still USED, and refreshed in the background.
DIVIDEND_HISTORY_MAX_AGE_SECONDS = 24 * 3600

# --------------------------------------------------------------------------------------------------------------------
# codec
# --------------------------------------------------------------------------------------------------------------------
# An encoded VALUE is a JSON scalar (str / int / float / bool / null: itself) or a ONE-KEY JSON object naming the
# type. A row never holds an object of its own (nested values are refused), so the two cannot be confused.
_DATE, _NAIVE, _AWARE, _ENUM = "date", "datetime", "datetime_tz", "enum"
_TZ_FIXED, _TZ_ZONE, _TZ_PYDANTIC = "fixed", "zone", "pydantic"


def _enum_types() -> Dict[str, type]:
    """The enum classes a stored row may name (by ``module.QualName``). A FIXED list: a stored name never imports
    anything that is not on it."""
    types = [OrderDirection]
    try:
        from alpaca.trading.enums import AssetClass, AssetExchange
        types += [AssetClass, AssetExchange]
    except ImportError:          # alpaca-py absent: such a value is refused on save and undecodable on load
        pass
    return {f"{t.__module__}.{t.__qualname__}": t for t in types}


_ENUMS = _enum_types()


def _encode_tz(key: str, tz: tzinfo) -> list:
    if type(tz) is timezone:
        offset = tz.utcoffset(None)
        name = tz.tzname(None)
        # ``timezone(offset)`` without a name derives one; only an explicit name needs storing
        default_name = timezone(offset).tzname(None)
        return [_TZ_FIXED, offset.total_seconds(), None if name == default_name else name]
    try:
        from zoneinfo import ZoneInfo
    except ImportError:          # pragma: no cover -- Python 3.9+
        ZoneInfo = None
    if ZoneInfo is not None and type(tz) is ZoneInfo and tz.key:
        return [_TZ_ZONE, tz.key]
    try:
        from pydantic_core import TzInfo
    except ImportError:          # pragma: no cover
        TzInfo = None
    if TzInfo is not None and type(tz) is TzInfo:
        return [_TZ_PYDANTIC, tz.utcoffset(None).total_seconds()]
    raise TypeError(f"cannot store {key!r}: a datetime with tzinfo of type {type(tz).__name__} "
                    f"does not round-trip exactly")


def _decode_tz(spec) -> tzinfo:
    kind = spec[0]
    if kind == _TZ_FIXED:
        offset = timedelta(seconds=spec[1])
        return timezone(offset, spec[2]) if spec[2] is not None else timezone(offset)
    if kind == _TZ_ZONE:
        from zoneinfo import ZoneInfo
        return ZoneInfo(spec[1])
    if kind == _TZ_PYDANTIC:
        from pydantic_core import TzInfo
        return TzInfo(int(spec[1]))
    raise ValueError(f"unknown tzinfo tag {kind!r}")


def _encode_value(key: str, value: Any, *, enums: bool = False) -> Any:
    kind = type(value)
    if value is None or kind in (str, int, bool):
        return value
    if kind is float:
        return value
    if kind is date:
        return {_DATE: value.isoformat()}
    if kind is datetime:
        # ``fold`` (which of two repeated wall times, at a DST change) is not part of isoformat: kept beside it
        if value.tzinfo is None:
            return {_NAIVE: [value.isoformat(), value.fold]}
        return {_AWARE: [value.replace(tzinfo=None).isoformat(), _encode_tz(key, value.tzinfo), value.fold]}
    if enums and isinstance(value, Enum):
        name = f"{kind.__module__}.{kind.__qualname__}"
        if _ENUMS.get(name) is kind:
            return {_ENUM: [name, value.value]}
    raise TypeError(f"cannot store {key!r}: values of type {kind.__name__} do not round-trip exactly")


def _decode_value(value: Any) -> Any:
    if value is None or type(value) in (str, int, float, bool):
        return value
    if type(value) is not dict or len(value) != 1:
        raise ValueError(f"not an encoded value: {value!r}")
    (tag, data), = value.items()
    if tag == _DATE:
        return date.fromisoformat(data)
    if tag == _NAIVE:
        out = datetime.fromisoformat(data[0])
        if out.tzinfo is not None:
            raise ValueError(f"naive datetime tag carries an offset: {data!r}")
        return out.replace(fold=int(data[1]))
    if tag == _AWARE:
        out = datetime.fromisoformat(data[0])
        if out.tzinfo is not None:
            raise ValueError(f"aware datetime tag carries an offset in its wall time: {data!r}")
        return out.replace(tzinfo=_decode_tz(data[1]), fold=int(data[2]))
    if tag == _ENUM:
        cls = _ENUMS.get(data[0])
        if cls is None:
            raise ValueError(f"enum {data[0]!r} is not one the codec may restore")
        return cls(data[1])
    raise ValueError(f"unknown value tag {tag!r}")


def encode_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Broker rows -> JSON-safe rows. ``TypeError`` (naming the key and the type) for anything that would not come
    back exactly."""
    out = []
    for row in rows:
        if type(row) is not dict:
            raise TypeError(f"cannot store a row of type {type(row).__name__}: rows must be dicts")
        encoded = {}
        for key, value in row.items():
            if type(key) is not str:
                raise TypeError(f"cannot store key {key!r} of type {type(key).__name__}: keys must be str")
            encoded[key] = _encode_value(key, value)
        out.append(encoded)
    return out


def decode_rows(data: Any) -> List[Dict[str, Any]]:
    """The inverse of ``encode_rows``. ``ValueError`` / ``TypeError`` on anything it did not write."""
    if type(data) is not list:
        raise ValueError(f"rows must be a list, not {type(data).__name__}")
    out = []
    for row in data:
        if type(row) is not dict:
            raise ValueError(f"a row must be a dict, not {type(row).__name__}")
        out.append({key: _decode_value(value) for key, value in row.items()})
    return out


_POSITION_FIELDS = tuple(Position.model_fields)


def encode_positions(positions: Iterable[Any]) -> List[Dict[str, Any]]:
    """``Position`` objects -> JSON-safe rows (every model field; the enum fields tagged). Anything that is not a
    ``Position`` is refused: a duck-typed stand-in would not come back as what it was."""
    out = []
    for p in positions:
        if type(p) is not Position:
            raise TypeError(f"cannot store a position of type {type(p).__name__}: only Position objects round-trip")
        out.append({name: _encode_value(f"position.{name}", getattr(p, name), enums=True)
                    for name in _POSITION_FIELDS})
    return out


def decode_positions(data: Any) -> List[Position]:
    rows = decode_rows(data)
    out = []
    for row in rows:
        if set(row) != set(_POSITION_FIELDS):
            raise ValueError(f"position fields {sorted(set(row) ^ set(_POSITION_FIELDS))} do not match the model")
        out.append(Position(**row))
    return out


def _check_utc(fetched_at: datetime) -> datetime:
    if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
        raise ValueError(f"fetched_at must be a tz-aware UTC datetime, not {fetched_at!r}")
    return fetched_at.astimezone(timezone.utc)


def _as_utc(stored: datetime) -> datetime:
    """SQLite hands a DateTime column back naive; every ``fetched_at`` here was written as UTC."""
    return stored.replace(tzinfo=timezone.utc) if stored.tzinfo is None else stored.astimezone(timezone.utc)


# --------------------------------------------------------------------------------------------------------------------
# account snapshots
# --------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class GrowthSnapshot:
    """One account's four growth datasets as last fetched together. ``balance_floor``: None = the broker's default
    (~1 year) balance window was requested, else the start date that was requested (the tab's ``balance_floor``)."""
    account_id: int
    fetched_at: datetime
    dividends: List[Dict[str, Any]]
    trades: List[Dict[str, Any]]
    positions: List[Position]
    balance: List[Dict[str, Any]]
    balance_floor: Optional[date]

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        """Seconds since the fetch, never negative (a clock that stepped back is not "from the future")."""
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.fetched_at).total_seconds())


@dataclass(frozen=True)
class StoredDividends:
    rows: List[Dict[str, Any]]
    fetched_at: datetime

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.fetched_at).total_seconds())


def _upsert(session, account_id: int, dataset: str, payload: Dict[str, Any], fetched_at: datetime) -> None:
    row = session.get(AccountGrowthSnapshot, (int(account_id), dataset))
    if row is None:
        row = AccountGrowthSnapshot(account_id=int(account_id), dataset=dataset, fetched_at=fetched_at,
                                    payload=payload, schema_version=GROWTH_SNAPSHOT_SCHEMA_VERSION)
    else:
        row.fetched_at = fetched_at
        row.payload = payload
        row.schema_version = GROWTH_SNAPSHOT_SCHEMA_VERSION
        row.updated_at = datetime.now(timezone.utc)
    session.add(row)


#: The datasets whose EMPTY answer cannot be told from a failed fetch (the lenient broker getters return ``[]`` on
#: failure). Positions are not here: a flat account is real, and a failed position fetch is ``None``.
EMPTY_MEANS_FAILED = ("dividends", "trades", "balance")


def _stored_has_rows(payload: Any, key: str) -> bool:
    """Whether a stored payload (of ANY schema version: a schema bump must not open the door) holds rows under
    ``key``. Reads the raw JSON, no decoding: an encoded row list has one entry per row."""
    return isinstance(payload, dict) and isinstance(payload.get(key), list) and bool(payload[key])


def save_growth_snapshot(account_id: int, *, dividends, trades, positions, balance,
                         balance_floor: Optional[date], fetched_at: datetime,
                         confirmed_empty: Iterable[str] = ()) -> None:
    """Store the account's ``"growth"`` snapshot AND its ``"dividends"`` dataset (the same dividend rows), in one
    transaction. Everything is encoded before anything is written: a refused value stores nothing.

    AN EMPTY ANSWER NEVER REPLACES STORED ROWS. For ``EMPTY_MEANS_FAILED``, a save whose list is empty while the
    stored snapshot has rows for it is REFUSED (``ValueError`` naming the account and the datasets): the lenient
    broker getters return ``[]`` on a failed fetch, and a page with no previous rows (a first load, a schema bump)
    cannot tell. ``confirmed_empty`` names the datasets the caller KNOWS became empty (the tab's manual refresh after
    an empty answer twice in a row). The ``"dividends"`` dataset row is never written with an empty list at all
    (``save_dividends``' rule): it keeps its previous rows."""
    fetched_at = _check_utc(fetched_at)
    if balance_floor is not None and type(balance_floor) is not date:
        raise TypeError(f"cannot store 'balance_floor' of type {type(balance_floor).__name__}: a date or None")
    confirmed = set(confirmed_empty)
    lists = {"dividends": dividends, "trades": trades, "balance": balance}
    encoded_dividends = encode_rows(dividends)
    payload = {
        "dividends": encoded_dividends,
        "trades": encode_rows(trades),
        "positions": encode_positions(positions),
        "balance": encode_rows(balance),
        "balance_floor": None if balance_floor is None else balance_floor.isoformat(),
    }
    with get_db() as session:
        stored = session.get(AccountGrowthSnapshot, (int(account_id), DATASET_GROWTH))
        stored_payload = stored.payload if stored is not None else None
        emptied = [name for name in EMPTY_MEANS_FAILED
                   if not lists[name] and name not in confirmed and _stored_has_rows(stored_payload, name)]
        if emptied:
            raise ValueError(f"refusing to replace the stored {', '.join(emptied)} of account {account_id} with an "
                             f"empty answer (a failed broker fetch returns none too); the stored snapshot is kept")
        _upsert(session, account_id, DATASET_GROWTH, payload, fetched_at)
        if encoded_dividends:
            _upsert(session, account_id, DATASET_DIVIDENDS, {"rows": encoded_dividends}, fetched_at)
        session.commit()


def _read(account_id: int, dataset: str) -> Optional[Tuple[Any, datetime]]:
    """``(payload, fetched_at)`` of a current-version row, or None (absent / unreadable, the latter logged)."""
    try:
        with get_db() as session:
            row = session.get(AccountGrowthSnapshot, (int(account_id), dataset))
            if row is None:
                return None
            version, payload, fetched_at = row.schema_version, row.payload, row.fetched_at
    except Exception as e:  # noqa: BLE001 -- a stored copy must never break the page: it falls back to the broker
        logger.warning(f"Stored {dataset} snapshot of account {account_id} could not be read ({e}); "
                       f"fetching from the broker instead")
        return None
    if version != GROWTH_SNAPSHOT_SCHEMA_VERSION:
        logger.warning(f"Stored {dataset} snapshot of account {account_id} has schema version {version}, "
                       f"not {GROWTH_SNAPSHOT_SCHEMA_VERSION}; ignored, fetching from the broker instead")
        return None
    return payload, _as_utc(fetched_at)


def load_growth_snapshot(account_id: int) -> Optional[GrowthSnapshot]:
    """The account's ``"growth"`` snapshot, or None (absent, or unreadable: logged at WARNING). Never raises."""
    hit = _read(account_id, DATASET_GROWTH)
    if hit is None:
        return None
    payload, fetched_at = hit
    try:
        if type(payload) is not dict:
            raise ValueError(f"payload is a {type(payload).__name__}, not a dict")
        floor = payload["balance_floor"]
        return GrowthSnapshot(
            account_id=int(account_id), fetched_at=fetched_at,
            dividends=decode_rows(payload["dividends"]), trades=decode_rows(payload["trades"]),
            positions=decode_positions(payload["positions"]), balance=decode_rows(payload["balance"]),
            balance_floor=None if floor is None else date.fromisoformat(floor))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Stored growth snapshot of account {account_id} could not be decoded "
                       f"({type(e).__name__}: {e}); fetching from the broker instead")
        return None


def save_dividends(account_id: int, rows, fetched_at: datetime) -> bool:
    """Store the account's ``"dividends"`` dataset alone (the allocator's fetches); True when written.

    An EMPTY list is never written (returns False, nothing changes): the lenient ``get_dividends`` returns ``[]`` on
    a failed fetch too, so it must neither replace stored rows nor be served as a fresh "pays nothing"."""
    fetched_at = _check_utc(fetched_at)
    if not rows:
        return False
    payload = {"rows": encode_rows(rows)}
    with get_db() as session:
        _upsert(session, account_id, DATASET_DIVIDENDS, payload, fetched_at)
        session.commit()
    return True


def load_dividends(account_id: int) -> Optional[StoredDividends]:
    """The account's stored dividend rows, or None (absent, or unreadable: logged at WARNING). Never raises."""
    hit = _read(account_id, DATASET_DIVIDENDS)
    if hit is None:
        return None
    payload, fetched_at = hit
    try:
        if type(payload) is not dict:
            raise ValueError(f"payload is a {type(payload).__name__}, not a dict")
        return StoredDividends(rows=decode_rows(payload["rows"]), fetched_at=fetched_at)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Stored dividends of account {account_id} could not be decoded "
                       f"({type(e).__name__}: {e}); fetching from the broker instead")
        return None


# --------------------------------------------------------------------------------------------------------------------
# broker share movements (the Overview heatmap's "My P/L" from the broker's own history)
# --------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StoredShareMovements:
    """One account's stored broker share movements (``services.broker_share_history`` decides what they mean).

    ``rows``: the classified transaction rows, each with its broker ``id``. ``covered_from``: the instant from which
    the store PROVABLY holds every share movement (from a complete full read). ``synced_at`` / ``full_read_at``:
    when the last sync / the last FULL read finished."""
    rows: List[Dict[str, Any]]
    covered_from: datetime
    synced_at: datetime
    full_read_at: datetime


#: The share-movement payload's own layout version (inside the payload: the table's schema_version is shared).
#: 4: rows classified by instrument (option assignments / exercises, equity offerings and warrants kept as
#: tile refusals; cash Money Movement rows not kept), no per-symbol baselines; a store of another format is
#: ignored and read again in full.
SHARE_MOVEMENTS_FORMAT = 4


def save_share_movements(account_id: int, store: StoredShareMovements) -> None:
    """Store the account's ``"share_movements"`` dataset (RAISES on a value the codec refuses or a database error;
    the caller counts it as a failed sync). Everything is encoded before anything is written."""
    times = {name: _check_utc(getattr(store, name)) for name in ("covered_from", "synced_at", "full_read_at")}
    payload = {"format": SHARE_MOVEMENTS_FORMAT, "rows": encode_rows(store.rows),
               **{name: value.isoformat() for name, value in times.items()}}
    with get_db() as session:
        _upsert(session, account_id, DATASET_SHARE_MOVEMENTS, payload, times["synced_at"])
        session.commit()


def load_share_movements(account_id: int) -> Optional[StoredShareMovements]:
    """The account's stored share movements, or None (absent, or unreadable / another format: logged at WARNING).
    Never raises."""
    hit = _read(account_id, DATASET_SHARE_MOVEMENTS)
    if hit is None:
        return None
    payload, _fetched_at = hit
    try:
        if type(payload) is not dict or payload.get("format") != SHARE_MOVEMENTS_FORMAT:
            raise ValueError("not a share-movement payload of this format")
        return StoredShareMovements(
            rows=decode_rows(payload["rows"]),
            covered_from=_as_utc(datetime.fromisoformat(payload["covered_from"])),
            synced_at=_as_utc(datetime.fromisoformat(payload["synced_at"])),
            full_read_at=_as_utc(datetime.fromisoformat(payload["full_read_at"])))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Stored share movements of account {account_id} could not be decoded "
                       f"({type(e).__name__}: {e}); reading them from the broker instead")
        return None


# --------------------------------------------------------------------------------------------------------------------
# FMP dividend histories (the forecast's "history" source)
# --------------------------------------------------------------------------------------------------------------------

def load_dividend_histories(symbols: Iterable[str]) -> Dict[str, Tuple[Any, datetime]]:
    """``{SYMBOL: (raw payload, fetched_at UTC)}`` for the symbols that have a stored row. ``{}`` (logged at WARNING)
    when the table cannot be read. Never raises."""
    wanted = sorted({str(s).upper() for s in symbols if s})
    if not wanted:
        return {}
    try:
        with get_db() as session:
            rows = session.exec(select(SymbolDividendHistory).where(SymbolDividendHistory.symbol.in_(wanted))).all()
            return {row.symbol: (row.payload, _as_utc(row.fetched_at)) for row in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Stored dividend histories could not be read ({e}); asking FMP instead")
        return {}


def _check_history_shape(symbol: str, payload: Any) -> None:
    """FMP's ``historical-price-full/stock_dividend`` reply is ``{"symbol": ..., "historical": [...]}``, ``{}`` for a
    symbol that never paid, and some FMP endpoints answer a bare list. Anything else -- an error body served with
    HTTP 200 (``{"Error Message": ...}``, ``{"message": ...}``), a string, ``None`` -- is not "pays nothing" and is
    refused with a ``ValueError``."""
    if isinstance(payload, list):
        return
    if isinstance(payload, dict) and (not payload or isinstance(payload.get("historical"), list)):
        return
    shown = repr(payload)
    raise ValueError(f"FMP's dividend history of {symbol} has an unexpected shape "
                     f"({shown[:120]}{'...' if len(shown) > 120 else ''}); not stored")


def save_dividend_history(symbol: str, payload: Any, fetched_at: datetime) -> bool:
    """Store one symbol's raw FMP payload; True when written. An empty history is stored too (a non-payer is not
    asked again on every cold start), EXCEPT over a stored payload that parses to payments: that is kept (WARNING
    naming the symbol, returns False), because an empty answer is also what a broken reply parses to. A payload of
    any other shape than FMP's (``_check_history_shape``) raises ``ValueError``."""
    from ..ui.utils.dividend_forecast import parse_provider_history

    fetched_at = _check_utc(fetched_at)
    sym = str(symbol).upper()
    _check_history_shape(sym, payload)
    with get_db() as session:
        row = session.get(SymbolDividendHistory, sym)
        if row is not None and not parse_provider_history(payload)[0] and parse_provider_history(row.payload)[0]:
            logger.warning(f"Dividend history of {sym}: FMP now answers no payments, the stored history has some; "
                           f"keeping the stored history")
            return False
        if row is None:
            row = SymbolDividendHistory(symbol=sym, fetched_at=fetched_at, payload=payload)
        else:
            row.fetched_at = fetched_at
            row.payload = payload
        session.add(row)
        session.commit()
    return True


_REFRESH_LOCK = threading.Lock()
_refresh_thread: Optional[threading.Thread] = None


def _refresh_histories(symbols: List[str], fetch: Callable[[str], Any]) -> None:
    for sym in symbols:
        try:
            payload = fetch(sym)
            save_dividend_history(sym, payload, datetime.now(timezone.utc))
        except Exception as e:  # noqa: BLE001 -- the stored copy stays in use; the next stale read tries again
            logger.warning(f"Background refresh of the stored dividend history of {sym} failed ({e}); "
                           f"the stored copy stays in use")


def start_dividend_history_refresh(symbols: Iterable[str], fetch: Callable[[str], Any]) -> Optional[threading.Thread]:
    """Refresh (fetch + store) the given symbols' histories in ONE daemon thread, one such thread per process at a
    time. Returns the started thread, or None when one is already running (its symbols then wait for the next stale
    read)."""
    global _refresh_thread
    todo = [str(s).upper() for s in symbols if s]
    if not todo:
        return None
    with _REFRESH_LOCK:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return None
        thread = threading.Thread(target=_refresh_histories, args=(todo, fetch),
                                  name="dividend-history-refresh", daemon=True)
        _refresh_thread = thread
        thread.start()
        return thread


def join_dividend_history_refresh(timeout: Optional[float] = None) -> None:
    """TEST HOOK (``tests/conftest.py`` and the tests call it; production never does): wait for the running history
    refresh, if any, so no thread doing DB work outlives its test."""
    with _REFRESH_LOCK:
        thread = _refresh_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout)
