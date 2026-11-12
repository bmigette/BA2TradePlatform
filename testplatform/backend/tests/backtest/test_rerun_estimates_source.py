"""A re-run of a row stored before runs had to name an estimates source (TEST 0175) must still rebuild: that
row ran on the FMP snapshot, so the rebuilt config says fmp. An explicit source re-measures it on another one.

THE DEFECT (2026-11-07): e9b34dfb made ``estimates_source`` mandatory in every run config, but the shared
rebuild path never stamped it, so ``/rerun``, the robustness launcher and tools/rerun_stored_row.py raised
``EstimatesSourceMissing`` for every older row."""
import importlib.util
from pathlib import Path

import pytest

from ba2_common.core.estimates_source import ESTIMATES_SOURCE_KEY, EstimatesSourceMissing  # noqa: F401
from app.services.backtest import rerun_handler as RH


def test_a_legacy_row_reruns_on_the_source_it_ran_on():
    block = {}
    RH._stamp_estimates_source(block, None, None)
    assert block[ESTIMATES_SOURCE_KEY] == RH.LEGACY_ESTIMATES_SOURCE == "fmp"


def test_the_stored_source_is_kept():
    block = {}
    RH._stamp_estimates_source(block, "alphavantage", None)
    assert block[ESTIMATES_SOURCE_KEY] == "alphavantage"


def test_an_explicit_source_wins():
    block = {}
    RH._stamp_estimates_source(block, "fmp", "alphavantage")
    assert block[ESTIMATES_SOURCE_KEY] == "alphavantage"


def test_an_unknown_source_refuses():
    with pytest.raises(Exception):
        RH._stamp_estimates_source({}, None, "bloomberg")


def _tool():
    path = Path(__file__).resolve().parents[4] / "tools" / "rerun_stored_row.py"
    src = path.read_text(encoding="utf-8")
    ns = {}
    # Only the pure helpers: the module body enters the backend on import.
    start = src.index("def parse_overrides")
    end = src.index("def main()")
    exec("import json\n" + src[start:end], ns)
    return ns


def test_rerun_tool_overrides_parse_json_then_string():
    t = _tool()
    assert t["parse_overrides"](["model_target_method=consensus", "max_expected_profit_percent=40",
                                 "use_model_target=true"]) == {
        "model_target_method": "consensus", "max_expected_profit_percent": 40, "use_model_target": True}
    with pytest.raises(ValueError):
        t["parse_overrides"](["nokey"])


def test_rerun_tool_overrides_reach_every_expert():
    t = _tool()
    cfg = {"experts": [{"class": "X", "settings": {"model_target_method": "forward"}}]}
    prev = t["apply_expert_overrides"](cfg, {"model_target_method": "consensus"})
    assert cfg["experts"][0]["settings"]["model_target_method"] == "consensus"
    assert prev == {"model_target_method": "forward"}
