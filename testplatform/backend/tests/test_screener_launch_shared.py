"""One function, two callers: ``ba2-test optimize --screener`` and the web API build a screener job's stamps with
``app.services.backtest.screener_launch`` / ``screener_job_request``.

The same request through the CLI path (``ba2test_launcher.main(["optimize", ...])``, in-process, ``--submit``) and through the
API path (``POST /api/strategies/{id}/optimize``) must yield the identical stamped ``screener_opt``, static universe,
``excluded_instruments``, screener genes, job-name tokens and checkpoint fingerprint.  Every refusal the CLI gives comes
back from the API as a 4xx with the same body and list.  FactorRanker keeps its weekly-store path; a gate-only (option) request
is refused with a stated reason; a request that carries stamps is verified, not trusted.

Synthetic world of ``packages/providers/tests/test_live_sim_parity.py`` (the one the gate tests use); no vendor call, the
cache folder is a tmp dir.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[3]
_LAUNCHER = REPO / "testplatform" / "ba2test_launcher.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


W = _load("live_sim_parity_worlds_shared", REPO / "packages/providers/tests/test_live_sim_parity.py")
L = _load("ba2test_launcher_shared_test", _LAUNCHER)
sys.path.insert(0, str(REPO / "tools"))
import matrix_flags as mf  # noqa: E402

from app.api import strategies as api  # noqa: E402
from app.services import strategy_optimization_handler as H  # noqa: E402
from app.services.backtest import screener_job_request as sjr  # noqa: E402
from app.services.backtest import screener_launch as sl  # noqa: E402
from ba2_providers.screener import live_sim as ls  # noqa: E402
from ba2_providers.screener import metric_store as msb  # noqa: E402
from ba2_providers.screener import universe_superset as us  # noqa: E402

UNCACHED = "S03"                       # one symbol of the world has no cached bars


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A tmp cache root with a complete daily panel of the synthetic world and cached bars for all but ``UNCACHED``."""
    import ba2_common.config as cfg
    cache = tmp_path / "cache"
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(cache))
    world = W.World(2, n_sym=40)
    panel = world.panel()
    pdir = cache / "screener" / "daily_panel" / "p1"
    arrs = {k: np.asarray(v) for k, v in panel.arrays.items()}
    man = {"shares_vendor_snapshot": "x", "shares_lag_days": 45, "stale_listed_symbols": [], "fresh_fraction": 1.0,
           "last_bar_date": panel.sessions[-2], "source_fingerprint": "t"}
    ls.save_panel(str(pdir), list(panel.symbols), panel.sessions, arrs, man)
    bars = cache / "FMPOHLCVProvider"
    bars.mkdir(parents=True)
    for s in panel.symbols:
        if s != UNCACHED:
            (bars / f"{s}_1d.parquet").write_bytes(b"")
    # the weekly metric store: only has to be non-empty for the classic (panel-gated) job; the bypass job reads it
    store_dir = str(tmp_path / "store")
    monkeypatch.setattr(msb, "load_store", lambda path: _WEEKLY)
    return {"cache": cache, "panel": str(pdir), "store": store_dir, "world": world, "sessions": panel.sessions}


_WEEKLY = pd.DataFrame([{"date": d, "symbol": f"S{i:02d}", "market_cap": 9.9e9 - i * 1e8, "price": 20.0, "close": 20.0,
                          "volume": 2e6, "sector": "T", "relative_volume": 2.0, "price_drop_pct": 5.0,
                          "price_drop_pct_22": 5.0, "float_shares": 1e8}
                         for d in ("2024-01-06", "2024-01-13") for i in range(40)])


def _window(env):
    return env["sessions"][300], env["sessions"][-5]


PARAM_RANGES = {k[len("screener_"):]: dict(v) for k, v in us.SCREENER_OPT.items()}


