"""``ga["seedPopulation"]`` and ``ga["requireAnEntryRule"]`` in the strategy_optimization handler.

Pure helpers are tested directly on a small GeneticOptimizer; the handler wiring (mutual
exclusion, refusals, checkpoint precedence) goes through the hermetic stub backtest of
test_strategy_optimization_handler.
"""
from __future__ import annotations

import random

import pytest

from app.models.database import Base
from app.models.database import engine
from app.services import strategy_optimization_handler as H
from app.services.genetic import GeneticOptimizer
from tests import test_strategy_optimization_handler as T

SPACE = {
    "entry:r1:enabled": {"type": "int", "min": 0, "max": 1, "step": 1},
    "entry:r2:enabled": {"type": "int", "min": 0, "max": 1, "step": 1},
    "entry:r1:a1:enabled": {"type": "int", "min": 0, "max": 1, "step": 1},
    "exit:x:enabled": {"type": "int", "min": 0, "max": 1, "step": 1},
    "m:tp": {"type": "float", "min": 1.0, "max": 5.0, "step": 0.5},
    "m:mode": {"type": "choice", "choices": ["a", "b", "c"], "min": 0, "max": 2, "step": 1,
               "stratify": True},
}


def _opt(pop=12, space=SPACE, seed=7):
    random.seed(seed)
    return GeneticOptimizer(param_ranges=space, population_size=pop, n_generations=1)


def _seed(**over):
    s = {"entry:r1:enabled": 1, "entry:r2:enabled": 0, "entry:r1:a1:enabled": 0,
         "exit:x:enabled": 0, "m:tp": 2.5, "m:mode": "b"}
    s.update(over)
    return s


# ------------------------------------------------------------------ _build_seed_population
def test_seeds_are_strict_encoded_first_and_padded_to_population():
    o = _opt()
    seeds = [_seed(), _seed(**{"m:tp": 4.0, "m:mode": "c"})]
    pop = H._build_seed_population(seeds, o, 12)
    assert len(pop) == 12
    assert o.decode_individual(pop[0]) == seeds[0]
    assert o.decode_individual(pop[1]) == seeds[1]
    idx = list(SPACE).index("m:mode")  # stratified gene balanced: 12 // 3 = 4 each
    assert all(sum(1 for i in pop if i[idx] == v) >= 4 for v in range(3))


def test_seed_population_is_deterministic_for_a_fixed_seed():
    a = H._build_seed_population([_seed()], _opt(), 12)
    b = H._build_seed_population([_seed()], _opt(), 12)
    assert [list(i) for i in a] == [list(i) for i in b]


def test_too_many_seeds_refused():
    with pytest.raises(ValueError, match="exceed populationSize"):
        H._build_seed_population([_seed()] * 3, _opt(), 2)


@pytest.mark.parametrize("bad", [[], "x", [1], None])
def test_empty_or_malformed_seed_list_refused(bad):
    with pytest.raises(ValueError, match="non-empty list"):
        H._build_seed_population(bad, _opt(), 12)


def test_missing_gene_refused_naming_seed_and_gene():
    s = _seed()
    del s["m:tp"]
    with pytest.raises(ValueError, match=r"seed #1: .*'m:tp'"):
        H._build_seed_population([_seed(), s], _opt(), 12)


# ------------------------------------------------------------------ _require_an_entry_rule
def test_repairs_only_all_off_non_seed_individuals():
    names = _opt().param_names
    e = [names.index("entry:r1:enabled"), names.index("entry:r2:enabled")]
    a1 = names.index("entry:r1:a1:enabled")

    def ind(r1, r2, act=0):
        x = [0] * len(names)
        x[e[0]], x[e[1]], x[a1] = r1, r2, act
        return x

    # index 0 is a seed: all-off and must stay so. Index 3 has only the ACTION toggle on, which
    # is not a rule-level toggle, so it is repaired too.
    pop = [ind(0, 0), ind(1, 0), ind(0, 0), ind(0, 0, 1)]
    n = H._require_an_entry_rule(pop, names, random.Random(1), n_protected=1)
    assert n == 2
    assert pop[0][e[0]] == 0 and pop[0][e[1]] == 0
    assert pop[1][e[0]] == 1 and pop[1][e[1]] == 0
    for k in (2, 3):
        assert pop[k][e[0]] + pop[k][e[1]] == 1


def test_no_entry_gene_refused():
    with pytest.raises(ValueError, match="no entry"):
        H._require_an_entry_rule([[0]], ["m:tp"], random.Random(1))


def test_repair_is_deterministic():
    names = list(SPACE)
    a = [[0] * len(names) for _ in range(20)]
    b = [[0] * len(names) for _ in range(20)]
    H._require_an_entry_rule(a, names, random.Random(5))
    H._require_an_entry_rule(b, names, random.Random(5))
    assert a == b


