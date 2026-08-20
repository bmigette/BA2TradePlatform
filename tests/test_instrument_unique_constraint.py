"""`instrument.name` uniqueness must be enforced by the schema, not by convention.

The helpers normalise symbols, but nothing stops a new code path from inserting a
second row for a symbol -- which is exactly how production accumulated its
duplicate groups. The database has to refuse it.

Once it does refuse it, three call sites that used to get away with a duplicate row
have to cope with an ``IntegrityError`` instead:
  * the Settings > Instruments add/edit dialog, where a whitespace-only name
    normalises to ``''`` (the first such row inserts, the second explodes in the UI),
  * ``JobManager.submit_market_analysis``, which would otherwise submit an analysis
    for the blank symbol it just declined to create,
  * the two select-then-insert writers (``JobManager.ensure_instrument_exists`` and
    ``InstrumentAutoAdder._add_instrument_if_missing``), which race.
All four are covered here, because the constraint and the code that has to survive
it land together.
"""
import asyncio
import contextlib
import threading

import pytest
from sqlalchemy import inspect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex
from sqlmodel import SQLModel, select

from ba2_trade_platform.core.db import add_instance, get_db
from ba2_trade_platform.core.models import Instrument
from ba2_trade_platform.core.types import InstrumentType


def _names():
    with get_db() as session:
        return sorted(i.name for i in session.exec(select(Instrument)).all())


# ---------------------------------------------------------------------------
# The constraint itself
# ---------------------------------------------------------------------------

def test_inserting_a_second_instrument_with_the_same_name_is_rejected():
    add_instance(Instrument(name='DUPX', labels=[]))
    with pytest.raises(IntegrityError):
        add_instance(Instrument(name='DUPX', labels=[]))


def test_create_all_emits_a_unique_index_named_ix_instrument_name(test_engine):
    """The name must match what the Alembic migration creates, or a migrated DB
    and a fresh DB disagree forever."""
    indexes = inspect(test_engine).get_indexes('instrument')
    unique_on_name = [ix['name'] for ix in indexes
                      if ix['column_names'] == ['name'] and ix['unique']]
    assert unique_on_name == ['ix_instrument_name']


def test_the_model_normalises_the_name_so_no_writer_can_skip_it():
    """The index is BINARY: it happily holds 'AAPL' and 'aapl' side by side.

    Uniqueness is therefore only as real as every writer's memory to call
    normalize_symbol first. Normalising on the model closes that: a new call site
    that forgets cannot reintroduce the lowercase duplicate groups Section A exists
    to eliminate.
    """
    add_instance(Instrument(name='  aapl ', labels=[]))
    assert _names() == ['AAPL']


def test_assignment_normalises_too_not_just_construction():
    add_instance(Instrument(name='AAPL', labels=[]))
    with get_db() as session:
        instrument = session.exec(select(Instrument).where(Instrument.name == 'AAPL')).first()
        instrument.name = '  msft '
        session.add(instrument)
        session.commit()
    assert _names() == ['MSFT']


def test_a_writer_that_forgets_to_normalise_collides_instead_of_duplicating():
    add_instance(Instrument(name='AAPL', labels=[]))
    with pytest.raises(IntegrityError):
        add_instance(Instrument(name=' aapl ', labels=[]))


def test_a_none_name_is_still_rejected_loudly_by_the_not_null_column():
    """`None` is deliberately NOT normalised to ''.

    normalize_symbol maps None to '' by design, but silently storing a nameless
    row (the first one would be accepted; only the second would collide) is worse
    than the NOT NULL failure the column already gives.
    """
    with pytest.raises(IntegrityError):
        add_instance(Instrument(name=None, labels=[]))


def test_create_all_ddl_is_byte_identical_to_the_migrations_ddl():
    """Pin the emitted SQL, not just the presence of an index.

    Alembic revision f1a7c2e9b4d0 runs
    ``CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)``; a fresh
    ``create_all`` database must produce exactly that statement, or the two
    provisioning paths drift apart.
    """
    index = next(ix for ix in Instrument.__table__.indexes if ix.name == 'ix_instrument_name')
    ddl = str(CreateIndex(index).compile(dialect=sqlite_dialect.dialect())).strip()
    assert ddl == 'CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)'


