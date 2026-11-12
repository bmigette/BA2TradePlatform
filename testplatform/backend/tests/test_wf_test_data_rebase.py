"""goal2027 two-phase walk-forward, phase B: a deferred fold is tested on the CURRENT command's data.

The out-of-sample re-run rebuilds its trial from the TRAIN job's stored config (``_persist_top_backtests``).
In January that config still names phase A's data: the market-condition manifests, the exclusions and,
for a static-universe (Senate) job, the universe. Under ``--wf-defer-last-test`` the test takes those
data choices from the command that runs it; the genome, the fixed settings and everything else stay the
train job's. Plan: docs/plans/2026-11-02-goal2027-two-phase-walk-forward.md."""
from __future__ import annotations

import copy
import os
import sys

import pytest

from app.services import walk_forward as WF
from app.services import wf_chain as C
from tests.test_walk_forward import D, _launcher_mod, _oos
from tests.test_wf_matrix_chain import ChainRunner, _spec

_TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

GOOD = _oos(5.0, -10.0)


@pytest.fixture(scope="module")
def L():
    return _launcher_mod()


# ------------------------------------------------------------------ what the current command says


def test_spec_of_a_screener_command(L):
    spec = L._data_rebase_from_optimize_args(
        ["--expert", "DeterministicScorer", "--universe", "AAPL,MSFT", "--screener", "--screener-store", "S2",
         "--market-condition-profile", "ohlcv-v1,ta-structure-v1",
         "--market-condition-manifest", "ohlcv-v1=aa,ta-structure-v1=bb", "--exclude-symbols", "x,Y"])
    assert spec == {"static_universe": None, "screener_store": "S2",
                    "manifests": {"ohlcv-v1": "aa", "ta-structure-v1": "bb"}, "exclude_symbols": ["X", "Y"]}


def test_spec_of_a_static_command_with_positional_manifests(L):
    spec = L._data_rebase_from_optimize_args(
        ["--universe", "aapl,nvda", "--market-condition-profile", "ohlcv-v1,ta-structure-v1",
         "--market-condition-manifest", "aa,bb"])
    assert spec == {"static_universe": ["AAPL", "NVDA"], "screener_store": None,
                    "manifests": {"ohlcv-v1": "aa", "ta-structure-v1": "bb"}, "exclude_symbols": []}


def test_spec_refuses_a_manifest_count_that_does_not_match_the_profiles(L):
    with pytest.raises(ValueError, match="profile"):
        L._data_rebase_from_optimize_args(["--universe", "A", "--market-condition-profile", "ohlcv-v1,ta-structure-v1",
                                           "--market-condition-manifest", "aa"])


# ------------------------------------------------------------------ applying it to the stored block


def _static_block(**kw):
    b = {"start_date": "2026-01-01", "end_date": "2026-12-31", "enabled_instruments": ["AAPL", "OLD"],
         "excluded_instruments": ["BAD1"], "experts": [{"class": "FMPSenateTraderWeight", "settings": {}}]}
    b.update(kw)
    return b


def _spec_dict(**kw):
    s = {"static_universe": None, "screener_store": None, "manifests": None, "exclude_symbols": []}
    s.update(kw)
    return s


def test_static_run_takes_the_current_universe_minus_every_exclusion(L):
    block = _static_block()
    before = copy.deepcopy(block)
    out = L._rebase_run_data(block, _spec_dict(static_universe=["AAPL", "NEW", "BAD1", "BAD2"],
                                               exclude_symbols=["BAD2"]))
    assert out["enabled_instruments"] == ["AAPL", "NEW"]
    assert out["excluded_instruments"] == ["BAD1", "BAD2"]   # stored + current: never lifted
    assert block == before                                    # the stored block is never mutated


def test_screener_run_keeps_its_universe_drops_newly_excluded_and_reads_the_current_store(L):
    block = _static_block(screener_opt={"store": "OLD_STORE", "cadence_days": 7})
    out = L._rebase_run_data(block, _spec_dict(screener_store="NEW_STORE", exclude_symbols=["OLD"]))
    assert out["enabled_instruments"] == ["AAPL"] and out["excluded_instruments"] == ["BAD1", "OLD"]
    assert out["screener_opt"] == {"store": "NEW_STORE", "cadence_days": 7}


def test_a_static_universe_for_a_screener_run_is_refused(L):
    with pytest.raises(ValueError, match="screener"):
        L._rebase_run_data(_static_block(screener_opt={"store": "S"}), _spec_dict(static_universe=["AAPL"]))


def test_a_screener_store_for_a_static_run_is_refused(L):
    with pytest.raises(ValueError, match="screener"):
        L._rebase_run_data(_static_block(), _spec_dict(screener_store="S", static_universe=["AAPL"]))


def test_a_static_run_without_a_current_universe_is_refused(L):
    with pytest.raises(ValueError, match="universe"):
        L._rebase_run_data(_static_block(), _spec_dict())


def test_everything_excluded_is_refused(L):
    with pytest.raises(ValueError, match="entire universe"):
        L._rebase_run_data(_static_block(), _spec_dict(static_universe=["AAPL"], exclude_symbols=["AAPL"]))


