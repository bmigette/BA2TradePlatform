"""A FRED series that fails to prewarm must be visible at WARNING, not INFO.

``prewarm_fred`` had ONE sink for both its progress lines and its failures, and the
API handler (``data_build_handler._prewarm_fred``) passed ``log=logger.info``. So a
series that could not be refreshed was reported at INFO -- below the level the
backend logs at -- and the task went on to return ``{"errors": 1}`` inside a summary
dict nothing reads at a glance. The next thing to touch that series is a hermetic
DeterministicScorer trial, which aborts on the missing file with no trace of why.

Progress stays on the progress sink; the failure path gets its own. A caller that
supplies only ``log`` (the CLI, which prints both to stdout where both are equally
visible) keeps exactly the behaviour it had.
"""
from __future__ import annotations

import pytest

from app.services import prewarm_fetchers


@pytest.fixture
def failing_fred(monkeypatch, tmp_path):
    """Two series in the spec, both of which fail to refresh, into an empty root."""
    from ba2_providers.macro import fred_series

    monkeypatch.setattr(fred_series, "SERIES_SPEC", {"VIXCLS": {"vintage": False},
                                                     "UNRATE": {"vintage": True}})
    monkeypatch.setattr(fred_series, "cache_path", lambda sid: str(tmp_path / f"{sid}.json"))
    monkeypatch.setattr(prewarm_fetchers, "resolve_fred_key", lambda: "a-key")

    def _boom(sid, key):
        raise RuntimeError(f"FRED said no for {sid}")

    monkeypatch.setattr(fred_series, "refresh_series", _boom)


def test_a_failed_series_goes_to_the_warning_sink(failing_fred):
    info, warnings = [], []

    summary = prewarm_fetchers.prewarm_fred(24.0, log=info.append, warn=warnings.append)

    assert summary["errors"] == 2
    assert info == [], "a failure is not progress"
    assert len(warnings) == 2 and all("FRED" in m for m in warnings), (
        f"the failures must reach the warning sink; got {warnings}")


def test_one_sink_still_receives_both(failing_fred):
    """The CLI passes ``log`` alone and prints everything; it must not lose failures
    to a logger the operator is not reading."""
    lines = []

    prewarm_fetchers.prewarm_fred(24.0, log=lines.append)

    assert len(lines) == 2, f"the single sink lost the failures: {lines}"


def test_the_api_handler_separates_the_two_sinks(failing_fred, monkeypatch):
    """``data_build_handler._prewarm_fred`` is the caller the defect was found in."""
    import importlib

    handler = importlib.import_module("app.services.data_build_handler")
    seen = []
    monkeypatch.setattr(handler.logger, "info", lambda msg, *a, **k: seen.append(("info", msg)))
    monkeypatch.setattr(handler.logger, "warning",
                        lambda msg, *a, **k: seen.append(("warning", msg)))

    handler._prewarm_fred(24.0)

    assert [level for level, _ in seen] == ["warning", "warning"], (
        f"the API prewarm still reports a failed series as progress: {seen}")