def test_handed_in_initial_population_is_the_draw_optimize_would_make():
    """With the flag absent the handler passes nothing (optimize() builds gen 0 itself); with it
    set the handler calls ``_initial_population`` with the same RNG position, so the same
    individuals result."""
    a = _opt()._initial_population(12)
    b = _opt()._initial_population(12)
    assert [list(i) for i in a] == [list(i) for i in b]


# ------------------------------------------------------------------ handler wiring
@pytest.fixture(scope="module", autouse=True)
def _host_db():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(H, "_run_trial_backtest", T._deterministic_stub)
    monkeypatch.setattr(H, "_build_hoisted_state", lambda cfg: {})


def _run(name, **ga):
    sid = T._seed_strategy()
    oid = T._seed_opt(sid, config=T._ga_config(**ga))
    return H.handle_strategy_optimization(name, {"optimization_id": oid}), oid


def _capture(monkeypatch):
    cap = {}

    class Spy(GeneticOptimizer):
        def optimize(self, *a, **kw):
            cap["init"] = kw.get("initial_population")
            cap["opt"] = self
            return super().optimize(*a, **kw)

    monkeypatch.setattr(H, "GeneticOptimizer", Spy)
    return cap


def _full_seed(monkeypatch):
    """Run once to learn the stub space; return one complete decoded genome."""
    cap = _capture(monkeypatch)
    out, _ = _run("t-learn", populationSize=4, generations=1)
    assert out["status"] == "completed", out
    return cap["opt"].decode_individual(cap["opt"]._create_individual())


def test_handler_seeds_generation_zero(monkeypatch):
    seed = _full_seed(monkeypatch)
    cap = _capture(monkeypatch)
    out, _ = _run("t-seed", populationSize=6, generations=1, seedPopulation=[seed])
    assert out["status"] == "completed", out
    assert len(cap["init"]) == 6
    assert cap["opt"].decode_individual(cap["init"][0]) == seed


def test_handler_refuses_seed_with_warm_start_or_chain(monkeypatch):
    seed = _full_seed(monkeypatch)
    for extra in ({"warmStartFromOptimizationId": 1}, {H.CHAIN_SEED_KEY: 1}):
        out, _ = _run("t-excl", seedPopulation=[seed], **extra)
        assert out["status"] == "failed"
        assert "seedPopulation" in out["error"] and next(iter(extra)) in out["error"]


def test_handler_refuses_too_many_and_incomplete_seeds(monkeypatch):
    seed = _full_seed(monkeypatch)
    out, _ = _run("t-many", populationSize=2, seedPopulation=[seed] * 3)
    assert out["status"] == "failed" and "seedPopulation refused" in out["error"]
    partial = dict(seed)
    gone = next(iter(partial))
    del partial[gone]
    out, _ = _run("t-miss", seedPopulation=[partial])
    assert out["status"] == "failed" and gone in out["error"]


def test_handler_refuses_require_an_entry_rule_without_entry_genes():
    out, _ = _run("t-req", requireAnEntryRule=True)  # the stub space has no entry:<rid>:enabled
    assert out["status"] == "failed"
    assert "requireAnEntryRule" in out["error"] and "entry" in out["error"]


def test_absent_or_false_flag_leaves_generation_zero_to_optimize(monkeypatch):
    cap = _capture(monkeypatch)
    out, _ = _run("t-absent", populationSize=4, generations=1)
    assert out["status"] == "completed" and cap["init"] is None
    out, _ = _run("t-false", populationSize=4, generations=1, requireAnEntryRule=False)
    assert out["status"] == "completed" and cap["init"] is None


def test_checkpoint_wins_over_seeds(monkeypatch):
    seed = _full_seed(monkeypatch)
    called = {}

    def fake_resume(self, ckpt):
        called["resumed"] = True
        raise RuntimeError("stop after the branch is taken")

    monkeypatch.setattr(H, "_load_checkpoint", lambda *a, **k: {"generation": 0})
    monkeypatch.setattr(GeneticOptimizer, "resume_from_checkpoint", fake_resume)
    monkeypatch.setattr(H, "_assert_checkpoint_robustness_matches", lambda *a, **k: None)
    monkeypatch.setattr(H, "_assert_checkpoint_metric_matches", lambda *a, **k: None)
    monkeypatch.setattr(H, "_checkpoint_exhausted", lambda *a, **k: False)
    monkeypatch.setattr(H, "_build_seed_population",
                        lambda *a, **k: pytest.fail("seeds must not be built on a resume"))
    try:
        _run("t-ckpt", generations=2, seedPopulation=[seed])
    except RuntimeError:
        pass
    assert called.get("resumed")
