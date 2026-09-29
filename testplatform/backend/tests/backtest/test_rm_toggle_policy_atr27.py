"""``--rm-toggle-policy atr-searched`` -- the 2027 ATR grid's run-level policy.

docs/strategy_research/atr_grid/atr_grid_2027_design.md §3.2. The DEFAULT policy ("pinned") must
stay byte-identical to today -- ``test_inert_rm_toggles_stay_off.py`` is the (unmodified) control
for that. This file covers the NEW policy: unpinning ``use_atr_stop`` only, persisted on the run
so every path that rebuilds a trial from the stored config sees it, refused for any other key,
folded into the checkpoint fingerprint, gated on the job name containing '-atr27', and a
search-space shape (D3/D4/D8) that drops the regime scale genes and the weekend schedule genes.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 -- registers every model class on Base.metadata
from app.models.database import Base
from app.models.backtest import Backtest
from app.models.strategy import Strategy as StrategyModel
from app.models.strategy_optimization import StrategyOptimization
from app.services.strategy_param_space import (
    ALLOWED_RM_TOGGLES_UNPINNED, INERT_RM_TOGGLES, pinned_rm_toggles,
)
from app.services.strategy_optimization_handler import (
    _build_daily_trial_config, checkpoint_fingerprint,
)

# tests/backtest/ -> tests/ -> backend/ -> testplatform/, then the launcher beside backend/.
_TESTPLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_LAUNCHER_PATH = os.path.join(_TESTPLATFORM, "ba2test_launcher.py")


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_atr27", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_atr27"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


_M = _launcher()


def _bt_cfg(**overrides):
    cfg = {
        "backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
        "enabled_instruments": ["AAPL"], "initial_capital": 10000.0, "warmup_days": 0,
        "seed": 42, "account_settings": {},
        "experts": [{"class": "FMPRating", "settings": {"sizing_mode": "risk_atr"}}],
    }
    cfg.update(overrides)
    return cfg


def _decoded(**expert_overrides):
    return {"expert_overrides": expert_overrides, "screener_overrides": {},
            "schedule_days": None, "entry_rules": None, "exit_rules": None}


# ==================================================================================================
# pinned_rm_toggles / _pinned_rm_toggles (backend + launcher mirrors)
# ==================================================================================================
class TestPinnedRmToggles:
    def test_none_and_empty_are_the_pinned_default(self):
        assert pinned_rm_toggles(None) == INERT_RM_TOGGLES
        assert pinned_rm_toggles([]) == INERT_RM_TOGGLES
        assert _M._pinned_rm_toggles(None) == _M._INERT_RM_TOGGLES
        assert _M._pinned_rm_toggles([]) == _M._INERT_RM_TOGGLES

    def test_unpinning_use_atr_stop_drops_only_that_key(self):
        out = pinned_rm_toggles(["use_atr_stop"])
        assert out == {"regime_overlay_enabled": False}
        out2 = _M._pinned_rm_toggles(["use_atr_stop"])
        assert out2 == {"regime_overlay_enabled": False}

    def test_regime_overlay_enabled_can_never_be_unpinned(self):
        """THE POINT of the allowlist: only use_atr_stop may ever be unpinned."""
        assert ALLOWED_RM_TOGGLES_UNPINNED == frozenset({"use_atr_stop"})
        with pytest.raises(ValueError):
            pinned_rm_toggles(["regime_overlay_enabled"])
        with pytest.raises(ValueError):
            _M._pinned_rm_toggles(["regime_overlay_enabled"])

    def test_any_unknown_key_is_refused_too(self):
        with pytest.raises(ValueError):
            pinned_rm_toggles(["risk_per_trade_pct"])
        with pytest.raises(ValueError):
            _M._pinned_rm_toggles(["risk_per_trade_pct"])


# ==================================================================================================
# decode 1 -> True, 0 -> False, through _build_daily_trial_config (the GA-trial AND the re-run path)
# ==================================================================================================
class TestPolicyRunDecodesTheGene:
    """``_build_daily_trial_config`` itself only STOPS re-pinning the gene under the policy --
    it does not coerce the raw decoded value (an ``int`` off a ``type: int`` GA gene, exactly as
    ``decode_params``/``expert_overrides`` produce it) to ``bool``. That coercion happens where
    the setting is READ (``TradeRiskManagement._ensure_safeguard_stop`` via ``coerce_bool`` --
    see packages/common/tests/test_ensure_safeguard_stop.py). So "decodes gene 1 -> True" is
    checked end to end here: the raw gene survives unpinned, AND coerce_bool reads it as the
    right bool -- the two halves of "all the way into the trial's expert settings"."""

    def test_gene_1_decodes_to_true(self):
        from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool

        backtest_cfg = _bt_cfg(rm_toggles_unpinned=["use_atr_stop"])
        decoded = _decoded(use_atr_stop=1, risk_per_trade_pct=2.5)
        cfg = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
        settings = cfg["experts"][0]["settings"]
        assert coerce_bool(settings["use_atr_stop"]) is True
        # regime_overlay_enabled stays pinned off -- not in rm_toggles_unpinned.
        assert settings["regime_overlay_enabled"] is False
        assert settings["risk_per_trade_pct"] == 2.5

    def test_gene_0_decodes_to_false(self):
        from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool

        backtest_cfg = _bt_cfg(rm_toggles_unpinned=["use_atr_stop"])
        decoded = _decoded(use_atr_stop=0)
        cfg = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
        assert coerce_bool(cfg["experts"][0]["settings"]["use_atr_stop"]) is False

    def test_a_persisted_policy_config_rerun_unpins_atr_too(self):
        """THE persisted-TOP-N bug class: a re-run rebuilds the trial from the run's STORED
        optimization_config['backtest'] exactly as tools/recover_missing_topn.py,
        tools/rerun_dev_deployed_on_worker.py and rerun_handler.py all do (``bt_block =
        dict(cfg["backtest"])``) -- so the policy must still be honoured on that path, not only
        on the live GA trial that first decoded it."""
        from ba2_common.core.interfaces.ExtendableSettingsInterface import coerce_bool

        stored_run_cfg = {"populationSize": 10, "generations": 1, "seed": 1,
                          "backtest": _bt_cfg(rm_toggles_unpinned=["use_atr_stop"])}
        bt_block = dict(stored_run_cfg["backtest"])   # exactly what the tools do
        decoded = _decoded(use_atr_stop=1)
        trial_cfg = _build_daily_trial_config(bt_block, decoded, None, option_trade_records=True)
        assert coerce_bool(trial_cfg["experts"][0]["settings"]["use_atr_stop"]) is True

    def test_the_string_shaped_case_docstring_encoder_reads_correctly(self):
        """A stored genome from before coerce_bool existed can carry the JSON string "1" --
        this must decode the same as the int 1 once merged onto expert settings and read by
        coerce_bool downstream (see TradeRiskManagement's own bool-read test); this test only
        pins that _build_daily_trial_config does not itself mangle the value."""
        backtest_cfg = _bt_cfg(rm_toggles_unpinned=["use_atr_stop"])
        decoded = _decoded(use_atr_stop="1")
        cfg = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
        # expert_overrides win over the pin (unchanged from before this policy existed) -- the
        # raw string rides through untouched; TradeRiskManagement.coerce_bool is what reads it.
        assert cfg["experts"][0]["settings"]["use_atr_stop"] == "1"


class TestThePinnedDefaultStaysPinned:
    """The other half of the control: a backtest_cfg with NO rm_toggles_unpinned key (every job
    before this policy existed, and every --rm-toggle-policy pinned run) behaves exactly as
    test_inert_rm_toggles_stay_off.py already pins -- restated here so this file is a complete
    read of the policy's two branches."""

    def test_a_decoded_gene_saying_on_still_does_not_win(self):
        backtest_cfg = _bt_cfg()   # no rm_toggles_unpinned key at all
        decoded = _decoded(use_atr_stop=1, regime_overlay_enabled=1)
        cfg = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
        settings = cfg["experts"][0]["settings"]
        assert settings["use_atr_stop"] is False
        assert settings["regime_overlay_enabled"] is False

    def test_an_explicit_empty_list_is_the_same_as_absent(self):
        backtest_cfg = _bt_cfg(rm_toggles_unpinned=[])
        decoded = _decoded(use_atr_stop=1)
        cfg = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
        assert cfg["experts"][0]["settings"]["use_atr_stop"] is False


class TestAPolicyRunCannotUnpinTheOverlay:
    def test_build_daily_trial_config_refuses_it(self):
        backtest_cfg = _bt_cfg(rm_toggles_unpinned=["regime_overlay_enabled"])
        decoded = _decoded()
        with pytest.raises(ValueError):
            _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)


