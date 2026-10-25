"""Without ``--benchmark-regime-gates`` nothing about an option job moves (rules, gene keys, job identity).

The fixture ``fixtures/spy_regime_golden_baseline.json`` was generated ONCE, on the untouched branch
(``python test_spy_regime_opt_in_golden.py --regen``). NEVER regenerate it in a later task: a failing
test here means the SPY regime gate leaked into a job that did not ask for it.

The job identity is the digest of the command tokens ``discovery_name`` hashes (everything after
``optimize``, with the job name blank) plus the launcher-fixed option settings. The launcher's absolute
path and the store env vars are left out on purpose: the golden must hold in any checkout.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_ROOT = os.path.abspath(os.path.join(_BACKEND, "..", ".."))
_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "spy_regime_golden_baseline.json")
_LAUNCHER = os.path.join(_ROOT, "testplatform", "ba2test_launcher.py")
_DRIVER = os.path.join(_ROOT, "tools", "run_options_matrix.py")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# single leg, vertical, composite, neutral, wheel, group member
_STRATS = ["O_BULLPS", "O_CSP", "O_LC", "O_IC", "O_WHEEL", "O_STRD"]
_NAMED = ["O_BULLPS", "O_CSP"]
_GRID_ARGV = ["--profile", "discovery", "--start", "2020-01-01", "--end", "2025-12-31",
              "--screener-gate-store", "/x/metric_store", "--max-stock-price", "0",
              "--early-stop", "8"]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _snapshot(launcher, kind):
    from app.services.strategy_param_space import collect_param_space
    s = launcher._build_strategy(kind, f"golden-{kind}", "FMPRating")
    return {"rules_sha": _sha(s.entry_rules), "exit_rules_sha": _sha(s.exit_rules),
            "genes": sorted(collect_param_space(s))}


def _identity(driver, strat):
    a = driver.resolve_args(driver.build_parser(), _GRID_ARGV)
    cmd = driver.build_cmd(a, _DRIVER, "", "DeterministicScorer", strat, "AAPL", "legacy")
    tokens = cmd[cmd.index("optimize") + 1:]
    fixed = driver.option_fixed_settings(_LAUNCHER, "DeterministicScorer", strat)
    return _sha({"tokens": tokens, "fixed": fixed})


def _current():
    launcher, driver = _load("l_spy_golden", _LAUNCHER), _load("d_spy_golden", _DRIVER)
    return {"rules": {k: _snapshot(launcher, k) for k in _STRATS},
            "identity": {k: _identity(driver, k) for k in _NAMED}}


def _golden():
    with open(_FIX, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def current():
    return _current()


@pytest.mark.parametrize("kind", _STRATS)
def test_existing_jobs_unchanged_without_the_flag(current, kind):
    assert current["rules"][kind] == _golden()["rules"][kind]


@pytest.mark.parametrize("kind", _NAMED)
def test_job_identity_unchanged_without_the_flag(current, kind):
    assert current["identity"][kind] == _golden()["identity"][kind]


def test_golden_is_not_vacuous():
    g = _golden()
    assert set(g["rules"]) == set(_STRATS)
    assert all(v["genes"] for v in g["rules"].values())


if __name__ == "__main__":
    if "--regen" not in sys.argv:
        sys.exit("pass --regen to (re)write the baseline; do this ONLY on the untouched branch")
    with open(_FIX, "w", encoding="utf-8") as fh:
        json.dump(_current(), fh, indent=1, sort_keys=True)
    print("wrote", _FIX)