def _api_body(env, *, expert="FMPRating", screener_opt=None, extra_bt=None, name="eq-job"):
    start, end = _window(env)
    bt = {"engine": "daily", "expert": {"class": expert, "settings": {}},
          "universe": {"mode": "screener", "screener_settings": {}, "screener_param_ranges": PARAM_RANGES},
          "start_date": start, "end_date": end, "initial_capital": 10000.0, "execution_interval": "1d",
          "commission": 0.1, "slippage": 0.0, "fill_model": "next_bar_open", **(extra_bt or {})}
    so = {"param_ranges": PARAM_RANGES, "cadence_days": 7, "base_settings": {}, "store": env["store"]}
    so.update(screener_opt or {})
    return {"name": name, "fitness_metric": "calmar_ratio", "optimization_type": "genetic",
            "expert_params": {}, "optimization_config": {
                "populationSize": 4, "generations": 1, "crossoverProb": 0.6, "mutationProb": 0.2,
                "earlyStoppingGenerations": 1, "elitismPercent": 10.0, "seed": 7, "backtest": bt},
            "screener_opt": so}


def _post(client, strat, body):
    return client.post(f"/api/strategies/{strat.id}/optimize", json=body)


def _cli_row(env, monkeypatch, *extra, expert="FMPRating", name="eq-cli"):
    """Run the real CLI in-process (``main``), ``--submit`` (no GA), and return the stored optimization_config + name."""
    from app.models.database import SessionLocal
    from app.models.strategy_optimization import StrategyOptimization
    from app.services.task_queue import get_task_queue
    monkeypatch.setattr(get_task_queue(), "queue_task", lambda *a, **kw: "stub-task", raising=False)
    start, end = _window(env)
    argv = ["optimize", "--expert", expert, "--strategy", "S1", "--universe", "AAPL", "--start", start, "--end", end,
            "--interval", "1d", "--screener", "--screener-store", env["store"], "--name", name, "--submit",
            "--run-schedule", "daily", "--population", "4", "--generations", "1", "--seed", "7", *extra]
    rc = L.main(argv)
    assert rc == 0
    db = SessionLocal()
    try:
        row = (db.query(StrategyOptimization).filter(StrategyOptimization.name == name)
               .order_by(StrategyOptimization.id.desc()).first())
        return copy.deepcopy(row.optimization_config), row.name
    finally:
        db.close()


STAMP_KEYS = ("screener_opt", "enabled_instruments", "screener_universe_rule", "excluded_instruments")


def _stamps(cfg):
    bt = cfg["backtest"]
    return {k: bt.get(k) for k in STAMP_KEYS}


def _genes(cfg):
    return {k: v for k, v in cfg["expert_params"].items() if k.startswith("screener:")}


def _fingerprint(cfg):
    space = {"x": {"min": 0, "max": 1, "step": 1, "type": "int"}, **{k: v for k, v in _genes(cfg).items()}}
    bt = cfg["backtest"]
    return H.checkpoint_fingerprint(space, {"populationSize": 4, "generations": 1},
                                    H.checkpoint_expert_settings_identity(bt), None,
                                    bt.get("screener_universe_rule"), H._screener_identity(bt.get("screener_opt")))


# ------------------------------------------------------------------------------------------------ equality
def test_cli_and_api_yield_identical_stamps_universe_names_and_fingerprint(env, monkeypatch, client, seed_strategy, capsys):
    cli_cfg, cli_name = _cli_row(env, monkeypatch, "--screener-exclude-uncached", "--screener-panel", env["panel"])
    r = _post(client, seed_strategy, _api_body(env, screener_opt={"exclude_uncached": True}))
    assert r.status_code == 200, r.text
    api_cfg = r.json()["optimizationConfig"]
    a, c = _stamps(api_cfg), _stamps(cli_cfg)
    assert a == c, {k: (a[k], c[k]) for k in a if a[k] != c[k]}
    # non-vacuous: a real superset universe, the uncached name recorded as excluded, the panel stamp present
    assert len(a["enabled_instruments"]) > 10 and UNCACHED not in a["enabled_instruments"]
    assert a["excluded_instruments"] == [UNCACHED] and a["screener_universe_rule"] == us.RULE_ID
    so = a["screener_opt"]
    assert so["criteria_version"] == ls.CRITERIA_VERSION and so["panel"] == "screener/daily_panel/p1" and so["panel_fingerprint"]
    assert _genes(api_cfg) == _genes(cli_cfg) and len(_genes(api_cfg)) == len(us.SCREENER_OPT)
    assert _fingerprint(api_cfg) == _fingerprint(cli_cfg)
    other = copy.deepcopy(api_cfg)
    other["backtest"]["screener_opt"]["panel_fingerprint"] = "another"
    assert _fingerprint(other) != _fingerprint(api_cfg)                      # the fingerprint covers the stamp
    # name tokens: the API tokenises the requested name with the function the drivers' helper is pinned equal to
    assert r.json()["name"] == "eq-job-sup1-lds2" == mf.with_universe_rule_name("eq-job")
    assert cli_name == "eq-cli"                                              # the CLI keeps the caller's name verbatim
    for n in ("a", "a-timegene", "a-sup1-lds2", "scr-large-FactorRanker"):
        for sim in (True, False):
            assert sl.universe_rule_name(n, simulated=sim) == mf.with_universe_rule_name(n, simulated=sim)
    # handler contract: the API job carries every key the trial builder indexes
    assert all(api_cfg["backtest"].get(k) not in (None, "", [], {}) for k in sjr.HANDLER_BACKTEST_KEYS)
    assert api_cfg["backtest"]["experts"] == [{"class": "FMPRating", "settings": {}}]


