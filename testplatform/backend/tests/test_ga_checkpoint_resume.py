"""GA generation-level checkpointing: it must actually persist, and only resume where valid.

Written after a 4h40m loss. Checkpointing LOOKED wired -- checkpoint_cb was passed to
optimizer.optimize and _load_checkpoint was consulted on every run -- but no CLI job ever had a
TaskQueue row, and _save_checkpoint's `if t:` made every save a silent no-op. So the first test
here is the one that matters: save then load, with NO row pre-created, exactly as a CLI run.
"""
import pytest

from app.models import SessionLocal, TaskQueue
from app.models.database import Base, engine
from app.models.task_queue import TaskStatus
from app.services import strategy_optimization_handler as H


@pytest.fixture(autouse=True, scope="module")
def _schema():
    """These tests hit a real TaskQueue row, so the table has to exist on the default engine
    (same approach as tests/backtest/fixtures/e2e_support.ensure_host_schema)."""
    Base.metadata.create_all(bind=engine)


def _row(task_id):
    db = SessionLocal()
    try:
        return db.query(TaskQueue).filter(TaskQueue.task_id == task_id).first()
    finally:
        db.close()


@pytest.fixture
def task_id():
    tid = H.checkpoint_task_id("unit-test-job", 1)
    yield tid
    db = SessionLocal()
    try:
        db.query(TaskQueue).filter(TaskQueue.task_id == tid).delete()
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------------------------
# The regression that started this
# --------------------------------------------------------------------------------------------

def test_save_creates_the_row_when_absent(task_id):
    """The CLI path has no TaskQueue row. Saving MUST create one, not silently do nothing."""
    assert _row(task_id) is None, "precondition: no row yet (this is the CLI situation)"

    H._save_checkpoint(task_id, {"generation": 3, "population": [[1, 2]], "fingerprint": "abc"})

    t = _row(task_id)
    assert t is not None, "checkpoint save was a no-op — the original bug"
    assert t.checkpoint_data["generation"] == 3


def test_round_trip_without_a_pre_existing_row(task_id):
    H._save_checkpoint(task_id, {"generation": 5, "population": [[7]], "fingerprint": "fp1"})
    ckpt = H._load_checkpoint(task_id, "fp1")
    assert ckpt is not None and ckpt["generation"] == 5


def test_holder_row_is_running_not_pending(task_id):
    """A PENDING/QUEUED row is claimable by the queue worker, which would re-dispatch a job that
    is already running here. Only QUEUED is actually claimed, but RUNNING keeps it clearly out."""
    H._save_checkpoint(task_id, {"generation": 1, "population": []})
    assert _row(task_id).status == TaskStatus.RUNNING.value


# --------------------------------------------------------------------------------------------
# Keying: stable across re-runs, because a relaunch gets a NEW optimization row
# --------------------------------------------------------------------------------------------

def test_key_is_stable_across_optimization_ids():
    """The aborted goal2020 job was opt 248 and its relaunch 249. An id-keyed checkpoint could
    never be found by the run that needs it."""
    assert H.checkpoint_task_id("scr-large-FMPRating-S1", 248) == \
           H.checkpoint_task_id("scr-large-FMPRating-S1", 249)


def test_key_differs_between_jobs():
    assert H.checkpoint_task_id("job-a", 1) != H.checkpoint_task_id("job-b", 1)


def test_key_fits_the_task_id_column():
    """TaskQueue.task_id is String(50); real job names are longer than that on their own."""
    long_name = "scr-large-FMPRating-S1-goal2020-riskatr-from2022"
    assert len(long_name) > 40
    assert len(H.checkpoint_task_id(long_name, 999)) <= 50


def test_key_falls_back_when_the_job_is_unnamed():
    assert H.checkpoint_task_id(None, 42) == H.checkpoint_task_id(None, 42)
    assert H.checkpoint_task_id(None, 42) != H.checkpoint_task_id(None, 43)


# --------------------------------------------------------------------------------------------
# Fingerprint: a checkpoint is meaningless against a different gene space
# --------------------------------------------------------------------------------------------

_GA = {"populationSize": 40, "generations": 8}


def _space(**genes):
    return {k: {"min": 0, "max": 1, "step": 1, "type": "int"} for k in genes} or {}


def test_fingerprint_changes_when_a_gene_is_added():
    """Adding the 4 regime genes to _RM_OPT changed every chromosome's LENGTH. Restoring an older
    population into the new space would misread each locus — silent corruption, not a crash."""
    before = H.checkpoint_fingerprint(_space(a=1, b=1), _GA)
    after = H.checkpoint_fingerprint(_space(a=1, b=1, regime_tp_scale=1), _GA)
    assert before != after


