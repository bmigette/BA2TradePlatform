"""goal2027 item 2.2: the ``market:profile`` gene (``ba2-test optimize --search-market-profile``).

Design: docs/plans/2026-10-25-goal2027-grid-design.md section 2.2; plan:
docs/plans/2026-10-25-goal2027-market-profile-gene-plan.md.

What this pins:
* ``market:profile`` REPLACES ``market:enabled`` (never both) and is the last gene;
* ``off`` decodes exactly like ``market:enabled = 0`` (S1 included since the entry-rule fix); a selected profile decodes exactly like a run that only
  carries that profile's leaves (the other profile's leaves off, its exit rules off);
* a market exit rule can only run when every profile it reads is selected;
* two genomes differing only in what belongs to an unselected profile share one trial-memo key;
* the launcher flag needs BOTH profiles with BOTH manifests and refuses bypass / option kinds;
* the capband driver needs the ``mktprof`` name token;
* export refuses a gene-run row that lost the gene; an older job (no gene) is untouched.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

from app.services.strategy_optimization_handler import canonical_trial_params, mode_anchor_index
from app.services.strategy_param_space import (
    collect_param_space, decode_params, market_profile_members, neutralise_market_profile_genes,
)
from app.services.trial_memo import trial_key

_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")
_BOTH = ("ohlcv-v1", "ta-structure-v1")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_market_profile_gene", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_market_profile_gene"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()


def test_the_worktree_code_is_under_test():
    import app.services.strategy_param_space as sps
    assert os.path.realpath(sps.__file__).startswith(os.path.realpath(_TESTPLATFORM))
    print("launcher:", _LAUNCHER_PATH, "param space:", sps.__file__)


def _reset():
    _M._MARKET_CONDITION_PROFILES = ()
    _M._MARKET_CONDITION_MANIFESTS = {}
    _M._MARKET_EXIT_KINDS_SELECTED = ()
    _M._MARKET_CONDITION_MODE = _M._MARKET_CONDITION_MODE_SEARCHED
    _M._SEARCH_MARKET_PROFILE = False


def _strategy(search_profile: bool, kind="S3"):
    """A real S3 strategy as the launcher builds it for the goal2027 grid (both profiles, all market exits)."""
    _reset()
    _M._MARKET_CONDITION_PROFILES = _BOTH
    _M._MARKET_EXIT_KINDS_SELECTED = ("exit", "stop", "tp")
    _M._SEARCH_MARKET_PROFILE = search_profile
    try:
        return _M._build_strategy(kind, "t-mp", "FMPRating")
    finally:
        _reset()


@pytest.fixture(scope="module")
def strat_profile():
    return _strategy(True)


@pytest.fixture(scope="module")
def strat_enabled():
    return _strategy(False)


def _genome(space, **overrides):
    """A full genome (decoded VALUES) from a gene space: every choice at its first non-off option, every numeric
    gene at its min, every toggle on. ``overrides`` replace single keys."""
    flat = {}
    for key, spec in space.items():
        if spec.get("type") == "choice":
            ch = spec["choices"]
            flat[key] = ch[1] if key.endswith(":mode") else ch[-1] if key == "market:profile" else ch[0]
        elif key.endswith(":enabled"):
            flat[key] = 1
        else:
            flat[key] = spec["min"]
    flat.update(overrides)
    return flat


def _rules(decoded):
    return decoded["entry_rules"], decoded["exit_rules"]


def _leaf_ids(rules):
    out = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("field"):
                out.append(n["id"])
            for c in n.get("conditions") or []:
                walk(c)
    for r in rules or []:
        walk(r.get("conditions"))
    return out


# ----------------------------------------------------------------------------- gene space
class TestGeneSpace:
    def test_profile_gene_replaces_enabled_and_is_last(self, strat_profile, strat_enabled):
        sp = collect_param_space(strat_profile)
        assert "market:enabled" not in sp
        assert list(sp)[-1] == "market:profile"
        assert sp["market:profile"]["choices"] == ["off", "ohlcv-v1", "ta-structure-v1", "both"]
        assert sp["market:profile"]["max"] == 3

    def test_old_space_is_byte_identical_with_the_gene_swapped_out(self, strat_profile, strat_enabled):
        new = collect_param_space(strat_profile)
        old = collect_param_space(strat_enabled)
        assert list(old)[-1] == "market:enabled"
        assert {k: v for k, v in new.items() if k != "market:profile"} == \
               {k: v for k, v in old.items() if k != "market:enabled"}
        assert list(new)[:-1] == list(old)[:-1]

    def test_members_split_leaves_and_exit_rules_by_profile(self, strat_profile):
        m = market_profile_members(strat_profile)
        assert m["leaf_profile"]["s3-market-slope"] == "ohlcv-v1"
        assert m["leaf_profile"]["s3-market-structure"] == "ta-structure-v1"
        assert m["rule_profiles"]["s3-mkt-exit-structure"] == {"ta-structure-v1"}
        assert m["rule_profiles"]["s3-mkt-exit-slope"] == {"ohlcv-v1"}
        assert m["rule_profiles"]["s3-mkt-tp"] == {"ohlcv-v1"}


# ----------------------------------------------------------------------------- decode equivalences
class TestDecode:
    def test_off_equals_market_enabled_zero(self, strat_profile, strat_enabled):
        gp = _genome(collect_param_space(strat_profile), **{"market:profile": "off"})
        ge = _genome(collect_param_space(strat_enabled), **{"market:enabled": 0})
        gp.pop("market:profile"), ge.pop("market:enabled")
        a = decode_params(strat_profile, {**gp, "market:profile": "off"})
        b = decode_params(strat_enabled, {**ge, "market:enabled": 0})
        assert _rules(a) == _rules(b)
        assert a["expert_overrides"] == b["expert_overrides"]
        # and nothing market is left anywhere
        assert not [i for i in _leaf_ids(a["entry_rules"]) + _leaf_ids(a["exit_rules"]) if "mkt" in i or "market" in i]

    def test_both_equals_every_gene_as_decoded(self, strat_profile, strat_enabled):
        gp = _genome(collect_param_space(strat_profile), **{"market:profile": "both"})
        ge = _genome(collect_param_space(strat_enabled), **{"market:enabled": 1})
        a = decode_params(strat_profile, gp)
        gp2 = {k: v for k, v in gp.items() if k != "market:profile"}
        b = decode_params(strat_enabled, {**{k: v for k, v in ge.items() if k != "market:enabled"}, "market:enabled": 1})
        assert gp2 == {k: v for k, v in ge.items() if k not in ("market:enabled",)}
        assert _rules(a) == _rules(b)

    @pytest.mark.parametrize("sel,gone,kept", [
        ("ohlcv-v1", "ta-structure-v1", "ohlcv-v1"),
        ("ta-structure-v1", "ohlcv-v1", "ta-structure-v1"),
    ])
    def test_single_profile_equals_a_run_with_only_that_profiles_leaves(self, strat_profile, strat_enabled, sel, gone, kept):
        sp = collect_param_space(strat_profile)
        members = market_profile_members(strat_profile)
        gp = _genome(sp, **{"market:profile": sel})
        a = decode_params(strat_profile, gp)
        # the same run expressed with the OLD master gene on and the other profile's genes switched off by hand
        ge = {k: v for k, v in gp.items() if k != "market:profile"}
        ge["market:enabled"] = 1
        for cid, prof in members["leaf_profile"].items():
            if prof == gone and f"cond:{cid}:mode" in ge:
                ge[f"cond:{cid}:mode"] = "off"
        for rid, profs in members["rule_profiles"].items():
            if gone in profs:
                ge[f"exit:{rid}:enabled"] = 0
        b = decode_params(strat_enabled, ge)
        assert _rules(a) == _rules(b)
        ids = _leaf_ids(a["entry_rules"]) + _leaf_ids(a["exit_rules"])
        gone_ids = [c for c, p in members["leaf_profile"].items() if p == gone]
        kept_ids = [c for c, p in members["leaf_profile"].items() if p == kept]
        assert not set(gone_ids) & set(ids)
        assert set(kept_ids) & set(ids), "the selected profile's leaves must still be live"

    def test_s1_entry_rules_survive_every_profile(self):
        """S1's three tier entry rules carry a rule-level toggle AND the market leaves; only the market EXIT rules
        are profile-gated rules. (market:enabled=0 disables S1's entry rules -- see the plan's open question.)"""
        s1 = _strategy(True, "S1")
        gp = _genome(collect_param_space(s1))
        for sel in ("off", "ohlcv-v1", "ta-structure-v1", "both"):
            d = decode_params(s1, {**gp, "market:profile": sel})
            assert len(d["entry_rules"]) == 3, sel
        assert set(market_profile_members(s1)["rule_profiles"]) == {
            "s1-mkt-exit-structure", "s1-mkt-exit-slope", "s1-mkt-tp"}

    def test_exit_rules_follow_the_selected_profile(self, strat_profile):
        gp = _genome(collect_param_space(strat_profile))
        by = {}
        for sel in ("off", "ohlcv-v1", "ta-structure-v1", "both"):
            ex = decode_params(strat_profile, {**gp, "market:profile": sel})["exit_rules"]
            by[sel] = {r["id"] for r in ex if "mkt" in r["id"]}
        assert by["off"] == set()
        assert by["ohlcv-v1"] == {"s3-mkt-exit-slope", "s3-mkt-tp"}
        assert by["ta-structure-v1"] == {"s3-mkt-exit-structure"}
        assert by["both"] == by["ohlcv-v1"] | by["ta-structure-v1"]

    def test_both_master_genes_in_one_genome_are_refused(self, strat_profile):
        gp = _genome(collect_param_space(strat_profile))
        with pytest.raises(ValueError, match="market:enabled and market:profile"):
            decode_params(strat_profile, {**gp, "market:enabled": 1})

    def test_unknown_profile_value_is_refused(self, strat_profile):
        with pytest.raises(ValueError, match="market:profile"):
            decode_params(strat_profile, {"market:profile": "ta-structure-v2"})

    def test_the_marker_is_not_left_on_decoded_rules(self, strat_profile):
        d = decode_params(strat_profile, _genome(collect_param_space(strat_profile)))
        for r in d["entry_rules"] + d["exit_rules"]:
            assert "market_profile_gene" not in r and "market_master_gene" not in r


# ----------------------------------------------------------------------------- memo key
class TestMemoKey:
    @staticmethod
    def _key(strat, flat):
        anchors = mode_anchor_index(strat)
        members = market_profile_members(strat)
        return trial_key({"engine": "daily", "seed": 1,
                          "params": canonical_trial_params(anchors, flat, members)})

    def _scramble_ta(self, strat, gp):
        """Change every gene that belongs to the TA profile (mode, threshold, exit toggle, actions)."""
        m = market_profile_members(strat)
        out = dict(gp)
        for cid, prof in m["leaf_profile"].items():
            if prof != "ta-structure-v1":
                continue
            if f"cond:{cid}:mode" in out:
                out[f"cond:{cid}:mode"] = "above" if out[f"cond:{cid}:mode"] != "above" else "below"
            if f"cond:{cid}:value" in out:
                out[f"cond:{cid}:value"] = out[f"cond:{cid}:value"] + 1.0
        for rid, profs in m["rule_profiles"].items():
            if "ta-structure-v1" in profs:
                out[f"exit:{rid}:enabled"] = 0 if out.get(f"exit:{rid}:enabled") else 1
                for k in [k for k in out if k.startswith(f"exit:{rid}:a")]:
                    out[k] = out[k] + 1.0
        assert out != gp
        return out

    def test_genomes_differing_only_in_the_unselected_profile_share_a_key(self, strat_profile):
        gp = _genome(collect_param_space(strat_profile), **{"market:profile": "ohlcv-v1"})
        assert self._key(strat_profile, gp) == self._key(strat_profile, self._scramble_ta(strat_profile, gp))

    def test_off_ignores_every_market_gene(self, strat_profile):
        gp = _genome(collect_param_space(strat_profile), **{"market:profile": "off"})
        other = self._scramble_ta(strat_profile, gp)
        other["cond:s3-market-slope:mode"] = "below"
        assert self._key(strat_profile, gp) == self._key(strat_profile, other)

    def test_a_selected_profile_gene_still_changes_the_key(self, strat_profile):
        gp = _genome(collect_param_space(strat_profile), **{"market:profile": "ohlcv-v1"})
        other = {**gp, "cond:s3-market-slope:mode": "above"}
        assert gp["cond:s3-market-slope:mode"] != "above"
        assert self._key(strat_profile, gp) != self._key(strat_profile, other)

    def test_profiles_never_collide(self, strat_profile):
        gp = _genome(collect_param_space(strat_profile))
        keys = {self._key(strat_profile, {**gp, "market:profile": s}) for s in
                ("off", "ohlcv-v1", "ta-structure-v1", "both")}
        assert len(keys) == 4

    def test_canonicalisation_does_not_change_the_decoded_trial(self, strat_profile):
        gp = _genome(collect_param_space(strat_profile), **{"market:profile": "ohlcv-v1"})
        assert _rules(decode_params(strat_profile, gp)) == \
               _rules(decode_params(strat_profile, self._scramble_ta(strat_profile, gp)))

    def test_older_jobs_keep_the_identity_function(self, strat_enabled):
        ge = _genome(collect_param_space(strat_enabled), **{"market:enabled": 1})
        assert neutralise_market_profile_genes(ge, market_profile_members(strat_enabled)) is ge
        assert neutralise_market_profile_genes(ge, None) is ge
        assert canonical_trial_params({}, ge) is ge
        assert canonical_trial_params({}, ge, market_profile_members(strat_enabled)) is ge

    def test_profile_with_nothing_to_drop_returns_the_same_object(self, strat_profile):
        gp = _genome(collect_param_space(strat_profile), **{"market:profile": "both"})
        assert neutralise_market_profile_genes(gp, market_profile_members(strat_profile)) is gp


# ----------------------------------------------------------------------------- launcher flag
class TestLauncherFlag:
    def setup_method(self):
        _reset()

    teardown_method = setup_method

    def _ready(self, profiles=_BOTH, manifests=None, mode="searched"):
        _M._MARKET_CONDITION_PROFILES = tuple(profiles)
        _M._MARKET_CONDITION_MANIFESTS = {p: "d" * 64 for p in profiles} if manifests is None else manifests
        _M._MARKET_CONDITION_MODE = mode

    def test_off_by_default_and_always_reset(self):
        assert _M._resolve_search_market_profile(False, "optimize") is False
        assert _M._SEARCH_MARKET_PROFILE is False
        self._ready()
        assert _M._resolve_search_market_profile(True, "optimize") is True
        assert _M._resolve_search_market_profile(False, "optimize-batch") is False
        assert _M._SEARCH_MARKET_PROFILE is False

    def test_accepts_both_profiles_with_both_manifests(self):
        self._ready()
        assert _M._resolve_search_market_profile(True, "optimize") is True

    @pytest.mark.parametrize("profiles", [(), ("ohlcv-v1",), ("ta-structure-v1",)])
    def test_refuses_anything_but_both_profiles(self, profiles):
        self._ready(profiles)
        with pytest.raises(SystemExit) as e:
            _M._resolve_search_market_profile(True, "optimize")
        assert "--search-market-profile" in str(e.value) and "ohlcv-v1" in str(e.value)

    def test_refuses_a_missing_manifest(self):
        self._ready(manifests={"ohlcv-v1": "d" * 64})
        with pytest.raises(SystemExit) as e:
            _M._resolve_search_market_profile(True, "optimize")
        assert "manifest" in str(e.value)

    def test_refuses_the_all_off_control_arm(self):
        self._ready(mode="all-off")
        with pytest.raises(SystemExit):
            _M._resolve_search_market_profile(True, "optimize")

    def test_refused_on_bypass_and_option_kinds(self):
        self._ready()
        _M._resolve_search_market_profile(True, "optimize")
        with pytest.raises(SystemExit):
            _M._refuse_market_profile_gene_unsupported("optimize", "FACTOR", bypass=True)
        with pytest.raises(SystemExit):
            _M._refuse_market_profile_gene_unsupported("optimize", "O_LC", bypass=False)
        _M._refuse_market_profile_gene_unsupported("optimize", "S3", bypass=False)    # no raise
        _M._resolve_search_market_profile(False, "optimize")
        _M._refuse_market_profile_gene_unsupported("optimize", "FACTOR", bypass=True)  # flag off: no raise

    def test_builder_stamps_the_marker_only_under_the_flag(self):
        on = _strategy(True)
        off = _strategy(False)
        assert any(r.get("market_profile_gene") for r in on.entry_rules)
        assert any(r.get("market_profile_gene") for r in on.exit_rules if "mkt" in r["id"])
        assert not any(r.get("market_profile_gene") for r in off.entry_rules + off.exit_rules)
        assert all(r.get("market_master_gene") for r in off.entry_rules if r.get("market_master_gene") is not None)


# ----------------------------------------------------------------------------- capband driver
_DRIVER = os.path.join(os.path.dirname(_TESTPLATFORM), "tools", "run_screener_capband_matrix.py")


@pytest.fixture(scope="module")
def driver():
    spec = importlib.util.spec_from_file_location("capband_market_profile_gene", _DRIVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestDriver:
    def test_forwarded_to_classic_jobs_only(self, driver):
        on = SimpleNamespace(search_market_profile=True)
        off = SimpleNamespace(search_market_profile=False)
        assert driver.market_profile_gene_tokens(on, "S1") == ["--search-market-profile"]
        assert driver.market_profile_gene_tokens(on, None) == []
        assert driver.market_profile_gene_tokens(off, "S1") == []

    @staticmethod
    def _run(*argv):
        import subprocess
        return subprocess.run([sys.executable, _DRIVER, *argv, "--dry-run"], capture_output=True, text=True)

    _BASE = ("--market-condition-profile", "ohlcv-v1,ta-structure-v1",
             "--market-condition-manifest", "ohlcv-v1=" + "a" * 64 + ",ta-structure-v1=" + "b" * 64)

    def test_requires_the_mktprof_name_token(self):
        r = self._run("--search-market-profile", *self._BASE, "--name-suffix", "plain")
        assert r.returncode != 0 and "mktprof" in r.stderr

    def test_requires_both_profiles(self):
        r = self._run("--search-market-profile", "--market-condition-profile", "ohlcv-v1",
                      "--market-condition-manifest", "a" * 64, "--name-suffix", "x-mktprof")
        assert r.returncode != 0 and "ta-structure-v1" in r.stderr

    def test_requires_both_manifests(self):
        r = self._run("--search-market-profile", "--market-condition-profile", "ohlcv-v1,ta-structure-v1",
                      "--market-condition-manifest", "ohlcv-v1=" + "a" * 64, "--name-suffix", "x-mktprof")
        assert r.returncode != 0 and "manifest" in r.stderr

    def test_refuses_the_all_off_arm(self):
        r = self._run("--search-market-profile", *self._BASE, "--market-condition-mode", "all-off",
                      "--name-suffix", "x-mktprof")
        assert r.returncode != 0 and "all-off" in r.stderr

    def test_token_and_flag_change_the_job_name_digest(self):
        a = self._run("--search-market-profile", *self._BASE, "--name-suffix", "x-mktprof")
        b = self._run(*self._BASE, "--name-suffix", "x-mktprof")
        assert a.returncode == 0, a.stderr
        assert b.returncode == 0, b.stderr
        assert a.stdout != b.stdout


# ----------------------------------------------------------------------------- export
class TestExport:
    @pytest.fixture(autouse=True)
    def _db(self):
        from app.models.database import Base, engine
        Base.metadata.create_all(bind=engine)

    @staticmethod
    def _opt_and_bt(strategy_params, ranges):
        from datetime import datetime
        from app.models.backtest import Backtest
        from app.models.database import SessionLocal
        from app.models.strategy import Strategy
        from app.models.strategy_optimization import StrategyOptimization
        db = SessionLocal()
        strat = Strategy(name="mp-strat", entry_rules=[], exit_rules=[])
        db.add(strat); db.commit(); db.refresh(strat)
        opt = StrategyOptimization(
            strategy_id=strat.id, name="mp-opt", fitness_metric="sharpe", optimization_type="genetic",
            optimization_config={"backtest": {"backtest_id": 1, "enabled_instruments": ["AAPL"],
                                              "experts": [{"class": "FMPRating", "settings": {}}],
                                              "account_settings": {}, "warmup_days": 30, "seed": 42}},
            parameter_ranges=ranges, all_results=[], best_params={}, best_fitness=1.0, status="completed")
        db.add(opt); db.commit(); db.refresh(opt)
        bt = Backtest(name="TOP1-mp", expert_name="FMPRating", engine_type="daily_expert",
                      strategy_params=strategy_params, start_date=datetime(2024, 1, 1),
                      end_date=datetime(2024, 6, 1), initial_capital=10_000.0, optimization_id=opt.id)
        return db, bt

    def test_gene_run_row_without_the_gene_is_refused(self):
        from fastapi import HTTPException
        from app.api.backtests import _derive_export_payload
        db, bt = self._opt_and_bt({"model:risk_per_trade_pct": 2.0}, {"market:profile": {"type": "choice"}})
        try:
            with pytest.raises(HTTPException) as e:
                _derive_export_payload(bt, "expert_settings", db)
            assert "market:profile" in str(e.value.detail)
        finally:
            db.close()

    def test_gene_run_row_with_the_gene_passes_the_check(self):
        from app.api.backtests import _refuse_market_profile_row_without_gene
        db, bt = self._opt_and_bt({"market:profile": "ohlcv-v1"}, {"market:profile": {"type": "choice"}})
        try:
            _refuse_market_profile_row_without_gene(bt, db)
        finally:
            db.close()

    def test_old_run_without_the_gene_in_its_space_is_untouched(self):
        from app.api.backtests import _refuse_market_profile_row_without_gene
        db, bt = self._opt_and_bt({"model:risk_per_trade_pct": 2.0}, {"market:enabled": {"type": "int"}})
        try:
            _refuse_market_profile_row_without_gene(bt, db)
        finally:
            db.close()

    def test_the_decoded_genome_with_off_leaves_is_what_a_stored_row_decodes_to(self, strat_profile):
        gp = _genome(collect_param_space(strat_profile), **{"market:profile": "ta-structure-v1"})
        d = decode_params(strat_profile, gp)       # what the export (and a re-run) feeds the deploy payload
        ids = {r["id"] for r in d["exit_rules"]}
        assert "s3-mkt-exit-structure" in ids and "s3-mkt-tp" not in ids
        assert "s3-market-slope" not in _leaf_ids(d["entry_rules"])
        assert "s3-market-structure" in _leaf_ids(d["entry_rules"])


# ----------------------------------------------------------------------------- market:enabled entry-rule fix
class TestMarketEnabledKeepsEntryRules:
    """S1's tier entry rules carry a rule-level toggle AND the market leaves among their ordinary gates.
    market:enabled == 0 used to disable those whole rules (0 entries decoded for S1)."""

    @pytest.mark.parametrize("kind", ["S1", "S2", "S3", "S4", "S5", "S6", "S7"])
    def test_enabled_zero_keeps_entry_rules_with_market_leaves_off(self, kind):
        s = _strategy(False, kind)
        g = _genome(collect_param_space(s))
        on = decode_params(s, {**g, "market:enabled": 1})
        off = decode_params(s, {**g, "market:enabled": 0})
        assert len(off["entry_rules"]) == len(on["entry_rules"]) > 0
        members = market_profile_members(s)
        assert not set(members["leaf_profile"]) & set(_leaf_ids(off["entry_rules"]) + _leaf_ids(off["exit_rules"]))
        assert set(members["leaf_profile"]) & set(_leaf_ids(on["entry_rules"]))
        assert not [r for r in off["exit_rules"] if "mkt" in r["id"]]

    def test_s1_profile_off_equals_enabled_zero(self, strat_profile):
        s1p, s1e = _strategy(True, "S1"), _strategy(False, "S1")
        gp = _genome(collect_param_space(s1p), **{"market:profile": "off"})
        ge = {k: v for k, v in gp.items() if k != "market:profile"}
        a = decode_params(s1p, gp)
        b = decode_params(s1e, {**ge, "market:enabled": 0})
        assert _rules(a) == _rules(b) and len(a["entry_rules"]) == 3


# ----------------------------------------------------------------------------- registry coverage / validation
class TestRegistryCoverage:
    def test_every_registered_profile_is_covered_by_the_choices(self):
        from app.services.strategy_param_space import assert_market_profiles_covered
        assert_market_profiles_covered()      # today: ohlcv-v1 and ta-structure-v1 only

    def test_a_third_profile_fails_loudly(self):
        from ba2_common.core.market_conditions import FieldSpec, ProfileSpec, registered_profile
        from app.services.strategy_param_space import assert_market_profiles_covered
        spec = ProfileSpec(name="third-v1", calc_version="t/1", fields=(
            FieldSpec(name="third_state", kind="categorical", short="third3", searched=True,
                      codes={"a": 1, "b": 2}, ui_name="t"),))
        with registered_profile(spec):
            with pytest.raises(ValueError, match="third-v1"):
                assert_market_profiles_covered()
            with pytest.raises(ValueError, match="third-v1"):
                market_profile_members(_strategy_template_only())

    def test_members_only_computed_when_the_space_has_the_gene(self, strat_profile, strat_enabled):
        from app.services.strategy_optimization_handler import market_members_for_space
        assert market_members_for_space(strat_enabled, collect_param_space(strat_enabled)) is None
        assert market_members_for_space(strat_profile, collect_param_space(strat_profile)) is not None


def _strategy_template_only():
    return SimpleNamespace(entry_rules=[], exit_rules=[])


class TestExportValidatesTheValue:
    @pytest.fixture(autouse=True)
    def _db(self):
        from app.models.database import Base, engine
        Base.metadata.create_all(bind=engine)

    @pytest.mark.parametrize("bad", ["ta-structure-v2", 1, ""])
    def test_an_unknown_stored_value_is_refused(self, bad):
        from fastapi import HTTPException
        from app.api.backtests import _refuse_market_profile_row_without_gene
        db, bt = TestExport._opt_and_bt({"market:profile": bad}, {"market:profile": {"type": "choice"}})
        try:
            with pytest.raises(HTTPException) as e:
                _refuse_market_profile_row_without_gene(bt, db)
            assert "market:profile" in str(e.value.detail)
        finally:
            db.close()


# ----------------------------------------------------------------------------- export writes only the selected profiles
class TestExportServesOnlyTheSelectedProfiles:
    _EXPECT = {"off": "", "ohlcv-v1": "ohlcv-v1", "ta-structure-v1": "ta-structure-v1",
               "both": "ohlcv-v1,ta-structure-v1"}

    @staticmethod
    def _settings(sp):
        from datetime import datetime
        from app.api.backtests import _derive_export_payload
        from app.models.backtest import Backtest
        bt = Backtest(name="TOP1-mp", expert_name="FMPRating", engine_type="daily_expert",
                      strategy_params=sp, start_date=datetime(2024, 1, 1),
                      end_date=datetime(2024, 6, 1), initial_capital=10_000.0, optimization_id=None)
        return _derive_export_payload(bt, "expert_settings", db=None)["settings"]["expert_params"]

    @pytest.mark.parametrize("value", list(_EXPECT))
    def test_each_value_exports_the_right_profile_setting(self, value):
        sp = {"market:profile": value, "model:risk_per_trade_pct": 2.0,
              "expertFixedSettings": {"market_condition_profile": "ohlcv-v1,ta-structure-v1"}}
        assert self._settings(sp)["market_condition_profile"] == self._EXPECT[value]

    @pytest.mark.parametrize("value", list(_EXPECT))
    def test_the_exported_ruleset_is_served_by_exactly_that_profile_set(self, strat_profile, value):
        from app.services.backtest.seam_wiring import (
            assert_each_expert_serves_its_gates, market_condition_leaf_fields_in)
        from ba2_common.core.market_condition_rules import assert_fields_served, parse_profile_setting
        from ba2_common.core.market_conditions import PROFILES
        d = decode_params(strat_profile, _genome(collect_param_space(strat_profile), **{"market:profile": value}))
        used = market_condition_leaf_fields_in({"entry_rules": d["entry_rules"], "exit_rules": d["exit_rules"]})
        setting = self._EXPECT[value]
        profiles = parse_profile_setting(setting)
        assert_fields_served(used, profiles, where="export")
        assert_each_expert_serves_its_gates({"entry_rules": d["entry_rules"], "exit_rules": d["exit_rules"],
                                             "experts": [{"class": "FMPRating",
                                                          "settings": {"market_condition_profile": setting}}]})
        if value == "off":
            assert used == []
        else:   # exactly: every selected profile is actually read, nothing else is
            read = {p for p, spec in PROFILES.items() for f in spec.fields if f.name in {u[1] for u in used}}
            assert read <= set(profiles)

    def test_old_rows_keep_their_stored_setting(self):
        sp = {"model:risk_per_trade_pct": 2.0,
              "expertFixedSettings": {"market_condition_profile": "ohlcv-v1,ta-structure-v1"}}
        assert self._settings(sp)["market_condition_profile"] == "ohlcv-v1,ta-structure-v1"