class _Reader:
    def symbols(self):
        return ["AAPL", "NEW"]


@pytest.fixture
def gated(L, monkeypatch):
    calls = []
    monkeypatch.setattr(L, "_market_condition_manifest_facts",
                        lambda digest, profile: {"reader": _Reader(), "digest_seen": digest})
    from app.services.backtest import seam_wiring
    monkeypatch.setattr(seam_wiring, "check_market_condition_coverage",
                        lambda cfg, ctx: calls.append(("coverage", cfg["start_date"], cfg["end_date"],
                                                       tuple(cfg["enabled_instruments"]))))
    monkeypatch.setattr(seam_wiring, "check_market_condition_window",
                        lambda cfg, ctx: calls.append(("window", cfg["start_date"], cfg["end_date"])))
    block = _static_block(market_condition_profiles=["ohlcv-v1"],
                          market_condition_manifests={"ohlcv-v1": "old"},
                          market_condition={"profiles": ["ohlcv-v1"], "manifests": {"ohlcv-v1": "old"},
                                            "facts": {"ohlcv-v1": {"digest_seen": "old"}}, "genes": ["g"]})
    return block, calls


def test_manifests_are_replaced_and_checked_on_the_test_window(L, gated):
    block, calls = gated
    out = L._rebase_run_data(block, _spec_dict(static_universe=["AAPL", "NEW"],
                                               manifests={"ohlcv-v1": "new"}))
    assert out["market_condition_manifests"] == {"ohlcv-v1": "new"}
    assert out["market_condition"]["manifests"] == {"ohlcv-v1": "new"}
    assert out["market_condition"]["facts"] == {"ohlcv-v1": {"digest_seen": "new"}}
    assert out["market_condition"]["genes"] == ["g"]           # the search space is the train job's
    assert ("coverage", "2026-01-01", "2026-12-31", ("AAPL", "NEW")) in calls
    assert ("window", "2026-01-01", "2026-12-31") in calls


def test_a_gated_run_tested_without_manifests_is_refused(L, gated):
    block, _ = gated
    with pytest.raises(ValueError, match="manifest"):
        L._rebase_run_data(block, _spec_dict(static_universe=["AAPL"]))


def test_other_profiles_than_the_train_job_are_refused(L, gated):
    block, _ = gated
    with pytest.raises(ValueError, match="profile"):
        L._rebase_run_data(block, _spec_dict(static_universe=["AAPL"], manifests={"ta-structure-v1": "x"}))


def test_manifests_for_an_ungated_run_are_refused(L):
    with pytest.raises(ValueError, match="manifest"):
        L._rebase_run_data(_static_block(), _spec_dict(static_universe=["AAPL"], manifests={"ohlcv-v1": "x"}))


# ------------------------------------------------------------------ plumbing: chain -> runner -> persist


class DataRunner(ChainRunner):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.data_args = []

    def run_oos(self, train_name, fold, picks, name_prefix, labels, parallel, data_args=None):
        self.data_args.append((fold.index, data_args))
        return super().run_oos(train_name, fold, picks, name_prefix, labels, parallel)


def test_a_deferring_cell_tests_every_fold_on_the_current_command_data(tmp_path):
    r = DataRunner([GOOD, GOOD, GOOD], completed={"cell-wf1", "cell-wf2", "cell-wf3"})
    C.run_cell(_spec(data_end=None, defer_last_test=True), r, str(tmp_path), log=lambda *_: None)
    assert [k for k, _ in r.data_args] == [1, 2, 3]
    assert all(a == _spec().optimize_args for _, a in r.data_args)


def test_a_plain_cell_keeps_the_stored_data(tmp_path):
    r = DataRunner([GOOD, GOOD, GOOD])
    C.run_cell(_spec(), r, str(tmp_path), log=lambda *_: None)
    assert [a for _, a in r.data_args] == [None, None, None]


def test_real_runner_hands_the_rebase_to_persist():
    import run_walk_forward as RW
    seen = {}

    class FakeL:
        @staticmethod
        def _data_rebase_from_optimize_args(args):
            return {"from": list(args)}

        @staticmethod
        def _persist_top_backtests(opt_id, expert, **kw):
            seen.update(kw)
            kw["persisted_ids"].append((1, 77))
            return 1

    runner = RW.RealRunner.__new__(RW.RealRunner)
    runner._summaries = lambda db, opt_id, names: {names[0]: (77,)}
    runner._metrics = lambda row, reused: "m"
    pick = WF.IsPick(rank=1, fitness=1.0, total_return=1.0, car=1.0, max_drawdown=-1.0, trades=10,
                     params={}, key="k")
    out = {}
    runner._rerun_chunks([pick], 1, FakeL, 5, "FMPRating", "WF3-OOS-R", ["OOS"], ("2026-01-01", "2026-12-31"),
                         {1: "WF3-OOS-R1-x"}, "x", out, data_args=["--universe", "A"])
    assert seen["data_rebase"] == {"from": ["--universe", "A"]} and out == {1: "m"}


