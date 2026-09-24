"""``--labels`` must reach the launcher WITHOUT moving any job's identity.

A label is metadata about a campaign, not part of its search configuration. The discovery job
name embeds a digest of the launched command, and the GA checkpoint is found by a hash of that
name -- so if a label entered the digest, adding one would rename every job and orphan every
banked checkpoint. That is the same failure the direction-gene change hit through the
fingerprint, and it costs days of compute, so it is pinned here rather than left to care.
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_DRIVER = os.path.join(_ROOT, "tools", "run_options_matrix.py")
_spec = importlib.util.spec_from_file_location("run_options_matrix", _DRIVER)
mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("run_options_matrix", mod)
_spec.loader.exec_module(mod)


def _args(*extra):
    """Real parsed args, the way test_option_discovery_driver builds them."""
    return mod.resolve_args(mod.build_parser(), list(extra))


def _name(labels):
    a = _args(*(["--labels", labels] if labels else []))
    return mod.discovery_name(a, _DRIVER, "optm-X-O_LC", "X", "O_LC", "AAPL,MSFT", "legacy")


def test_a_label_does_not_change_the_discovery_job_name():
    assert _name("") == _name("ForwardTest,goal2020"), (
        "adding --labels moved the job identity digest; every job would be renamed and every "
        "GA checkpoint orphaned"
    )


def test_two_different_labels_still_give_the_same_name():
    assert _name("alpha") == _name("beta")


def test_the_label_is_actually_forwarded_to_the_launcher():
    cmd = mod.build_cmd(_args("--labels", "ForwardTest,opt"), _DRIVER, "n", "X", "O_LC", "AAPL", "legacy")
    assert "--labels" in cmd, "the driver accepted --labels but never passed it on"
    assert cmd[cmd.index("--labels") + 1] == "ForwardTest,opt,O_LC"


def test_the_structure_is_appended_per_job():
    """One --labels string cannot vary across the 16 jobs a campaign launches, and the structure
    is exactly the thing that does. It has no column of its own, unlike the expert."""
    for strat in ("O_LC", "O_LP", "O_IC"):
        cmd = mod.build_cmd(_args("--labels", "OptionStage1"), _DRIVER, "n", "X", strat, "AAPL", "legacy")
        assert cmd[cmd.index("--labels") + 1] == "OptionStage1," + strat, strat


def test_the_structure_is_not_duplicated_if_already_named():
    cmd = mod.build_cmd(_args("--labels", "OptionStage1,O_LC"), _DRIVER, "n", "X", "O_LC", "AAPL", "legacy")
    assert cmd[cmd.index("--labels") + 1] == "OptionStage1,O_LC"


def test_the_expert_is_not_added_as_a_label():
    """backtests.expert_name is already an indexed column; a label would duplicate it."""
    cmd = mod.build_cmd(_args("--labels", "OptionStage1"), _DRIVER, "n", "DeterministicScorer",
                        "O_LC", "AAPL", "legacy")
    assert "DeterministicScorer" not in cmd[cmd.index("--labels") + 1]


def test_no_label_emits_no_flag():
    """An empty default must not add a bare flag -- that would change the command for every
    existing campaign and, through it, the digest."""
    assert "--labels" not in mod.build_cmd(_args(), _DRIVER, "n", "X", "O_LC", "AAPL", "legacy")
