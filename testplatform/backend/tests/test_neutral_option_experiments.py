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


@pytest.mark.parametrize("modes", ["", "hold,hold", "legacy,hold", "invalid"])
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
    assert base["experts"][0]["settings"]["neutral_option_entry_mode"] == mode
    strategy = L._build_strategy("O_IC", "trial", "FMPRating", neutral_entry_mode=mode)
    trial = _build_daily_trial_config(base, decode_params(strategy, {}))
    assert trial["experts"][0]["settings"]["neutral_option_entry_mode"] == mode
    from tests.test_backtest_export_fixed_settings import _backtest
    from app.api.backtests import _derive_export_payload
    saved = _backtest(strategy_params={"expertFixedSettings": base["experts"][0]["settings"]})
    payload = _derive_export_payload(saved, "expert_settings", db=None)
    assert payload["settings"]["expert_params"]["neutral_option_entry_mode"] == mode