# ==================================================================================================
# checkpoint fingerprint
# ==================================================================================================
def test_checkpoint_fingerprint_differs_between_policies():
    param_space = {"model:risk_per_trade_pct": {"type": "float", "min": 0.5, "max": 10.0, "step": 0.5}}
    ga = {"populationSize": 60, "generations": 20}
    pinned = checkpoint_fingerprint(param_space, ga, None, None)
    unpinned = checkpoint_fingerprint(param_space, ga, None, ["use_atr_stop"])
    assert pinned != unpinned
    # Same policy, same everything else -> same fingerprint (determinism, not just "differs").
    assert checkpoint_fingerprint(param_space, ga, None, ["use_atr_stop"]) == unpinned
    # Absent and empty must be the SAME fingerprint as before this parameter existed.
    assert checkpoint_fingerprint(param_space, ga) == pinned


# ==================================================================================================
# the -atr27 job-name guard
# ==================================================================================================
class TestTheAtr27NameGuard:
    def test_pinned_policy_is_unrestricted(self):
        _M._refuse_atr_policy_without_job_name("pinned", None)
        _M._refuse_atr_policy_without_job_name("pinned", "opt-FMPRating-S1")

    @pytest.mark.parametrize("name", [None, "", "opt-FMPRating-S1", "opt-atr-S1"])
    def test_atr_searched_refuses_a_name_without_the_token(self, name):
        with pytest.raises(SystemExit):
            _M._refuse_atr_policy_without_job_name("atr-searched", name)

    @pytest.mark.parametrize("name", ["opt-FMPRating-S1-atr27", "phase1-atr27-large-FMPRating-S1"])
    def test_atr_searched_accepts_a_name_with_the_token(self, name):
        _M._refuse_atr_policy_without_job_name("atr-searched", name)  # must not raise


