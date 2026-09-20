"""Separate research arms, immutable job identities, and persisted entry policy."""
import pytest

from tests.test_option_discovery_driver import M, discovery_args
from tests.test_launcher_option_entry_rule import mod as L
from app.services.strategy_param_space import collect_param_space, decode_params


def test_split_changes_only_three_neutral_job_identities():
    def jobs(args):
        return list(M.planned_jobs(args, "launcher.py", ["DeterministicScorer"],
                                  args.strategies.split(","), "AAPL,F"))
    legacy = jobs(discovery_args())
    split = jobs(discovery_args("--neutral-entry-modes", "hold,low_confidence"))
    assert len(legacy) == 16 and len(split) == 19
    assert len({j[0] for j in split}) == 19
    assert [j for j in legacy if j[2] not in M._NEUTRAL_STRUCTURES] == [
        j for j in split if j[2] not in M._NEUTRAL_STRUCTURES]
    assert not {j[0] for j in legacy if j[2] in M._NEUTRAL_STRUCTURES} & {j[0] for j in split}
    for name, expert, kind, mode in split:
        cmd = M.build_cmd(discovery_args(), "launcher.py", name, expert, kind, "AAPL,F", mode)
        assert ("--neutral-entry-mode" in cmd) == (kind in M._NEUTRAL_STRUCTURES)


@pytest.mark.parametrize("modes", ["", "hold,hold", "legacy,hold", "invalid", "joint,hold"])
def test_invalid_modes_refused(modes):
    with pytest.raises(SystemExit):
        discovery_args("--neutral-entry-modes", modes)


@pytest.mark.parametrize("kind", ["O_STRD", "O_STRG", "O_IC"])
@pytest.mark.parametrize("mode", ["hold", "low_confidence"])
def test_signal_gate_cannot_mutate_out_of_its_experimental_arm(kind, mode):
    strategy = L._build_strategy(kind, "neutral-test", "DeterministicScorer", neutral_entry_mode=mode)
    genes = collect_param_space(strategy, [])
    leaves = strategy.entry_rules[0]["conditions"]["conditions"]
    fields = {c["field"] for c in leaves}
    prefix = kind.lower()
    if mode == "hold":
        assert "current_rating_neutral" in fields and "confidence" not in fields
        assert not any(c["id"] == prefix + "-exp_profit" for c in leaves)
        assert f"cond:{prefix}-signal:enabled" not in genes
    else:
        assert "current_rating_neutral" not in fields and "confidence" in fields
        assert f"cond:{prefix}-low_confidence:enabled" not in genes
        assert any(prefix + "-low_confidence" in key for key in genes)
    decoded = decode_params(strategy, {})
    assert decoded["entry_rules"]


@pytest.mark.parametrize("mode", ["hold", "low_confidence"])
def test_cli_policy_survives_trial_config_and_export(mode, monkeypatch):
    from tests.test_robust_fitness_default_on import _parse, _run_optimize, _BASE_ARGV
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    args = _parse([*_BASE_ARGV, "--strategy", "O_IC", "--neutral-entry-mode", mode,
                   "--options-store", "thetadata", "--name", "neutral-" + mode])
    config = _run_optimize(args, monkeypatch)
    base = config["backtest"]
    assert base["experts"][0]["settings"]["evaluate_entry_rules_on_hold"] is True
    strategy = L._build_strategy("O_IC", "trial", "FMPRating", neutral_entry_mode=mode)
    trial = _build_daily_trial_config(base, decode_params(strategy, {}))
    assert trial["experts"][0]["settings"]["evaluate_entry_rules_on_hold"] is True
    from tests.test_backtest_export_fixed_settings import _backtest
    from app.api.backtests import _derive_export_payload
    saved = _backtest(strategy_params={"expertFixedSettings": base["experts"][0]["settings"]})
    payload = _derive_export_payload(saved, "expert_settings", db=None)
    assert payload["settings"]["expert_params"]["evaluate_entry_rules_on_hold"] is True
    assert "neutral_option_entry_mode" not in payload["settings"]["expert_params"]