def test_fingerprint_changes_when_gene_order_changes():
    """encode_params maps genes BY POSITION, so order is part of the space's identity."""
    import collections
    ab = collections.OrderedDict([("a", {"min": 0}), ("b", {"min": 0})])
    ba = collections.OrderedDict([("b", {"min": 0}), ("a", {"min": 0})])
    assert H.checkpoint_fingerprint(ab, _GA) != H.checkpoint_fingerprint(ba, _GA)


def test_fingerprint_changes_when_a_range_changes():
    a = {"x": {"min": 0.5, "max": 2.0, "step": 0.25, "type": "float"}}
    b = {"x": {"min": 0.5, "max": 3.0, "step": 0.25, "type": "float"}}
    assert H.checkpoint_fingerprint(a, _GA) != H.checkpoint_fingerprint(b, _GA)


def test_fingerprint_changes_with_population_size():
    assert H.checkpoint_fingerprint(_space(a=1), {"populationSize": 40, "generations": 8}) != \
           H.checkpoint_fingerprint(_space(a=1), {"populationSize": 80, "generations": 8})


def test_fingerprint_is_stable_for_an_identical_space():
    assert H.checkpoint_fingerprint(_space(a=1, b=1), _GA) == \
           H.checkpoint_fingerprint(_space(a=1, b=1), _GA)


def test_mismatched_fingerprint_is_refused(task_id):
    H._save_checkpoint(task_id, {"generation": 5, "population": [[1]], "fingerprint": "OLD"})
    assert H._load_checkpoint(task_id, "NEW") is None, "resumed into a changed search space"
    assert H._load_checkpoint(task_id, "OLD") is not None


def test_checkpoint_without_a_fingerprint_still_loads(task_id):
    """Checkpoints written before the fingerprint existed must keep working."""
    H._save_checkpoint(task_id, {"generation": 2, "population": [[1]]})
    assert H._load_checkpoint(task_id, "anything") is not None


def test_no_fingerprint_requested_skips_the_check(task_id):
    H._save_checkpoint(task_id, {"generation": 2, "population": [[1]], "fingerprint": "X"})
    assert H._load_checkpoint(task_id) is not None


# --------------------------------------------------------------------------------------------
# Clearing on completion
# --------------------------------------------------------------------------------------------

def test_clear_drops_the_checkpoint_and_retires_the_row(task_id):
    """A finished search must not resume from its own final generation on a later re-run."""
    H._save_checkpoint(task_id, {"generation": 8, "population": [[1]]})
    H._clear_checkpoint(task_id)

    assert H._load_checkpoint(task_id) is None
    assert _row(task_id).status == TaskStatus.COMPLETED.value


def test_clear_is_safe_when_nothing_was_saved(task_id):
    H._clear_checkpoint(task_id)          # must not raise
    assert H._load_checkpoint(task_id) is None


def test_load_returns_none_for_an_unknown_key():
    assert H._load_checkpoint(H.checkpoint_task_id("never-saved", 1)) is None


# NOTE: the EXHAUSTED-checkpoint guard (a checkpoint at/past the final generation is discarded
# rather than resumed into a 0-generation run) lives inline in handle_strategy_optimization and is
# covered by tests/test_strategy_optimization_handler.py's
# test_generation_sync_pushes_optimization_each_generation and
# test_completion_pushes_final_completed_state -- both seed an optimization named "opt-run", so
# the second inherits the first's checkpoint by name and fails with "0 successful trials" if the
# guard is removed. Verified by deleting the guard: both go red.


# --------------------------------------------------------------------------------------------
# The saved payload is what GeneticOptimizer needs back
# --------------------------------------------------------------------------------------------

def test_saved_payload_survives_json_and_resumes(task_id):
    """checkpoint_data is a JSON column: the RNG state must round-trip through it, or a resumed
    run silently diverges from the one it is supposed to continue."""
    pytest.importorskip("deap")
    from app.services.genetic import GeneticOptimizer

    space = {"a": {"min": 0, "max": 10, "step": 1, "type": "int"},
             "b": {"min": 0.0, "max": 1.0, "step": 0.1, "type": "float"}}
    opt = GeneticOptimizer(param_ranges=space, population_size=6, n_generations=4,
                           crossover_prob=0.5, mutation_prob=0.2,
                           early_stopping_generations=99, elitism_percent=10.0)
    pop = [opt.toolbox.individual() for _ in range(6)]
    data = opt.get_checkpoint_data(2, pop)
    data["fingerprint"] = H.checkpoint_fingerprint(space, {"populationSize": 6, "generations": 4})

    H._save_checkpoint(task_id, data)
    loaded = H._load_checkpoint(task_id, data["fingerprint"])
    assert loaded is not None

    start_gen, population = opt.resume_from_checkpoint(loaded)
    assert start_gen == 3                      # resumes AFTER the checkpointed generation
    assert len(population) == 6
    assert all(len(ind) == len(pop[0]) for ind in population)