# ==================================================================================================
# search space under the policy (D3/D4/D8)
# ==================================================================================================
class TestSearchSpaceUnderThePolicy:
    def test_use_atr_stop_is_now_searched(self):
        block = _M._rm_opt_for("S1", atr_searched=True)
        assert block["use_atr_stop"] == {"optimize": True, "min": 0, "max": 1, "step": 1,
                                         "type": "int"}

    def test_atr_multiplier_floor_is_widened_per_d4(self):
        block = _M._rm_opt_for("S1", atr_searched=True)
        assert block["atr_multiplier"] == {"optimize": True, "min": 1.5, "max": 6.0,
                                           "step": 0.5, "type": "float"}
        # atr_period is UNCHANGED -- it was always searched, just inert.
        assert block["atr_period"] == _M._RM_OPT["atr_period"]

    def test_the_three_regime_scale_genes_are_dropped(self):
        block = _M._rm_opt_for("S1", atr_searched=True)
        for gene in ("regime_risk_scale", "regime_stop_scale", "regime_tp_scale"):
            assert gene not in block

    def test_the_pinned_policy_rm_opt_is_unaffected(self):
        """atr_searched=False (the default) must be byte-identical to _RM_OPT."""
        assert _M._rm_opt_for("S1") == _M._rm_opt_for("S1", atr_searched=False)
        assert _M._rm_opt_for("S1")["use_atr_stop"]["optimize"] is False
        for gene in ("regime_risk_scale", "regime_stop_scale", "regime_tp_scale"):
            assert gene in _M._rm_opt_for("S1")

    def test_collected_param_space_matches_the_gene_dict(self):
        from app.services.strategy_param_space import collect_param_space

        space = collect_param_space(None, expert_cfg=_M._rm_opt_for("S1", atr_searched=True))
        assert "model:use_atr_stop" in space
        assert space["model:use_atr_stop"] == {"type": "int", "min": 0, "max": 1, "step": 1}
        for gene in ("regime_risk_scale", "regime_stop_scale", "regime_tp_scale"):
            assert f"model:{gene}" not in space
        # the still-live RM genes are untouched
        for gene in ("risk_per_trade_pct", "atr_multiplier", "atr_period", "min_stop_loss_pct",
                    "max_virtual_equity_per_instrument_percent"):
            assert f"model:{gene}" in space

    def test_weekend_schedule_genes_are_absent_under_the_policy(self):
        assert set(_M._WEEKDAY_SCHEDULE_DAY_OPT) == {
            "monday", "tuesday", "wednesday", "thursday", "friday"}
        assert "saturday" not in _M._WEEKDAY_SCHEDULE_DAY_OPT
        assert "sunday" not in _M._WEEKDAY_SCHEDULE_DAY_OPT
        # the pinned policy's own constant is untouched (all 7 days).
        assert set(_M._SCHEDULE_DAY_OPT) == {
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}

    def test_dropping_the_weekend_genes_from_expert_params_drops_them_from_the_search(self):
        """Exactly what the launcher merges into expert_params under the policy: schedule:<day>
        for weekdays only. decode_params must then default saturday/sunday to False via its
        existing schedule_by_day.get(day, False) repair -- no separate change needed there."""
        from app.services.strategy_param_space import collect_param_space, decode_params

        schedule_cfg = {k: v for k, v in _M._WEEKDAY_SCHEDULE_DAY_OPT.items()}
        space = collect_param_space(None, expert_cfg={"risk_per_trade_pct": _M._RM_OPT[
            "risk_per_trade_pct"]}, schedule_cfg=schedule_cfg)
        assert "schedule:saturday" not in space and "schedule:sunday" not in space
        assert "schedule:monday" in space