def test_persist_applies_the_rebase_after_the_window(L, monkeypatch):
    """``data_rebase`` reaches the trial config, applied to the block that already carries the test window."""
    import app.models  # noqa: F401
    from app.models.database import Base, SessionLocal, engine
    from app.models.strategy import Strategy
    from app.models.strategy_optimization import StrategyOptimization
    from app.services import strategy_optimization_handler as soh
    from app.services import sync_client
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(sync_client, "push_backtest", lambda bt, db: None)
    seen, rebased = [], []

    def worker(cfg):
        seen.append(cfg)
        return {"ok": False, "error": "stop here"}
    monkeypatch.setattr(soh, "_persist_trial_worker", worker)
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(L, "_new_local_pool", lambda n: ThreadPoolExecutor(max_workers=n))
    real = L._rebase_run_data

    def spy(block, spec, command="walk-forward test"):
        rebased.append((block["start_date"], block["end_date"]))
        return real(block, spec, command)
    monkeypatch.setattr(L, "_rebase_run_data", spy)
    db = SessionLocal()
    try:
        strat = Strategy(name="wf-rebase", entry_rules=[], exit_rules=[])
        db.add(strat); db.commit(); db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="wf-rebase-train", fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={"backtest": {
                "backtest_id": 1, "start_date": "2020-01-01", "end_date": "2025-12-31", "labels": [],
                "enabled_instruments": ["AAPL", "OLD"], "experts": [{"class": "FMPRating", "settings": {}}],
                "initial_capital": 10000.0, "account_settings": {"starting_cash": 10000.0},
                "warmup_days": 30, "seed": 42}},
            all_results=[{"params": {}, "fitness": 1.0, "trades": 10}], best_params={}, best_fitness=1.0,
            status="completed")
        db.add(opt); db.commit(); db.refresh(opt)
        opt_id = opt.id
    finally:
        db.close()
    L._persist_top_backtests(opt_id, "FMPRating", n=1, parallel=1, candidates=[({}, "k", 1.0)], ranks=[1],
                             name_prefix="WF3-OOS-R", use_remote_workers=False, persisted_ids=[],
                             window=("2026-01-01", "2026-12-31"),
                             data_rebase=_spec_dict(static_universe=["AAPL", "NEW"]))
    assert rebased == [("2026-01-01", "2026-12-31")]
    assert seen and seen[0]["enabled_instruments"] == ["AAPL", "NEW"]


# ---- a screener job's static universe follows the TEST window (2026-11-10: every goal2027 phase-A cell failed its
# out-of-sample re-run on "candidates but have no bars in this run's price source": names listed after the train window)

def _screener_block(**kw):
    from ba2_providers.screener import universe_superset as us
    b = {"start_date": "2024-01-01", "end_date": "2024-12-31", "enabled_instruments": ["AAPL", "OLD"],
         "excluded_instruments": [], "screener_opt": {"store": "s"}, "screener_universe_rule": us.RULE_ID}
    b.update(kw)
    return b


def test_test_window_universe_is_recomputed_for_a_superset_screener_block(L, monkeypatch, capsys):
    from app.services.backtest import rerun_handler as rh
    calls = []

    def fake(block, ranges, *, row_id=None):
        calls.append((block["start_date"], block["end_date"], ranges))
        return {**block, "enabled_instruments": ["AAPL", "NEW"], "excluded_instruments": ["GONE"],
                "_universe_recompute_note": {"static_universe_size": 2, "previous_size": 2, "computed_size": 3,
                                             "excluded_for_missing_cache": ["GONE"], "criteria_version": None}}
    monkeypatch.setattr(rh, "recompute_static_universe", fake)
    out = L._test_window_static_universe(_screener_block(), {"screener:x": 1}, 7, "WF1-OOS-R")
    assert calls == [("2024-01-01", "2024-12-31", {"screener:x": 1})]
    assert out["enabled_instruments"] == ["AAPL", "NEW"]
    said = capsys.readouterr().out
    assert "recomputed for the test window 2024-01-01..2024-12-31" in said
    assert "EXCLUDED" in said and "GONE" in said               # an uncached name is dropped LOUDLY, by name


def test_test_window_universe_leaves_static_and_legacy_blocks_alone(L, monkeypatch):
    from app.services.backtest import rerun_handler as rh
    monkeypatch.setattr(rh, "recompute_static_universe",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not recompute")))
    static = {"start_date": "2024-01-01", "end_date": "2024-12-31", "enabled_instruments": ["AAPL"]}
    assert L._test_window_static_universe(static, {}, 1, "WF1-OOS-R") is static
    legacy = _screener_block(screener_universe_rule=None)
    assert L._test_window_static_universe(legacy, {}, 1, "WF1-OOS-R") is legacy


def test_persist_recomputes_the_universe_only_under_a_window_override(L, monkeypatch):
    import inspect
    src = inspect.getsource(L._persist_top_backtests)
    assert "if window is not None:\n            bt_block = _test_window_static_universe(" in src
    # after the rebase: the rebase's exclusions must be in the block the superset is computed from
    assert src.index("_rebase_run_data(bt_block, data_rebase)") < src.index("_test_window_static_universe(")