def test_joint_is_16_jobs_with_only_three_changed_identities():
    def jobs(args):
        return list(M.planned_jobs(args, "launcher.py", ["DeterministicScorer"],
                                  args.strategies.split(","), "AAPL,F"))
    old = jobs(discovery_args())
    joint = jobs(discovery_args("--neutral-entry-modes", "joint"))
    assert len(joint) == len({j[0] for j in joint}) == 16
    assert [j for j in old if j[2] not in M._NEUTRAL_STRUCTURES] == [
        j for j in joint if j[2] not in M._NEUTRAL_STRUCTURES]
    assert all(j[3] == "joint" and "-joint-" in j[0]
               for j in joint if j[2] in M._NEUTRAL_STRUCTURES)


@pytest.mark.parametrize("mode", ["hold", "low_confidence"])
def test_joint_gene_reaches_trial_and_saved_backtest_export(mode, monkeypatch):
    from tests.test_robust_fitness_default_on import _parse, _run_optimize, _BASE_ARGV
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from tests.test_backtest_export_fixed_settings import _backtest
    from app.api.backtests import _derive_export_payload
    args = _parse([*_BASE_ARGV, "--strategy", "O_IC", "--neutral-entry-mode", "joint",
                   "--options-store", "thetadata", "--name", "neutral-joint"])
    config = _run_optimize(args, monkeypatch)
    strategy = L._build_strategy("O_IC", "joint", "FMPRating", neutral_entry_mode="joint")
    # The handler splits schedule/screener namespaces before expert collection.
    space = collect_param_space(strategy, {k: v for k, v in config["expert_params"].items()
                                          if not k.startswith(("schedule:", "screener:"))})
    assert "model:neutral_option_entry_mode" not in space
    assert "model:evaluate_entry_rules_on_hold" not in space
    assert "cond:o_ic-signal:enabled" not in space
    assert "cond:o_ic-low_confidence:enabled" not in space
    genes = {f"entry:o_ic-entry-{arm}:enabled": int(arm == mode)
             for arm in ("hold", "low_confidence")}
    assert all(key in space for key in genes)
    decoded = decode_params(strategy, genes)
    trial = _build_daily_trial_config(config["backtest"], decoded)
    assert trial["experts"][0]["settings"]["evaluate_entry_rules_on_hold"] is True
    assert decoded["expert_overrides"] == {}
    saved = _backtest(strategy_params={**genes,
        "expertFixedSettings": config["backtest"]["experts"][0]["settings"]})
    payload = _derive_export_payload(saved, "expert_settings", db=None)
    assert payload["settings"]["expert_params"]["evaluate_entry_rules_on_hold"] is True


@pytest.mark.parametrize("kind", ["O_STRD", "O_STRG", "O_IC"])
@pytest.mark.parametrize("mode", ["hold", "low_confidence"])
def test_joint_template_materializes_exactly_the_fixed_arm(kind, mode):
    strategy = L._build_strategy(kind, "joint", "DeterministicScorer", neutral_entry_mode="joint")
    decoded = decode_params(strategy, {f"entry:{kind.lower()}-entry-{arm}:enabled": int(arm == mode)
                                       for arm in ("hold", "low_confidence")})
    fixed = L._build_strategy(kind, "joint", "DeterministicScorer", neutral_entry_mode=mode)
    selected = decoded["entry_rules"][0]
    fixed_rule = decode_params(fixed, {})["entry_rules"][0]
    assert selected["conditions"] == fixed_rule["conditions"]
    assert selected["actions"] == fixed_rule["actions"]
    assert len(strategy.entry_rules) == 2, "decode must not mutate the template"


def test_joint_uses_ordinary_rules_that_survive_normalization():
    from ba2_common.core.rule_models import normalize_trade_rules
    from ba2_common.core.rules_convert import live_actions_from_trade_rule
    strategy = L._build_strategy("O_IC", "joint", "FMPRating", neutral_entry_mode="joint")
    strategy.entry_rules = normalize_trade_rules(strategy.entry_rules)
    for rule in strategy.entry_rules:
        assert "neutral_entry_mode_search" not in rule
        assert live_actions_from_trade_rule(rule)


@pytest.mark.parametrize("hold,low", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_joint_uses_existing_rule_enabled_genes(hold, low):
    strategy = L._build_strategy("O_IC", "joint", "FMPRating", neutral_entry_mode="joint")
    decoded = decode_params(strategy, {"entry:o_ic-entry-hold:enabled": hold,
                                       "entry:o_ic-entry-low_confidence:enabled": low})
    assert len(decoded["entry_rules"]) == hold + low
    assert decoded["expert_overrides"] == {}
