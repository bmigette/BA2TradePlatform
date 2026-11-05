"""ONE default decision time (ba2_common.core.knowability.DEFAULT_DECISION_TIME) for every NEW run.

The decision price is the close of the latest bar that has ENDED, so a new grid must not decide on
the session's first bar. The launcher, the API, the robustness suite and the UI all take the time
from the one constant; stored rows keep the time they ran at (``LEGACY_DECISION_TIME``).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ba2_common.core.knowability import DEFAULT_DECISION_TIME, LEGACY_DECISION_TIME

REPO = Path(__file__).resolve().parents[3]


def test_the_default_is_not_the_first_bar():
    assert DEFAULT_DECISION_TIME != LEGACY_DECISION_TIME
    assert DEFAULT_DECISION_TIME > LEGACY_DECISION_TIME      # at least one bar after the open


def test_the_frontend_constant_equals_the_backend_one():
    ts = (REPO / "testplatform" / "frontend" / "src" / "lib" / "decisionTime.ts").read_text(encoding="utf-8")
    m = re.search(r"DEFAULT_DECISION_TIME\s*=\s*'(\d\d:\d\d)'", ts)
    assert m and m.group(1) == DEFAULT_DECISION_TIME


def test_no_frontend_file_hardcodes_the_open_as_a_schedule_time():
    for rel in ("components/ScheduleEditor.tsx", "pages/Backtesting.tsx"):
        src = (REPO / "testplatform" / "frontend" / "src" / rel).read_text(encoding="utf-8")
        assert "'09:30'" not in src, f"{rel} hard-codes 09:30"


def test_the_api_weekly_schedule_uses_the_default():
    from app.api.backtests import _run_schedule_override
    assert _run_schedule_override("weekly", "tuesday")["times"] == [DEFAULT_DECISION_TIME]


def test_the_launcher_manage_schedule_uses_the_default():
    import ba2test_launcher
    assert ba2test_launcher._daily_manage_schedule()["times"] == [DEFAULT_DECISION_TIME]


def test_the_launcher_source_has_no_hardcoded_schedule_time():
    src = (REPO / "testplatform" / "ba2test_launcher.py").read_text(encoding="utf-8")
    assert '"times": ["09:30"]' not in src


def test_a_robustness_day_variant_keeps_the_parents_time():
    """It used to pin 09:30, silently moving a row decided at another time."""
    from app.services.robustness_handler import _schedule_variants
    out = _schedule_variants({"day_variants": True}, ["09:40"])
    assert len(out) == 5 and all(v["override"]["times"] == ["09:40"] for v in out)


def test_a_robustness_day_variant_refuses_an_intraday_parent_without_a_time():
    from app.services.robustness_handler import _schedule_variants
    with pytest.raises(ValueError):
        _schedule_variants({"day_variants": True}, [], True)


def test_a_robustness_day_variant_on_a_daily_clock_needs_no_time():
    """Options / daily-clock parents state no time; the engine ignores it there: variants still work."""
    from app.services.robustness_handler import _schedule_variants
    out = _schedule_variants({"day_variants": True}, [], False)
    assert len(out) == 5 and all(v["override"]["times"] for v in out)