# ---------------------------------------------------------------------------
# Settings > Instruments dialog: a blank name must never reach the database
# ---------------------------------------------------------------------------

class _FakeElement:
    """Stand-in for a NiceGUI input/select: holds a value, chains like a widget."""

    def __init__(self, **kwargs):
        self.value = kwargs.get('value')

    def classes(self, *args, **kwargs):
        return self

    def props(self, *args, **kwargs):
        return self


class _FakeDialog:
    def __init__(self):
        self.open_count = 0
        self.close_count = 0

    def clear(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def open(self):
        self.open_count += 1

    def close(self):
        self.close_count += 1


class _FakeUI:
    """The handful of ``ui.*`` calls ``add_instrument_dialog`` makes.

    The save handler is a closure inside the dialog and cannot be reached any other
    way, so the widgets are faked and the REAL closure is then invoked.
    """

    def __init__(self):
        self.inputs = {}
        self.buttons = {}
        self.notifications = []
        self.dialogs = []

    def dialog(self):
        d = _FakeDialog()
        self.dialogs.append(d)
        return d

    def card(self):
        return contextlib.nullcontext()

    def input(self, **kwargs):
        element = _FakeElement(**kwargs)
        self.inputs[kwargs['label']] = element
        return element

    def select(self, **kwargs):
        return _FakeElement(**kwargs)

    def button(self, label, on_click=None):
        self.buttons[label] = on_click
        return _FakeElement()

    def notify(self, message, type=None, **kwargs):
        self.notifications.append((message, type))


def _open_instrument_dialog(monkeypatch, instrument=None):
    """Render the add/edit dialog against ``_FakeUI`` and return (fake_ui, save)."""
    import ba2_trade_platform.ui.pages.settings as settings_page

    fake_ui = _FakeUI()
    monkeypatch.setattr(settings_page, 'ui', fake_ui)
    tab = settings_page.InstrumentSettingsTab.__new__(settings_page.InstrumentSettingsTab)
    tab._update_table_rows = lambda: None
    tab.add_instrument_dialog(instrument)
    return fake_ui, fake_ui.buttons['Save']


def test_dialog_refuses_to_save_an_instrument_whose_name_is_only_whitespace(monkeypatch):
    fake_ui, save = _open_instrument_dialog(monkeypatch)
    fake_ui.inputs['Instrument Name'].value = '   '

    save()

    assert _names() == []
    assert [t for _, t in fake_ui.notifications] == ['negative']
    assert fake_ui.dialogs[-1].close_count == 0, 'the dialog must stay open so the name can be fixed'


def test_dialog_refuses_to_rename_an_existing_instrument_to_a_blank_name(monkeypatch):
    """The guard sits above the ``is_edit`` branch, so it covers editing too."""
    add_instance(Instrument(name='AAPL', instrument_type=InstrumentType.STOCK, labels=[]))
    with get_db() as session:
        existing = session.exec(select(Instrument).where(Instrument.name == 'AAPL')).first()

    fake_ui, save = _open_instrument_dialog(monkeypatch, instrument=existing)
    fake_ui.inputs['Instrument Name'].value = ''

    save()

    assert _names() == ['AAPL']
    assert [t for _, t in fake_ui.notifications] == ['negative']
    assert fake_ui.dialogs[-1].close_count == 0


def test_dialog_still_saves_a_valid_name_normalised(monkeypatch):
    """Positive control: the guard must not block real input."""
    fake_ui, save = _open_instrument_dialog(monkeypatch)
    fake_ui.inputs['Instrument Name'].value = '  aapl '

    save()

    assert _names() == ['AAPL']
    assert [t for _, t in fake_ui.notifications] == ['positive']
    assert fake_ui.dialogs[-1].close_count == 1


# ---------------------------------------------------------------------------
# submit_market_analysis: a blank symbol creates nothing, so analyse nothing
# ---------------------------------------------------------------------------

def test_submit_market_analysis_refuses_a_blank_symbol(monkeypatch, mock_expert_instance):
    import ba2_trade_platform.core.JobManager as jm_module

    submitted = []

    class _FakeQueue:
        def submit_analysis_task(self, **kwargs):
            submitted.append(kwargs)
            return 'task-1'

    monkeypatch.setattr(jm_module, 'get_worker_queue', lambda: _FakeQueue())
    manager = jm_module.JobManager.__new__(jm_module.JobManager)

    with pytest.raises(ValueError):
        manager.submit_market_analysis(mock_expert_instance.id, '   ')

    assert submitted == [], 'no analysis task may be queued for a symbol that has no instrument'
    assert _names() == []


def test_submit_market_analysis_still_accepts_a_real_symbol(monkeypatch, mock_expert_instance):
    """Positive control: the blank guard must not reject ordinary symbols."""
    import ba2_trade_platform.core.JobManager as jm_module

    submitted = []

    class _FakeQueue:
        def submit_analysis_task(self, **kwargs):
            submitted.append(kwargs)
            return 'task-1'

    monkeypatch.setattr(jm_module, 'get_worker_queue', lambda: _FakeQueue())
    manager = jm_module.JobManager.__new__(jm_module.JobManager)

    assert manager.submit_market_analysis(mock_expert_instance.id, ' tsla ') == 'task-1'
    assert submitted[0]['symbol'] == 'TSLA'
    assert _names() == ['TSLA']


# ---------------------------------------------------------------------------
# The select-then-insert race the unique index creates
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _shared_file_db(tmp_path):
    """Point the DB helpers at a real on-disk SQLite file for the duration.

    The session-scoped test engine is ``sqlite:///:memory:`` on a
    ``SingletonThreadPool``, which hands every THREAD its own empty database -- a
    threaded race is unobservable on it. ``_build_engine`` is the production
    builder (WAL, 30s busy timeout, one pooled connection per thread), so the two
    threads below share one database with the real unique index.
    """
    import ba2_common.core.db as pkg_db

    engine = pkg_db._build_engine(str(tmp_path / 'race.db'))
    SQLModel.metadata.create_all(engine)
    saved = pkg_db._engine
    pkg_db._engine = engine
    try:
        yield engine
    finally:
        pkg_db._engine = saved
        engine.dispose()


class _BarrierSession:
    """Session proxy that parks its thread once, right after a query returns."""

    def __init__(self, session, park_once):
        self._session = session
        self._park_once = park_once

    def __enter__(self):
        self._session.__enter__()
        return self

    def __exit__(self, *exc):
        return self._session.__exit__(*exc)

    def exec(self, *args, **kwargs):
        result = self._session.exec(*args, **kwargs)
        self._park_once()
        return result

    def __getattr__(self, name):
        return getattr(self._session, name)


def _barriered_get_db(real_get_db, barrier):
    """A ``get_db`` that holds each thread at the barrier after its FIRST query.

    Both threads therefore leave their "does this symbol exist?" SELECT having
    missed, and both go on to INSERT -- the select-then-insert window, forced open
    deterministically instead of hoped for. Only the first query per thread parks,
    so the re-select after an IntegrityError does not deadlock.
    """
    state = threading.local()

    def park_once():
        if not getattr(state, 'parked', False):
            state.parked = True
            barrier.wait()

    return lambda: _BarrierSession(real_get_db(), park_once)


class _LoggerSpy:
    """Wrap a module logger so what it says becomes assertable.

    Errors, because ``_add_instrument_if_missing`` swallows its exceptions and only
    logs them; infos, because "one row survived" would also be true if the barrier
    failed to interleave the two threads and no race ever happened -- the
    concurrency message is the proof that the IntegrityError branch really ran.
    """

    def __init__(self, real):
        self._real = real
        self.errors = []
        self.infos = []

    def error(self, message, *args, **kwargs):
        self.errors.append(message)
        self._real.error(message, *args, **kwargs)

    def info(self, message, *args, **kwargs):
        self.infos.append(message)
        self._real.info(message, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_two_threads_calling_ensure_instrument_exists_create_one_row(tmp_path, monkeypatch):
    import ba2_trade_platform.core.JobManager as jm_module

    with _shared_file_db(tmp_path):
        barrier = threading.Barrier(2, timeout=30)
        monkeypatch.setattr(jm_module, 'get_db', _barriered_get_db(jm_module.get_db, barrier))
        spy = _LoggerSpy(jm_module.logger)
        monkeypatch.setattr(jm_module, 'logger', spy)

        failures = []

        def go():
            try:
                jm_module.ensure_instrument_exists('RACE')
            except Exception as exc:  # noqa: BLE001 -- the point of the test
                failures.append(exc)

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert failures == []
        assert _names() == ['RACE']
        assert [m for m in spy.infos if 'created concurrently' in m], \
            'the loser must have gone through the IntegrityError branch'


def _run_auto_add_race(tmp_path, monkeypatch):
    """Two threads auto-add RACE at once under DIFFERENT expert labels.

    Returns (auto-adder log spy, db log spy, the surviving row's labels). Different
    labels per thread are what makes label adoption observable: whichever thread
    loses, its label is only on the surviving row if the loser carried it over.
    """
    import ba2_common.core.db as pkg_db
    import ba2_trade_platform.core.InstrumentAutoAdder as auto_adder_module

    barrier = threading.Barrier(2, timeout=30)
    monkeypatch.setattr(auto_adder_module, 'get_db',
                        _barriered_get_db(auto_adder_module.get_db, barrier))
    spy = _LoggerSpy(auto_adder_module.logger)
    monkeypatch.setattr(auto_adder_module, 'logger', spy)
    # The DB helpers log on their own logger, which is where a lost race used to
    # print an ERROR + traceback from @retry_on_lock.
    db_spy = _LoggerSpy(pkg_db.logger)
    monkeypatch.setattr(pkg_db, 'logger', db_spy)

    async def fake_fetch(symbol):
        return {'name': symbol, 'category': 'Technology', 'company_name': 'Fake Corp'}

    def go(expert_shortname, extra_labels):
        adder = auto_adder_module.InstrumentAutoAdder()
        adder._fetch_instrument_data = fake_fetch
        asyncio.run(adder._add_instrument_if_missing('RACE', expert_shortname, 'expert', extra_labels))

    threads = [
        threading.Thread(target=go, args=('expert-1', [])),
        threading.Thread(target=go, args=('expert-2', ['extra-b'])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    with get_db() as session:
        row = session.exec(select(Instrument).where(Instrument.name == 'RACE')).first()
        labels = list(row.labels or []) if row else None
    return spy, db_spy, labels


def test_two_threads_auto_adding_the_same_symbol_create_one_row(tmp_path, monkeypatch):
    with _shared_file_db(tmp_path):
        spy, _db_spy, _labels = _run_auto_add_race(tmp_path, monkeypatch)

        # _add_instrument_if_missing swallows and logs its exceptions, so "no
        # exception" is asserted against the log, not against the thread.
        assert spy.errors == []
        assert _names() == ['RACE']
        assert [m for m in spy.infos if 'added concurrently' in m], \
            'the loser must have gone through the IntegrityError branch'


def test_the_loser_of_an_auto_add_race_carries_its_labels_to_the_surviving_row(tmp_path, monkeypatch):
    """Losing the race must not cost the loser's labels.

    Before the unique index the loser inserted its own row and its labels lived on
    there until the merge united them. Now there is one row, and nothing else will
    ever add them: the `existing` branch appends to `Instrument.labels` IN PLACE,
    and that is a plain JSON column with no MutableList wrapper, so the change is
    not tracked and never persists. There is no self-healing pass.
    """
    with _shared_file_db(tmp_path):
        _spy, _db_spy, labels = _run_auto_add_race(tmp_path, monkeypatch)

        assert labels is not None
        assert {'expert-1', 'expert-2', 'extra-b'} <= set(labels), \
            f'the losing thread dropped its labels: {labels}'


def test_a_lost_auto_add_race_logs_no_error_anywhere(tmp_path, monkeypatch):
    """A benign lost race must not print an ERROR with a traceback.

    `add_instance` is wrapped by @retry_on_lock, which logs any non-lock exception
    at ERROR with exc_info before re-raising -- so routing the insert through it
    made the handled, expected case emit a UNIQUE-constraint traceback for
    operators to chase, directly above the INFO saying everything is fine.
    """
    with _shared_file_db(tmp_path):
        spy, db_spy, _labels = _run_auto_add_race(tmp_path, monkeypatch)

        assert spy.errors == []
        assert db_spy.errors == [], 'the DB layer logged an error for a handled race'