def test_the_two_callers_share_the_function_not_a_copy():
    src = _LAUNCHER.read_text(encoding="utf-8")
    assert "_sl.prepare_screener_launch" in src and "_sl.apply_uncached_policy" in src
    assert "static_universe(" not in src.split("def _cmd_optimize")[1].split("def _cmd_optimize_batch")[0]
    api_src = (REPO / "testplatform/backend/app/api/strategies.py").read_text(encoding="utf-8")
    assert "sjr.apply_screener_request" in api_src
    assert "static_universe" not in api_src


# ------------------------------------------------------------------------------------------------ refusals
def _refuse_both(env, monkeypatch, client, strat, *, cli_extra=(), body, status=409):
    """The CLI exits with a message; the API answers with the same body."""
    with pytest.raises(SystemExit) as e:
        _cli_row(env, monkeypatch, *cli_extra)
    r = _post(client, strat, body)
    assert r.status_code == status, r.text
    return str(e.value), r.json()["detail"]


def _body_of(msg: str, prefix: str) -> str:
    assert msg.startswith(prefix), msg
    return msg[len(prefix):]


def test_uncached_symbols_refuse_with_the_same_list_and_the_remedy_named_per_surface(env, monkeypatch, client, seed_strategy):
    cli, api_ = _refuse_both(env, monkeypatch, client, seed_strategy, cli_extra=("--screener-panel", env["panel"]),
                             body=_api_body(env))
    cb, ab = _body_of(cli, "optimize: --screener REFUSED: "), _body_of(api_, "screener optimization REFUSED: ")
    head = lambda s: s.split(". Fetch them")[0]
    assert head(cb) == head(ab) and UNCACHED in head(ab)
    assert "--screener-exclude-uncached" in cb and "screener_opt.exclude_uncached" in ab


def test_no_panel_is_refused_not_served_by_the_weekly_gate(env, monkeypatch, client, seed_strategy):
    import shutil
    shutil.rmtree(env["panel"])
    cli, api_ = _refuse_both(env, monkeypatch, client, seed_strategy, cli_extra=("--screener-exclude-uncached",),
                             body=_api_body(env, screener_opt={"exclude_uncached": True}))
    assert (_body_of(cli, "optimize: --screener REFUSED: ") == _body_of(api_, "screener optimization REFUSED: ")
            and "no daily criteria panel under" in api_ and "ba2-test prewarm --screener-panel" in api_)


@pytest.mark.parametrize("what", ["criteria", "window", "stale", "pending"])
def test_panel_problems_come_back_as_the_same_list(env, monkeypatch, client, seed_strategy, what):
    man_path = Path(env["panel"]) / "manifest.json"
    man = json.loads(man_path.read_text())
    if what == "criteria":
        man["criteria_version"] = "old-weekly"
    elif what == "stale":
        man["stale_listed_symbols"] = ["AAA", "BBB"]
    elif what == "pending":
        man["excluded_unusable"] = [{"symbol": "ZZZ", "reason": "bad data", "added": "2026-10-01", "reviewed_by": "pending review"}]
    man_path.write_text(json.dumps(man))
    body = _api_body(env, screener_opt={"exclude_uncached": True})
    if what == "window":
        body["optimization_config"]["backtest"]["start_date"] = env["sessions"][10]
        body["optimization_config"]["backtest"]["end_date"] = "2031-01-01"
        extra = ()
    else:
        extra = ()
    # the CLI reads the same files; its window is the fixture's, so compare the API against ls.panel_problems itself
    r = _post(client, seed_strategy, body)
    assert r.status_code == 409
    start = body["optimization_config"]["backtest"]["start_date"]
    end = body["optimization_config"]["backtest"]["end_date"]
    probs = ls.panel_problems(env["panel"], start, end)
    assert probs
    for p in probs:
        assert "\n  - " + p in r.json()["detail"]
    assert r.json()["detail"].startswith("screener optimization REFUSED: the daily screener-criteria panel cannot serve this job:")
    if what != "window":
        with pytest.raises(SystemExit) as e:
            _cli_row(env, monkeypatch, "--screener-exclude-uncached", "--screener-panel", env["panel"])
        assert (_body_of(str(e.value), "optimize: --screener REFUSED: ")
                == _body_of(r.json()["detail"], "screener optimization REFUSED: "))