# ==================================================================================================
# exporter: carries the gene, refuses the absent case on a policy run
# ==================================================================================================
@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("atr27db") / "atr27.sqlite"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _persist_policy_row(db, *, model_genes, rm_toggles_unpinned):
    srow = StrategyModel(name="atr27-row", entry_rules=[], exit_rules=[])
    db.add(srow); db.commit(); db.refresh(srow)
    bt_block = {
        "experts": [{"class": "FMPRating", "settings": {"allow_automated_trade_opening": True}}],
        "rm_toggles_unpinned": rm_toggles_unpinned,
        "account_settings": {}, "enabled_instruments": ["AAPL"],
    }
    opt = StrategyOptimization(
        strategy_id=srow.id, name="opt-atr27", fitness_metric="calmar_ratio",
        optimization_type="genetic", status="completed",
        optimization_config={"populationSize": 10, "generations": 1, "seed": 1,
                             "backtest": bt_block},
    )
    db.add(opt); db.commit(); db.refresh(opt)
    bt = Backtest(
        name="TOP1-atr27", expert_name="FMPRating", engine_type="daily_expert",
        status="completed", start_date=datetime(2024, 2, 1), end_date=datetime(2024, 6, 1),
        initial_capital=20_000.0, optimization_id=opt.id,
        strategy_params=dict(model_genes),
    )
    db.add(bt); db.commit(); db.refresh(bt)
    return bt


class TestTheExporterUnderThePolicy:
    def test_the_gene_is_carried_when_present(self, db):
        from app.api.backtests import _derive_export_payload

        bt = _persist_policy_row(
            db, model_genes={"model:use_atr_stop": 1}, rm_toggles_unpinned=["use_atr_stop"])
        payload = _derive_export_payload(bt, "expert_settings", db)
        assert payload["settings"]["expert_params"]["use_atr_stop"] is True

    def test_absent_on_a_policy_run_raises_rather_than_exporting_false(self, db):
        from app.api.backtests import _derive_export_payload

        bt = _persist_policy_row(
            db, model_genes={}, rm_toggles_unpinned=["use_atr_stop"])
        with pytest.raises(ValueError):
            _derive_export_payload(bt, "expert_settings", db)

    def test_absent_on_a_pinned_run_still_exports_false_unchanged(self, db):
        """Every existing row (no policy at all) must be completely unaffected."""
        from app.api.backtests import _derive_export_payload

        bt = _persist_policy_row(db, model_genes={}, rm_toggles_unpinned=[])
        payload = _derive_export_payload(bt, "expert_settings", db)
        assert payload["settings"]["expert_params"]["use_atr_stop"] is False
