"""Family-independent core of the per-action / per-step capture records.

The option record (:mod:`~ba2_common.core.replay.option_action_record`) was the first family:
one ``execute`` = one observation holding the INPUTS that rebuild the shared code, the DB rows it
read, the window of broker reads on the tape (``tape_span``) and the OUTCOME including the effects
the code handed to the account. A stock decision step (rules / sizing / execute) needs exactly the
same mechanics, so everything that does not depend on the family lives here and each family sits on
top of it:

* the thread-local frame (``_tls``) and the :class:`Frame` it holds: the subject being recorded,
  the ordered ``submits`` (effects handed to the account) and the named ``records`` a family stamps;
* :func:`note_effect` / :func:`note_record`: how shared code reports an effect or a record into the
  current frame (a no-op when no frame is open, never raises);
* :class:`FrameRecorder`: the recorder lifecycle (span start, inputs, db rows, frame install and
  restore, outcome, ``record_observation``); a family subclasses it and supplies ``provider``,
  ``version``, ``method_name``, ``identity``, ``build_inputs`` and ``collect_db_rows``;
* :func:`build_outcome`, :func:`replay_effects`: ONE outcome derivation for the live recorder and
  the offline replay, which compares like for like;
* :func:`dump_row`, :func:`is_secret`, :func:`encodable`, :func:`db_row_instances`,
  :func:`collect_expert_rows`: the row dump, the secret filter, the codec guard, the scratch-DB
  loader and the expert-scoped DB read set (instance, non-secret settings, capital-holding
  transactions and their orders);
* :func:`tape_span` and :func:`check_version`: the payload ``tape_span`` block and the version gate.

Outside a capture scope, or while replaying, a family entry point is one ``current_capture()``
check (``NOOP_CONTEXT``). A recording failure goes to ``context.note_failure`` and never changes
the recorded code's result.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from ba2_common.core.replay.context import current_capture
from ba2_common.core.replay.observe import SECRET_KEY_TOKENS, record_observation

__all__ = [
    "Frame",
    "FrameRecorder",
    "NOOP",
    "NOOP_CONTEXT",
    "RESULT_DATA_SKIP",
    "account_declarations",
    "build_outcome",
    "check_version",
    "collect_expert_rows",
    "db_row_instances",
    "dump_row",
    "encodable",
    "encodable_effect",
    "is_secret",
    "note_account_effect",
    "note_effect",
    "note_record",
    "replay_effects",
    "result_fields",
    "sorted_rows",
    "tape_span",
]

_tls = threading.local()


# ---------------------------------------------------------------------------
# disabled recorder
# ---------------------------------------------------------------------------
class _Noop:
    """The handle of a disabled recorder: assignments vanish."""

    __slots__ = ()

    def __setattr__(self, name, value):
        pass


NOOP = _Noop()


class _NoopContext:
    __slots__ = ()

    def __enter__(self):
        return NOOP

    def __exit__(self, exc_type, exc, tb):
        return False


NOOP_CONTEXT = _NoopContext()


# ---------------------------------------------------------------------------
# frame + effect notes
# ---------------------------------------------------------------------------
class Frame:
    """What one recorded span collects: its subject, the effects it submitted, its named records."""

    __slots__ = ("action", "submits", "records", "accepts_account_effects")

    def __init__(self, action, accepts_account_effects: bool = False):
        self.action = action
        #: Whether the account EFFECT taps (``account_taps``) may record into this frame. The option
        #: family says no: its submits are noted at the call sites, and a tapped ``submit_order``
        #: reached from inside ``submit_option_order`` must not add a second, different entry.
        self.accepts_account_effects = accepts_account_effects
        self.submits: List[Dict[str, Any]] = []
        self.records: Dict[str, Any] = {}


def note_effect(method: str, summarise_leg: Optional[Callable[[Any], Dict[str, Any]]] = None,
                **call) -> None:
    """Called where shared code hands something to the account. Never raises.

    ``summarise_leg`` (option family): when the call carries ``legs``, the entry also gets a
    ``leg_summary`` built with it, and ``legs`` is stored as a list.
    """
    frame = getattr(_tls, "frame", None)
    if frame is None:
        return
    try:
        legs = call.get("legs")
        entry = {"method": method}
        entry.update(call)
        if legs is not None:
            entry["legs"] = list(legs)
            if summarise_leg is not None:
                entry["leg_summary"] = [summarise_leg(leg) for leg in legs]
        frame.submits.append(entry)
    except Exception as exc:
        context = current_capture()
        if context is not None:
            context.note_failure("option submit not noted", exc)


def note_account_effect(method: str, **call) -> bool:
    """Called by the account EFFECT taps. Records into the current frame only if it accepts them.

    Returns whether an entry was recorded (False: no frame, or a frame of a family that notes its
    own submits). Never raises.
    """
    frame = getattr(_tls, "frame", None)
    if frame is None or not frame.accepts_account_effects:
        return False
    note_effect(method, **call)
    return True


def note_record(name: str, value: Any) -> None:
    """Called where shared code stamps a named record (``entry_record`` / ``exit_record``)."""
    frame = getattr(_tls, "frame", None)
    if frame is None:
        return
    frame.records[name] = value


@contextmanager
def replay_effects(action, accepts_account_effects: bool = False):
    """Offline replay: collect what one re-run submits and stamps.

    The live recorder is a no-op while replaying (it must never write into the tape it reads), but
    the replay still needs the SAME effect / record notes the recorder collected, in the same
    shape, so it installs the recorder's own frame without recording anything. Restores the
    previous frame on exit, exceptions included.
    """
    frame = Frame(action, accepts_account_effects)
    previous = getattr(_tls, "frame", None)
    _tls.frame = frame
    try:
        yield frame
    finally:
        _tls.frame = previous


# ---------------------------------------------------------------------------
# outcome
# ---------------------------------------------------------------------------
#: Result-data keys that are bulky and NOT a replay comparison input.
RESULT_DATA_SKIP = frozenset({"evaluation_details", "calculation_preview"})


def result_fields(result):
    """``(success, message, data)`` of an action result: the real dict, or an attribute object."""
    if isinstance(result, dict):
        return result["success"], result["message"], result["data"]
    return (getattr(result, "success", None), getattr(result, "message", None),
            getattr(result, "data", None))


def build_outcome(exc, result, frame, encode) -> Dict[str, Any]:
    """The ``outcome`` block of a record, from what one span did.

    ONE derivation for two callers: the live recorder and the offline replay, which builds the
    outcome of the re-run the same way so the two are compared like for like. ``encode(value,
    label)`` is the recorder's codec guard (the replay passes an identity function: it compares the
    live objects).
    """
    outcome: Dict[str, Any] = {
        "status": "error" if exc is not None else "returned",
        "error": repr(exc) if exc is not None else None,
        "success": None, "message": None, "result_data": None,
        "entry_record": None, "exit_record": None,
        "submits": [],
    }
    if exc is None and result is not None:
        outcome["success"], outcome["message"], data = result_fields(result)
        if isinstance(data, dict):
            outcome["entry_record"] = encode(data.get("entry_record"), "entry_record")
            outcome["result_data"] = encode(
                {k: v for k, v in data.items() if k not in RESULT_DATA_SKIP}, "result data")
    if "exit_record" in frame.records:
        outcome["exit_record"] = encode(frame.records["exit_record"], "exit_record")
    if outcome["entry_record"] is None and "entry_record" in frame.records:
        outcome["entry_record"] = encode(frame.records["entry_record"], "entry_record")
    outcome["submits"] = [encodable_effect(entry, encode) for entry in frame.submits]
    return outcome


def encodable_effect(entry: Dict[str, Any], encode) -> Dict[str, Any]:
    """``entry`` if the codec can store it, else ``{"method", "unencodable"}``.

    Effects can carry live objects, so one with an attribute the codec refuses would otherwise
    fail the freeze of the WHOLE record. The marker keeps the method name; the replay reports a
    recorded marker as ``missing_capture`` (it cannot compare what was not stored).
    """
    kept = encode(entry, "submit")
    if kept is entry:
        return entry
    return {"method": entry["method"], "unencodable": kept["unencodable"]}


def encodable(context, value, label, family: str = "option action"):
    """``value`` if the exact codec can store it, else a visible marker (and a counted failure)."""
    if value is None:
        return None
    from ba2_common.core.replay import codec
    try:
        codec.encode(value)
        return value
    except Exception as exc:
        context.note_failure(f"{family} {label} not encodable", exc)
        return {"unencodable": repr(value)[:2000]}


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------
def tape_span(start: Optional[int], clock_start: Optional[int], clock_end: int) -> Dict[str, Any]:
    """The payload ``tape_span``: the half-open observation / clock-read window of one span.

    The span's reads are exactly the observations with ``start <= invocation_seq <`` the record's
    own seq; anything read earlier is outside the window.
    """
    return {"start": start, "clock_start": clock_start, "clock_end": clock_end}


def check_version(payload: Dict[str, Any], readable: Iterable[int]) -> bool:
    """Whether the replay can read ``payload`` (its ``version`` is one of ``readable``)."""
    return payload["version"] in tuple(readable)


def account_declarations(account, names: Sequence[str]) -> Dict[str, Any]:
    """Class-level declarations of the ACCOUNT that shared code reads and a tape account cannot
    learn from a tapped read. A declaration is a string tag or absent."""
    out = {}
    for name in names:
        value = getattr(account, name, None)
        # A test double's auto-attribute is neither a string nor absent.
        out[name] = value if isinstance(value, str) else None
    return out


# ---------------------------------------------------------------------------
# db rows
# ---------------------------------------------------------------------------
def dump_row(obj) -> Dict[str, Any]:
    return {"model": type(obj).__name__, "row": obj.model_dump()}


def is_secret(key: Any) -> bool:
    text = str(key).lower()
    return any(token in text for token in SECRET_KEY_TOKENS)


def sorted_rows(rows: Dict[tuple, Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(rows.items(), key=lambda item: (item[0][0], item[0][1] or 0))
    return [value for _, value in ordered]


def db_row_instances(payload: Dict[str, Any]) -> list:
    """The recorded ``db_rows`` as model instances, ready to ``session.add`` into a scratch DB."""
    from ba2_common.core import models

    instances = []
    for entry in payload["db_rows"]:
        model = getattr(models, entry["model"])
        instances.append(model(**entry["row"]))
    return instances


def collect_expert_rows(add: Callable[[Any], None], expert_id: Optional[int]) -> List[str]:
    """Add the expert-scoped rows every decision reads; returns the REDACTED setting keys.

    ``ExpertInstance`` and its non-secret ``ExpertSetting`` rows (secret-looking keys are dropped
    and named), then THE BOUND: the expert's capital-holding transactions (WAITING / OPENED /
    CLOSING) on ANY symbol, plus all their orders. Nothing in the decision code reads a CLOSED /
    FAILED row, so history is not recorded.
    """
    from sqlmodel import Session, select

    from ba2_common.core.db import get_db
    from ba2_common.core.models import ExpertInstance, ExpertSetting
    from ba2_common.core.trade_store import get_or_none, orders_where, transactions_where
    from ba2_common.core.types import CAPITAL_HOLDING_TRANSACTION_STATUSES

    redacted: List[str] = []
    if expert_id is None:
        return redacted
    add(get_or_none(ExpertInstance, expert_id))
    with Session(get_db().bind) as session:
        for setting in session.exec(
                select(ExpertSetting).where(ExpertSetting.instance_id == expert_id)).all():
            if is_secret(setting.key):
                redacted.append(setting.key)
            else:
                add(setting)
    transactions = list(transactions_where(
        expert_id=expert_id, statuses=list(CAPITAL_HOLDING_TRANSACTION_STATUSES)))
    ids = []
    for transaction in transactions:
        add(transaction)
        if transaction.id not in ids:
            ids.append(transaction.id)
    if ids:
        for held in orders_where(transaction_ids=ids):
            add(held)
    return redacted


# ---------------------------------------------------------------------------
# recorder lifecycle
# ---------------------------------------------------------------------------
class FrameRecorder:
    """The live recorder of one span (only built inside a capture scope, never in replay).

    A family subclasses it:

    * ``provider`` / ``version`` / ``family`` (the label used in failure messages);
    * ``method_name()`` and ``identity()`` for the observation;
    * ``build_inputs()`` and ``collect_db_rows()`` (-> ``(rows, redacted keys)``), both called on
      enter, after the span start has been read.

    ``__enter__`` returns the recorder (the caller sets ``.result``) or ``NOOP`` when the same
    subject is already being recorded (re-entry is one record) or the inputs could not be built.
    """

    provider: str = ""
    version: int = 0
    family: str = "action"
    #: True for a family whose frame collects the five account effect calls (``account_taps``).
    accepts_account_effects: bool = False

    def __init__(self, action, context):
        self.action = action
        self.context = context
        self.result: Any = None
        self._inner = False
        self._active = False
        self._previous = None
        self._frame: Optional[Frame] = None
        self._inputs: Optional[Dict[str, Any]] = None
        self._db_rows: Optional[List[Dict[str, Any]]] = None
        self._redacted: List[str] = []
        self._tape_start: Optional[int] = None
        self._clock_start: Optional[int] = None

    # --- family hooks ------------------------------------------------------
    def method_name(self) -> str:
        raise NotImplementedError

    def identity(self) -> Dict[str, Any]:
        raise NotImplementedError

    def build_inputs(self) -> Dict[str, Any]:
        raise NotImplementedError

    def collect_db_rows(self):
        raise NotImplementedError

    # --- lifecycle ---------------------------------------------------------
    def __enter__(self):
        self._tape_start = self.context.observation_position
        self._clock_start = self.context.clock_read_position    # BEFORE the decision-time read
        current = getattr(_tls, "frame", None)
        if current is not None and current.action is self.action:
            self._inner = True              # re-entry for the same subject: one record
            return NOOP
        try:
            self._inputs = self.build_inputs()
            self._db_rows, self._redacted = self.collect_db_rows()
        except Exception as exc:
            self.context.note_failure(f"{self.family} inputs not recorded", exc)
            return NOOP
        self._previous = current
        self._frame = Frame(self.action, self.accepts_account_effects)
        _tls.frame = self._frame
        self._active = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._inner:
            return False
        if self._active:
            _tls.frame = self._previous
            try:
                self._record(exc)
            except Exception as record_exc:      # never change the recorded code's outcome
                self.context.note_failure(f"{self.family} outcome not recorded", record_exc)
        return False

    def _record(self, exc):
        outcome = build_outcome(
            exc, self.result, self._frame,
            lambda value, label: encodable(self.context, value, label, self.family))
        inputs = dict(self._inputs)
        inputs["redacted_setting_keys"] = self._redacted
        payload = {
            "version": self.version,
            "tape_span": tape_span(self._tape_start, self._clock_start,
                                   self.context.clock_read_position),
            "inputs": inputs,
            "db_rows": self._db_rows,
            "outcome": outcome,
        }
        self.emit(provider=self.provider, method=self.method_name(),
                  identity=self.identity(), payload=payload)

    def emit(self, **observation):
        """Write the observation (a family may route it through its own module-level name)."""
        record_observation(**observation)