def test_intraday_coverage_below_threshold_refuses_with_the_cli_text(env, monkeypatch, client, seed_strategy):
    from ba2_providers.screener import intraday_coverage as ic
    bars = env["cache"] / "FMPOHLCVProvider"
    for s in env["world"].symbols:
        (bars / f"{s}_5min.parquet").write_bytes(b"")
    monkeypatch.setattr(ic, "scan", lambda *a, **k: {
        "interval": "5min", "start": "x", "end": "y", "n_symbols": 3, "no_intraday_file": [], "daily_sessions": 100,
        "covered_sessions": 50, "missing_coverage_share": 0.5, "worst_coverage": [{"symbol": "S01", "covered": 1, "daily": 10}]})
    body = _api_body(env, screener_opt={"exclude_uncached": True}, extra_bt={"execution_interval": "5min"})
    r = _post(client, seed_strategy, body)
    assert r.status_code == 409
    expect = ic.problems(ic.scan())[0]
    assert r.json()["detail"] == "screener optimization: REFUSED (intraday coverage):\n  - " + expect
    with pytest.raises(SystemExit) as e:
        _cli_row_5min(env, monkeypatch)
    assert str(e.value) == "optimize: REFUSED (intraday coverage):\n  - " + expect


def _cli_row_5min(env, monkeypatch):
    from app.services.task_queue import get_task_queue
    monkeypatch.setattr(get_task_queue(), "queue_task", lambda *a, **kw: "stub-task", raising=False)
    start, end = _window(env)
    return L.main(["optimize", "--expert", "FMPRating", "--strategy", "S1", "--universe", "AAPL", "--start", start, "--end", end,
                   "--interval", "5min", "--screener", "--screener-store", env["store"], "--name", "cov", "--submit",
                   "--run-schedule", "daily", "--screener-exclude-uncached", "--screener-panel", env["panel"]])


def test_unknown_gene_refuses_instead_of_guessing_its_loosest_value(env, client, seed_strategy):
    body = _api_body(env, screener_opt={"exclude_uncached": True,
                                        "param_ranges": {**PARAM_RANGES, "mystery_gene": {"optimize": True, "min": 0, "max": 1, "step": 1, "type": "int"}}})
    r = _post(client, seed_strategy, body)
    assert r.status_code == 409 and "mystery_gene" in r.json()["detail"] and "no declared role" in r.json()["detail"]


def test_warm_start_with_out_of_range_seeds_is_refused_at_launch(env, client, seed_strategy, gate_engine):
    from app.models import StrategyOptimization
    from sqlalchemy.orm import sessionmaker
    s = sessionmaker(bind=gate_engine)()
    src = StrategyOptimization(strategy_id=seed_strategy.id, name="src", fitness_metric="x", optimization_type="genetic",
                               optimization_config={}, status="completed",
                               all_results=[{"params": {"screener:screener_market_cap_min": 5e10}, "fitness": 1.0}])
    s.add(src); s.commit(); sid = src.id; s.close()
    body = _api_body(env, screener_opt={"exclude_uncached": True})
    body["optimization_config"]["warmStartFromOptimizationId"] = sid
    r = _post(client, seed_strategy, body)
    assert r.status_code == 409 and "warm-start individual #0" in r.json()["detail"] and "market_cap_min" in r.json()["detail"]
    body["optimization_config"]["warmStartFromOptimizationId"] = 10 ** 6
    assert _post(client, seed_strategy, body).status_code == 400


