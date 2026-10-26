"""goal2027 item 2.9: ``--search-exploration-ranges`` (name token ``explr``).

Every dimension the exploration phase-1 families varied by hand must be reachable by the GA in the matching goal2027
cell, with the family's CONTROL value on the lattice. Design: docs/plans/2026-10-25-goal2027-grid-design.md 2.9
(the audit table lives there).

What this pins:
* without the flag nothing changes (every audited expert/strategy builds the same gene space as before);
* with it, the audited gaps are closed: the time-exit lattices contain the exploration values and the control, an
  EarningsDrift S5 cell gets a toggleable time exit ("off" = unlimited hold), and nothing else moves;
* the flag is refused where it would change nothing, the genes decode / export through the ordinary rule path;
* the capband driver requires the ``explr`` name token and forwards the flag only where it applies.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.strategy_param_space import collect_param_space, decode_params
from ba2_common.core.rules_convert import live_export_to_trade_rules, trade_rules_to_live_export

_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO = os.path.dirname(_TESTPLATFORM)
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_explr", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_explr"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()


@pytest.fixture(autouse=True)
def _flag_off_after():
    yield
    _M._resolve_search_exploration_ranges(False, "optimize")


def _lattice(spec):
    lo, hi, st = spec["min"], spec["max"], spec["step"]
    out, v, i = [], lo, 0
    while v <= hi + 1e-9:
        out.append(round(v, 6))
        i += 1
        v = lo + i * st
    return set(out)


def _built(kind, expert, flag):
    _M._resolve_search_exploration_ranges(flag, "optimize", expert)
    strat = _M._build_strategy(kind, "n", expert)
    return strat, collect_param_space(strat)


def _space(kind, expert, flag):
    return _built(kind, expert, flag)[1]


def _time_key(strat):
    """Gene key of the strategy's ONE days_opened threshold (S1 renames its leaf ids, so look it up)."""
    ids = [leaf_id for r in strat.exit_rules for leaf_id in _leaf_ids(r["conditions"], "days_opened")]
    assert len(ids) == 1, ids
    return f"cond:{ids[0]}:value"


