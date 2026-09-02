"""Task 11: FMPEarningsEvent's gene table -- three weights + min_analysts +
allow_unconfirmed_dates, wired into ``_EXPERT_OPT`` and threaded through
``_build_daily_trial_config``'s expert settings.

THE WHITELIST TRAP. ``daily_backtest_handler._expert_decision_settings`` (the function
``_build_experts`` actually calls to turn a trial's per-expert ``settings`` overrides into
the dict the CLASS reads) walks ONLY ``expert_cls._SETTING_KEYS`` -- a key silently dropped
from that tuple is dropped from the effective settings too, and every log up to that point
(the launcher's gene table, ``collect_param_space``, ``decode_params``,
``_build_daily_trial_config``'s echoed ``experts[i]['settings']``) still claims the value
"reached" the expert. A test that stops at any of those earlier dicts cannot see this.

Each gene below is proven end-to-end with the SAME calls the real backtest path makes:
    genome (``model:<setting>``) -> decode_params -> _build_daily_trial_config
    -> _expert_decision_settings(FMPEarningsEvent, that trial's settings overrides)
    -> the CLASS's effective setting equals the genome's (non-default) value.

Mutation executed (see the report): one key removed from
``FMPEarningsEvent._SETTING_KEYS`` by FILE COPY (never ``git checkout --``) kills every
``test_gene_reaches_the_effective_expert_setting`` case for that key.
"""
import importlib.util
import os
import sys

import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

from app.services.backtest.daily_backtest_handler import _expert_decision_settings  # noqa: E402
from app.services.strategy_optimization_handler import _build_daily_trial_config  # noqa: E402
from app.services.strategy_param_space import collect_param_space, decode_params  # noqa: E402
from ba2_experts.FMPEarningsEvent import FMPEarningsEvent  # noqa: E402

#: (setting name, a NON-DEFAULT value the genome carries). Defaults: w_* = 1.0,
#: min_analysts = 1 (Task 11), allow_unconfirmed_dates = False.
GENES = [
    ("w_hist_move", 1.75),
    ("w_surprise_vol", 0.25),
    ("w_vol_cheapness", 2.0),
    ("min_analysts", 4),
    ("min_analysts", 0),          # the special "gate OFF" value, not just "a different int"
    ("allow_unconfirmed_dates", True),
]
GENE_IDS = [f"{name}={val}" for name, val in GENES]


def _trial_settings_for(setting: str, value) -> dict:
    """Run the REAL chain a trial takes: _EXPERT_OPT's spec -> collect_param_space (so a
    gene missing from the launcher's spec is caught HERE, not just by reading the dict) ->
    decode -> _build_daily_trial_config -> the experts[0]['settings'] dict
    daily_backtest_handler._build_experts receives as ``overrides``."""
    strategy = mod._build_strategy_option("O_ERN")
    opt_spec = mod._EXPERT_OPT["FMPEarningsEvent"]["expert_params"]
    space = collect_param_space(strategy, expert_cfg=opt_spec)
    assert f"model:{setting}" in space, (
        f"{setting} is not collected into the joint param space from "
        f"_EXPERT_OPT['FMPEarningsEvent']['expert_params'] -- THE WHITELIST TRAP")
    decoded = decode_params(strategy, {f"model:{setting}": value})
    backtest_cfg = {
        "backtest_id": "gene-chain", "start_date": "2024-02-01", "end_date": "2024-06-01",
        "enabled_instruments": ["AAPL"],
        "experts": [{"class": "FMPEarningsEvent", "settings": {}}],
        "initial_capital": 20_000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "options_store": "parquet",
    }
    trial = _build_daily_trial_config(backtest_cfg, decoded, None)
    return trial["experts"][0]["settings"]


@pytest.mark.parametrize("setting,value", GENES, ids=GENE_IDS)
def test_the_launcher_declares_the_gene(setting, value):
    """The gene table itself: the setting is a searched ``model:*`` param on
    ``_EXPERT_OPT['FMPEarningsEvent']``."""
    space = mod._EXPERT_OPT["FMPEarningsEvent"]["expert_params"]
    assert setting in space, f"{setting} is not a searched gene for FMPEarningsEvent"
    assert space[setting]["optimize"] is True


@pytest.mark.parametrize("setting,value", GENES, ids=GENE_IDS)
def test_gene_survives_the_trial_config_WHITELIST(setting, value):
    """The value the GA decoded for one individual reaches
    ``_build_daily_trial_config``'s per-trial expert settings dict."""
    settings = _trial_settings_for(setting, value)
    assert settings[setting] == value


@pytest.mark.parametrize("setting,value", GENES, ids=GENE_IDS)
def test_gene_reaches_the_effective_expert_setting(setting, value):
    """THE LAST LINK: ``_expert_decision_settings`` -- the function
    ``daily_backtest_handler._build_experts`` actually calls -- must resolve the CLASS's
    effective setting to the genome's value, not silently fall back to the class default."""
    overrides = _trial_settings_for(setting, value)
    decision_settings = _expert_decision_settings(FMPEarningsEvent, overrides)
    assert decision_settings[setting] == value


def test_min_analysts_default_is_now_one_not_three():
    """Task 11 amendment: 1 is a data-quality floor, not a selection filter -- the grid's own
    0-5 gene (0 = gate off) searches the selection question."""
    defs = FMPEarningsEvent.get_settings_definitions()
    assert defs["min_analysts"]["default"] == 1


@pytest.mark.parametrize("other_expert", [
    "FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy",
    "DeterministicScorer", "FMPSenateTraderWeight",
])
def test_no_other_experts_gene_table_carries_the_new_genes(other_expert):
    """Genes apply ONLY when the job's expert is FMPEarningsEvent -- another expert's genome
    has none of them. (FMPRating carries its OWN, unrelated ``min_analysts`` gene -- an
    analyst-consensus floor with a 5-25 range, not this expert's 0-5 data-quality gate --
    which is expected and does not violate the pin.)"""
    other_space = mod._EXPERT_OPT[other_expert]["expert_params"]
    for setting, _ in GENES:
        if setting == "min_analysts":
            if setting in other_space:
                assert other_space[setting] != mod._EXPERT_OPT[
                    "FMPEarningsEvent"]["expert_params"]["min_analysts"], (
                    f"{other_expert}'s min_analysts gene is suspiciously IDENTICAL to "
                    f"FMPEarningsEvent's -- they should be independent gene definitions")
            continue
        assert setting not in other_space, (
            f"{other_expert} unexpectedly carries the FMPEarningsEvent-only gene {setting!r}")