# ------------------------------------------------------------------------------------------------ request shape
def test_malformed_requests_are_400_and_nothing_is_enqueued(env, client, seed_strategy):
    for bad in ({"cadence_days": None}, {"base_settings": None}, {"param_ranges": {}}, {"cap_band": "huge"},
                {"apply_to_expert_settings": True}, {"panel": "../../etc"}):
        r = _post(client, seed_strategy, _api_body(env, screener_opt={**bad, "exclude_uncached": True}))
        assert r.status_code == 400, (bad, r.text)
    body = _api_body(env, screener_opt={"exclude_uncached": True})
    del body["optimization_config"]["backtest"]["fill_model"]
    r = _post(client, seed_strategy, body)
    assert r.status_code == 400 and "fill_model" in r.json()["detail"]


def test_gate_only_option_requests_are_refused_with_the_reason(env, client, seed_strategy):
    r = _post(client, seed_strategy, _api_body(env, screener_opt={"gate_only": True}))
    assert r.status_code == 400 and "--screener-gate-store" in r.json()["detail"]


def test_requests_without_screener_opt_are_untouched(env, client, seed_strategy):
    body = _api_body(env)
    del body["screener_opt"]
    r = _post(client, seed_strategy, body)
    assert r.status_code == 200
    cfg = r.json()["optimizationConfig"]
    assert cfg["backtest"] == body["optimization_config"]["backtest"] and r.json()["name"] == "eq-job"


# ------------------------------------------------------------------------------------------------ FactorRanker
def test_factor_ranker_keeps_the_weekly_store_gate_and_the_sup1_name(env, monkeypatch, client, seed_strategy):
    store_syms = sorted(_WEEKLY["symbol"].unique())
    cached = set(store_syms)
    for s in store_syms:                                             # FR ignores the panel: cached bars only
        (env["cache"] / "FMPOHLCVProvider" / f"{s}_1d.parquet").write_bytes(b"")
    body = _api_body(env, expert="FactorRanker", screener_opt={"exclude_uncached": True})
    # the weekly scans must sit inside the visible window of the request
    body["optimization_config"]["backtest"]["start_date"] = "2024-01-08"
    body["optimization_config"]["backtest"]["end_date"] = "2024-01-30"
    r = _post(client, seed_strategy, body)
    assert r.status_code == 200, r.text
    cfg = r.json()["optimizationConfig"]
    so = cfg["backtest"]["screener_opt"]
    assert so["apply_to_expert_settings"] is True
    assert "panel" not in so and "criteria_version" not in so and "panel_fingerprint" not in so
    assert cfg["backtest"]["screener_universe_rule"] == us.RULE_ID
    assert set(cfg["backtest"]["enabled_instruments"]) <= cached and cfg["backtest"]["enabled_instruments"]
    assert r.json()["name"] == "eq-job-sup1" == mf.with_universe_rule_name("eq-job", simulated=False)
    # the same universe as the weekly store rule the CLI applies for the bypass expert
    base = {**sjr_off(), **msb.normalize_screener_settings({})}
    want = us.static_universe(_WEEKLY, "2024-01-08", "2024-01-30", base, us.gene_ranges_from_opt(us.SCREENER_OPT), intraday=False)
    assert cfg["backtest"]["enabled_instruments"] == [s for s in want]


def sjr_off():
    from ba2_common.core.deploy_parity import SCREENER_OFF_VALUES
    return dict(SCREENER_OFF_VALUES)


def test_factor_ranker_request_with_a_contradicting_bypass_flag_is_refused(env, client, seed_strategy):
    r = _post(client, seed_strategy, _api_body(env, expert="FactorRanker",
                                               screener_opt={"exclude_uncached": True, "apply_to_expert_settings": False}))
    assert r.status_code == 400 and "apply_to_expert_settings" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ stamped requests
def _stamped(env, client, strat):
    r = _post(client, strat, _api_body(env, screener_opt={"exclude_uncached": True}))
    assert r.status_code == 200, r.text
    return r.json()["optimizationConfig"]


def _replay_body(env, cfg, mutate=None):
    body = _api_body(env, screener_opt={"exclude_uncached": True})
    bt = body["optimization_config"]["backtest"]
    for k in ("enabled_instruments", "screener_universe_rule", "excluded_instruments"):
        bt[k] = copy.deepcopy(cfg["backtest"][k])
    body["screener_opt"].update({k: copy.deepcopy(cfg["backtest"]["screener_opt"][k])
                                 for k in ("criteria_version", "behaviour", "panel", "panel_fingerprint", "declared_ranges")})
    if mutate:
        mutate(body)
    return body