def _leaf_ids(node, field, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("field") == field:
            out.append(node["id"])
        for c in node.get("conditions") or []:
            _leaf_ids(c, field, out)
    return out


def _time_gene(strat_and_space):
    strat, sp = strat_and_space
    return sp[_time_key(strat)]


#: (expert, strategy, exploration values that must be reachable, control that must be reachable or None=off)
GAPS = [
    ("FMPInsiderClusterBuy", "S1", {60, 90, 120, 210}, 210),     # mid_insider timeout 60/90/120 + the 210 control
    ("DeterministicScorer", "S1", {15, 20, 25, 30}, 25),         # mid_ds timeout 15/20/25/30, control 25
    ("DeterministicScorer", "S2", {15, 20, 25, 30}, 25),
    ("DeterministicScorer", "S6", {15, 20, 25, 30}, 25),         # already covered: must stay so
    ("FMPEarningsDrift", "S2", {60, 90, 120}, None),             # small_earnings timeout 60/90/120, control off
    ("FMPEarningsDrift", "S1", {60, 90, 120}, None),
    ("FMPEarningsDrift", "S3", {60, 90, 120}, None),
    ("FMPRating", "S2", {60, 90, 120}, None),                    # small_rating timeout 60/90/120, control off
    ("FMPRating", "S1", {60, 90, 120}, None),
    ("DeterministicScorer", "SQM", {15, 30, 60}, 30),            # analyst_targets hold 15/30/60
]


class TestTimeExitLattices:
    @pytest.mark.parametrize("expert,kind,values,control", GAPS)
    def test_exploration_values_and_control_are_reachable_with_the_flag(self, expert, kind, values, control):
        lat = _lattice(_time_gene(_built(kind, expert, True)))
        assert values <= lat, (expert, kind, sorted(values - lat))
        if control is not None:
            assert control in lat

    @pytest.mark.parametrize("expert,kind", [("FMPEarningsDrift", "S1"), ("FMPEarningsDrift", "S2"),
                                              ("FMPEarningsDrift", "S3"), ("FMPEarningsDrift", "S5"),
                                              ("FMPRating", "S1"), ("FMPRating", "S2"),
                                              ("FMPInsiderClusterBuy", "S1"), ("DeterministicScorer", "S1"),
                                              ("DeterministicScorer", "S2")])
    def test_time_exit_can_be_switched_off(self, expert, kind):
        sp = _space(kind, expert, True)
        toggles = [k for k in sp if k.startswith("exit:") and "time" in k and k.endswith(":enabled")]
        assert len(toggles) == 1, toggles
        assert sp[toggles[0]]["min"] == 0 and sp[toggles[0]]["max"] == 1

    def test_the_gaps_really_were_gaps_without_the_flag(self):
        assert 210 not in _lattice(_time_gene(_built("S1", "FMPInsiderClusterBuy", False)))
        assert 90 not in _lattice(_time_gene(_built("S2", "FMPRating", False)))
        assert 15 not in _lattice(_time_gene(_built("S1", "DeterministicScorer", False)))
        assert not [k for k in _space("S5", "FMPEarningsDrift", False) if "time" in k]
        assert 15 not in _lattice(_time_gene(_built("SQM", "DeterministicScorer", False)))

    def test_defaults_sit_on_the_widened_lattice(self):
        for expert, kind, _v, _c in GAPS:
            _M._resolve_search_exploration_ranges(True, "optimize", expert)
            strat = _M._build_strategy(kind, "n", expert)
            gene = collect_param_space(strat)[_time_key(strat)]
            dec = decode_params(strat, {})
            days = [leaf for r in dec["exit_rules"] for leaf in _leaves(r["conditions"]) if leaf[0] == "days_opened"]
            assert days and float(days[0][2]) in _lattice(gene), (expert, kind, days, gene)


def _leaves(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("field"):
            out.append((node["field"], node.get("op"), node.get("value")))
        for c in node.get("conditions") or []:
            _leaves(c, out)
    return out


class TestNothingElseMoves:
    @pytest.mark.parametrize("expert,kind", [
        ("FMPRating", "S3"), ("FMPRating", "S6"), ("FMPEarningsDrift", "S6"), ("FMPInsiderClusterBuy", "S2"),
        ("DeterministicScorer", "S3"), ("DeterministicScorer", "S5"), ("FMPSenateTraderWeight", "S2"),
    ])
    def test_unlisted_pairs_build_the_same_space_with_the_flag_on_or_off(self, expert, kind):
        # (the launcher refuses the flag for these in a real run; the builder itself must be inert)
        off = _space(kind, expert, False)
        _M._resolve_search_exploration_ranges(True, "optimize", "FMPRating")
        on = collect_param_space(_M._build_strategy(kind, "n", expert))
        assert on == off

    def test_only_the_time_gene_changes_for_a_listed_pair(self):
        off = _space("S2", "FMPRating", False)
        on = _space("S2", "FMPRating", True)
        assert set(on) == set(off)
        assert {k for k in on if on[k] != off[k]} == {"cond:xt:value"}

    def test_s5_earnings_drift_gains_exactly_one_toggleable_rule(self):
        off = _space("S5", "FMPEarningsDrift", False)
        on = _space("S5", "FMPEarningsDrift", True)
        assert set(on) - set(off) == {"exit:exit_time:enabled", "cond:xt:value"}
        assert all(on[k] == off[k] for k in off)
        assert _lattice(on["cond:xt:value"]) >= {60, 90, 120}

    def test_s5_for_other_experts_is_untouched(self):
        assert _space("S5", "FMPRating", True) == _space("S5", "FMPRating", False)


class TestS5TimeExitRule:
    def _strat(self):
        _M._resolve_search_exploration_ranges(True, "optimize", "FMPEarningsDrift")
        return _M._build_strategy("S5", "n", "FMPEarningsDrift")

    def test_it_closes_and_sits_before_the_always_matching_floor_stop(self):
        ids = [r["id"] for r in self._strat().exit_rules]
        assert ids.index("exit_time") < ids.index("exit_stoploss")
        assert ids[-1] == "exit_stoploss", ids      # first match wins: the floor stop must stay last

    def test_off_removes_it_and_a_value_moves_it(self):
        s = self._strat()
        assert "exit_time" not in [r["id"] for r in decode_params(s, {"exit:exit_time:enabled": 0})["exit_rules"]]
        dec = decode_params(s, {"exit:exit_time:enabled": 1, "cond:xt:value": 90.0})
        rule = next(r for r in dec["exit_rules"] if r["id"] == "exit_time")
        assert [a["action_type"] for a in rule["actions"]] == ["close"]
        assert ("days_opened", ">", 90.0) in [(f, o, float(v)) for f, o, v in _leaves(rule["conditions"])]

    def test_survives_export_and_the_deploy_conversion(self):
        from app.api.backtests import _derive_export_payload
        from app.models.backtest import Backtest
        s = self._strat()
        genome = {"exit:exit_time:enabled": 1, "cond:xt:value": 120.0}
        dec = decode_params(s, genome)
        bt = Backtest(name="TOP1-s5", expert_name="FMPEarningsDrift", engine_type="daily_expert",
                      strategy_params={**genome, "entryRules": dec["entry_rules"], "exitRules": dec["exit_rules"]},
                      start_date=datetime(2024, 1, 1), end_date=datetime(2024, 6, 1), initial_capital=10_000.0,
                      optimization_id=None)
        out = _derive_export_payload(bt, "ruleset", db=None)
        back = live_export_to_trade_rules(trade_rules_to_live_export(out["entry_rules"], out["exit_rules"],
                                                                      name="s5"))
        found = [(f, float(v)) for r in back["exit_rules"] for f, o, v in _leaves(r["conditions"])
                 if f == "days_opened"]
        assert found == [("days_opened", 120.0)]


class TestLauncherFlag:
    def test_resolver_sets_and_resets_the_global(self):
        assert _M._resolve_search_exploration_ranges(True, "optimize", "FMPRating") is True
        assert _M._resolve_search_exploration_ranges(False, "optimize-batch") is False
        assert _M._SEARCH_EXPLORATION_RANGES is False

    @pytest.mark.parametrize("expert", ["FMPSenateTraderWeight", "FactorRanker", "FMPEarningsEvent"])
    def test_refused_for_experts_with_no_audited_dimension(self, expert):
        with pytest.raises(SystemExit) as e:
            _M._resolve_search_exploration_ranges(True, "optimize", expert)
        assert "--search-exploration-ranges" in str(e.value)

    def test_refused_for_a_strategy_it_would_not_change(self):
        _M._resolve_search_exploration_ranges(True, "optimize", "FMPRating")
        _M._refuse_exploration_ranges_unsupported("optimize", "FMPRating", "S1")        # listed: fine
        for expert, kind in (("FMPRating", "S6"), ("FMPRating", "S3"), ("DeterministicScorer", "S3"),
                             ("FMPInsiderClusterBuy", "S2"), ("FMPRating", "O_LC")):
            with pytest.raises(SystemExit) as e:
                _M._refuse_exploration_ranges_unsupported("optimize", expert, kind)
            assert "would not change" in str(e.value)
        _M._resolve_search_exploration_ranges(False, "optimize")
        _M._refuse_exploration_ranges_unsupported("optimize", "FMPRating", "S6")        # flag off: no raise

    def test_dsfw_and_explr_share_no_gene(self):
        _M._resolve_search_ds_factor_weights(True, "optimize", "DeterministicScorer")
        try:
            dsfw = set(_M._ds_factor_gene_space())
        finally:
            _M._resolve_search_ds_factor_weights(False, "optimize")
        sp = _space("S1", "DeterministicScorer", True)
        assert not [k for k in sp if k.startswith("model:") and k[6:] in dsfw]


_DRIVER = os.path.join(_REPO, "tools", "run_screener_capband_matrix.py")


@pytest.fixture(scope="module")
def driver():
    spec = importlib.util.spec_from_file_location("capband_explr", _DRIVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestDriver:
    def test_forwarded_only_where_it_changes_something(self, driver):
        on = SimpleNamespace(search_exploration_ranges=True)
        off = SimpleNamespace(search_exploration_ranges=False)
        assert driver.exploration_range_tokens(on, "FMPRating", "S1") == ["--search-exploration-ranges"]
        assert driver.exploration_range_tokens(on, "DeterministicScorer", "SQM") == ["--search-exploration-ranges"]
        assert driver.exploration_range_tokens(on, "FMPRating", "S6") == []
        assert driver.exploration_range_tokens(on, "FMPSenateTraderWeight", "S1") == []
        assert driver.exploration_range_tokens(on, "FactorRanker", None) == []
        assert driver.exploration_range_tokens(off, "FMPRating", "S1") == []

    def test_the_driver_table_matches_the_launcher_table(self, driver):
        assert driver.EXPLORATION_RANGE_PAIRS == {e: set(k) for e, k in _M._EXPLORATION_RANGE_PAIRS.items()}

    @staticmethod
    def _run(*argv):
        import subprocess
        return subprocess.run([sys.executable, _DRIVER, *argv, "--dry-run"], capture_output=True, text=True)

    def test_requires_the_explr_name_token(self):
        r = self._run("--search-exploration-ranges", "--name-suffix", "plain")
        assert r.returncode != 0 and "explr" in r.stderr

    def test_names_without_the_flag_are_unchanged_and_with_it_differ(self):
        a = self._run("--name-suffix", "goal2027-explr", "--strategies", "S1", "--skip-experts", "FactorRanker")
        b = self._run("--name-suffix", "goal2027-explr", "--search-exploration-ranges", "--strategies", "S1",
                      "--skip-experts", "FactorRanker")
        assert a.returncode == 0 and b.returncode == 0, a.stderr[-500:] + b.stderr[-500:]
        names = lambda out: {ln.split()[1] for ln in out.splitlines() if ln.strip().startswith("TODO")}   # noqa: E731
        na, nb = names(a.stdout), names(b.stdout)
        assert na and nb and na != nb