def test_a_replayed_stamped_config_is_verified_and_accepted(env, client, seed_strategy):
    cfg = _stamped(env, client, seed_strategy)
    r = _post(client, seed_strategy, _replay_body(env, cfg))
    assert r.status_code == 200, r.text
    assert _stamps(r.json()["optimizationConfig"]) == _stamps(cfg)


@pytest.mark.parametrize("field, mutate, needle", [
    ("universe", lambda b: b["optimization_config"]["backtest"]["enabled_instruments"].append("EVIL"), "enabled_instruments differ"),
    ("fingerprint", lambda b: b["screener_opt"].update(panel_fingerprint="forged"), "forged"),
    ("criteria", lambda b: b["screener_opt"].update(criteria_version="live-daily-v1"), "criteria_version"),
    ("rule", lambda b: b["optimization_config"]["backtest"].update(screener_universe_rule="top50-v0"), "screener_universe_rule"),
    ("ranges", lambda b: b["screener_opt"]["declared_ranges"]["market_cap_min"].update(max=1e12), "declared_ranges"),
    ("panel", lambda b: b["screener_opt"].update(panel="screener/daily_panel/nope"), "panel"),
])
def test_a_stamp_that_disagrees_with_the_servers_own_build_is_refused(env, client, seed_strategy, field, mutate, needle):
    cfg = _stamped(env, client, seed_strategy)
    r = _post(client, seed_strategy, _replay_body(env, cfg, mutate))
    assert r.status_code in (400, 409), r.text
    assert needle in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ batch
def test_batch_builds_all_jobs_first_and_enqueues_none_when_one_is_refused(env, client, seed_strategy, gate_engine):
    """FactorRanker (weekly store, bars cached) is valid; FMPRating (daily panel) is refused because the panel is gone.
    FactorRanker comes FIRST in the batch: it must not be left enqueued."""
    import shutil
    shutil.rmtree(env["panel"])
    (env["cache"] / "FMPOHLCVProvider" / f"{UNCACHED}_1d.parquet").write_bytes(b"")
    body = _api_body(env, screener_opt={"exclude_uncached": True})
    batch = {"experts": ["FactorRanker", "FMPRating"], "strategy_id": seed_strategy.id, "fitness_metric": "calmar_ratio",
             "optimization_type": "genetic", "expert_params": {}, "name_prefix": "B",
             "optimization_config": {**body["optimization_config"],
                                     "backtest": {k: v for k, v in body["optimization_config"]["backtest"].items() if k != "expert"}},
             "screener_opt": body["screener_opt"]}
    from sqlalchemy.orm import sessionmaker
    from app.models import StrategyOptimization
    s = sessionmaker(bind=gate_engine)()
    before = s.query(StrategyOptimization).count()
    r = client.post("/api/strategies/optimize-batch", json=batch)
    assert r.status_code == 409 and "no daily criteria panel under" in r.json()["detail"], r.text
    assert s.query(StrategyOptimization).count() == before                  # nothing half-enqueued
    s.close()


def test_batch_enqueues_every_job_with_its_own_tokens_and_one_shared_universe_build(env, client, seed_strategy, monkeypatch):
    calls = []
    real = sl.build_screener_launch
    monkeypatch.setattr(sl, "build_screener_launch", lambda *a, **k: calls.append(1) or real(*a, **k))
    body = _api_body(env, screener_opt={"exclude_uncached": True})
    batch = {"experts": ["FMPRating", "FMPRating"], "strategy_id": seed_strategy.id, "fitness_metric": "calmar_ratio",
             "optimization_type": "genetic", "expert_params": {}, "name_prefix": "B",
             "optimization_config": {**body["optimization_config"],
                                     "backtest": {k: v for k, v in body["optimization_config"]["backtest"].items() if k != "expert"}},
             "screener_opt": body["screener_opt"]}
    r = client.post("/api/strategies/optimize-batch", json=batch)
    assert r.status_code == 200, r.text
    assert [j["name"] for j in r.json()["jobs"]] == ["B-FMPRating-calmar_ratio-sup1-lds2"] * 2
    assert len(calls) == 1                                                  # the memo: one build for two identical jobs
