"""tools/split_basis_ack_from_build.py: pure parser + ratio-measurement tests.

No cache, no network: ``parse_build_log``/``mixed_basis_moves``/``unacknowledgeable_verdicts``
are exercised against an embedded excerpt of a real ``warm_market_conditions.py build`` log
(2026-09-29, ABTC and IAC's actual lines), and ``measure_ack``/``generate`` against synthetic
in-memory daily bars.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import split_basis_ack_from_build as ack  # noqa: E402

# Real lines lifted from the 2026-09-29 goal2027atr equity market-condition build report
# (scratchpad/atr27/build-ohlcv-v1.out). ABTC: two mixed_basis checks (2 + 1 offending moves,
# plus two informational 'refetched' checks). IAC: no mixed_basis at all -- consistent, drift,
# undetectable -- the kind acknowledge_jump cannot express.
BUILD_LOG_EXCERPT = r"""
2026-09-29 11:04:15,985 - ba2_common - MarketDataProviderInterface - DEBUG - noise line, ignored
[11:05:45] build ABTC: EXCLUDED [{'kind': 'split_basis_unverified', 'checks': [{'split_date': '2022-11-08', 'ratio': 0.05, 'verdict': 'refetched', 'checked_bar': None, 'basis': None, 'reason': 'full history fetched on 2026-09-29 (UTC)'}, {'split_date': '2024-02-09', 'ratio': 0.05, 'verdict': 'mixed_basis', 'checked_bar': '2024-02-13', 'basis': 'mixed', 'reason': 'one-day close move(s) beyond x1.4 within 30 days of the 2024-02-09 split but not on its ex-date bar: 2024-02-13 x0.6601. The file is on a mixed basis (adjusted on the wrong day); only a full re-fetch can repair it'}, {'split_date': '2025-09-03', 'ratio': 0.2, 'verdict': 'mixed_basis', 'checked_bar': '2025-08-28', 'basis': 'mixed', 'reason': 'one-day close move(s) beyond x1.4 within 30 days of the 2025-09-03 split but not on its ex-date bar: 2025-08-28 x1.4215. The file is on a mixed basis (adjusted on the wrong day); only a full re-fetch can repair it'}, {'split_date': '2026-07-06', 'ratio': 0.06666666666666667, 'verdict': 'refetched', 'checked_bar': None, 'basis': None, 'reason': 'full history fetched on 2026-09-29 (UTC)'}]}]
[11:28:05] build IAC: EXCLUDED [{'kind': 'split_basis_unverified', 'checks': [{'split_date': '2020-07-01', 'ratio': 3.054, 'verdict': 'consistent', 'checked_bar': '2020-07-01', 'basis': 'split-adjusted', 'reason': ''}, {'split_date': '2021-05-25', 'ratio': 1.503, 'verdict': 'drift', 'checked_bar': '2021-05-25', 'basis': 'mixed', 'reason': 'a split-sized close move (0.077 log) occurs near the split'}, {'split_date': '2025-04-01', 'ratio': 1.219, 'verdict': 'undetectable', 'checked_bar': None, 'basis': None, 'reason': 'factor 1.219 < 1.5: prices cannot tell the basis; only a full re-fetch can'}]}]
[11:30:00] build UNAVAIL: EXCLUDED [{'kind': 'split_calendar_unavailable', 'error': 'boom'}]
""".strip("\n")

# A log concatenated from two runs: only the LAST 'build ABTC' line should win.
CONCATENATED_LOG_EXCERPT = (
    "[10:00:00] build ABTC: EXCLUDED [{'kind': 'split_basis_unverified', 'checks': "
    "[{'split_date': '2020-01-01', 'ratio': 2.0, 'verdict': 'mixed_basis', 'checked_bar': "
    "'2020-01-05', 'basis': 'mixed', 'reason': 'one-day close move(s) beyond x1.4 within 30 days "
    "of the 2020-01-01 split but not on its ex-date bar: 2020-01-05 x1.5000. stale run'}]}]\n"
    + BUILD_LOG_EXCERPT.splitlines()[1] + "\n"  # the real, later ABTC line
)


@pytest.fixture
def log_file(tmp_path):
    p = tmp_path / "build.out"
    p.write_text(BUILD_LOG_EXCERPT + "\n", encoding="utf-8")
    return p


def test_parse_build_log_extracts_only_split_basis_unverified_checks(log_file):
    parsed = ack.parse_build_log(log_file)
    assert set(parsed) == {"ABTC", "IAC", "UNAVAIL"}
    assert [c["split_date"] for c in parsed["ABTC"]] == [
        "2022-11-08", "2024-02-09", "2025-09-03", "2026-07-06"]
    assert parsed["UNAVAIL"] == []  # split_calendar_unavailable carries no 'checks' to parse


def test_parse_build_log_keeps_only_the_last_line_per_symbol(tmp_path):
    p = tmp_path / "build.out"
    p.write_text(CONCATENATED_LOG_EXCERPT, encoding="utf-8")
    parsed = ack.parse_build_log(p)
    assert [c["split_date"] for c in parsed["ABTC"]] == [
        "2022-11-08", "2024-02-09", "2025-09-03", "2026-07-06"]  # the real line, not the stale one


def test_mixed_basis_moves_parses_every_offending_move(log_file):
    parsed = ack.parse_build_log(log_file)
    moves = ack.mixed_basis_moves("ABTC", parsed["ABTC"])
    assert moves == [
        ack.LoggedMove("ABTC", date(2024, 2, 9), date(2024, 2, 13), 0.6601),
        ack.LoggedMove("ABTC", date(2025, 9, 3), date(2025, 8, 28), 1.4215),
    ]


def test_mixed_basis_moves_is_empty_when_there_is_none(log_file):
    parsed = ack.parse_build_log(log_file)
    assert ack.mixed_basis_moves("IAC", parsed["IAC"]) == []


def test_unacknowledgeable_verdicts_flags_drift_and_undetectable_not_refetched(log_file):
    parsed = ack.parse_build_log(log_file)
    bad = ack.unacknowledgeable_verdicts(parsed["IAC"])
    assert [c["verdict"] for c in bad] == ["drift", "undetectable"]
    assert ack.unacknowledgeable_verdicts(parsed["ABTC"]) == []  # mixed_basis + refetched only


# ---- measure_ack: ratio/anchors measured from synthetic bars, never copied from the log -------
def _bars(rows):
    """rows: [(iso date, close), ...] -> (dates, close) like ``_load_daily`` returns."""
    dates = np.array([np.datetime64(d, "D") for d, _ in rows])
    close = np.array([c for _, c in rows], dtype=np.float64)
    return dates, close


def test_measure_ack_computes_the_ratio_from_the_bars_and_anchors_both_bars():
    dates, close = _bars([("2024-02-12", 306.75), ("2024-02-13", 202.5)])
    move = ack.LoggedMove("ABTC", date(2024, 2, 9), date(2024, 2, 13), 0.6601)
    measured = ack.measure_ack(move, dates, close)
    assert measured.symbol == "ABTC" and measured.event_date == date(2024, 2, 13)
    assert measured.ratio == pytest.approx(202.5 / 306.75)
    assert measured.anchors == ((date(2024, 2, 12), 306.75), (date(2024, 2, 13), 202.5))


def test_measure_ack_raises_on_a_ratio_mismatch_beyond_tolerance():
    dates, close = _bars([("2024-02-12", 100.0), ("2024-02-13", 100.0)])  # no move at all
    move = ack.LoggedMove("ABTC", date(2024, 2, 9), date(2024, 2, 13), 0.6601)
    with pytest.raises(ack.RatioMismatch, match="does not match the log"):
        ack.measure_ack(move, dates, close)


def test_measure_ack_raises_when_the_bar_is_missing():
    dates, close = _bars([("2024-02-12", 306.75)])  # no 2024-02-13 bar
    move = ack.LoggedMove("ABTC", date(2024, 2, 9), date(2024, 2, 13), 0.6601)
    with pytest.raises(ack.RatioMismatch, match="no bar"):
        ack.measure_ack(move, dates, close)


def test_measure_ack_raises_on_the_files_first_bar():
    dates, close = _bars([("2024-02-13", 202.5)])
    move = ack.LoggedMove("ABTC", date(2024, 2, 9), date(2024, 2, 13), 0.6601)
    with pytest.raises(ack.RatioMismatch, match="first bar"):
        ack.measure_ack(move, dates, close)


# ---- generate(): end to end against the embedded excerpt, cache stubbed -----------------------
def test_generate_emits_measured_entries_for_a_clean_symbol(log_file, monkeypatch):
    dates, close = _bars([
        ("2022-11-07", 1.0), ("2022-11-08", 1.0),
        ("2024-02-12", 306.75), ("2024-02-13", 202.5),
        ("2025-08-27", 90.75), ("2025-08-28", 129.0),
        ("2026-07-05", 1.0), ("2026-07-06", 1.0),
    ])
    monkeypatch.setattr(ack, "_load_daily", lambda symbol, cache_root=None: (dates, close))
    acks, problems = ack.generate(log_file, ["ABTC"])
    assert problems == []
    assert [e.event_date for e in acks["ABTC"]] == [date(2024, 2, 13), date(2025, 8, 28)]
    assert acks["ABTC"][0].ratio == pytest.approx(202.5 / 306.75)
    assert acks["ABTC"][0].anchors == ((date(2024, 2, 12), 306.75), (date(2024, 2, 13), 202.5))


def test_generate_reports_a_symbol_whose_verdicts_acknowledge_jump_cannot_express(log_file):
    acks, problems = ack.generate(log_file, ["IAC"])
    assert "IAC" not in acks
    assert any("acknowledge_jump cannot express" in p and "drift" in p for p in problems)


def test_generate_reports_an_unknown_symbol(log_file):
    acks, problems = ack.generate(log_file, ["NOPE"])
    assert acks == {}
    assert any("NOPE" in p and "no 'build NOPE" in p for p in problems)


def test_generate_reports_a_ratio_mismatch_instead_of_guessing(log_file, monkeypatch):
    # A close series where the 2024-02-13 move does NOT match the logged x0.6601.
    dates, close = _bars([
        ("2022-11-07", 1.0), ("2022-11-08", 1.0),
        ("2024-02-12", 100.0), ("2024-02-13", 100.0),
        ("2025-08-27", 90.75), ("2025-08-28", 129.0),
        ("2026-07-05", 1.0), ("2026-07-06", 1.0),
    ])
    monkeypatch.setattr(ack, "_load_daily", lambda symbol, cache_root=None: (dates, close))
    acks, problems = ack.generate(log_file, ["ABTC"])
    # The clean move still measures; the mismatching one is reported, not guessed.
    assert [e.event_date for e in acks["ABTC"]] == [date(2025, 8, 28)]
    assert any("2024-02-13" in p and "does not match the log" in p for p in problems)


def test_render_entries_round_trips_through_ast_literal_eval_friendly_python():
    """The rendered source must be valid Python that defines the same values (spot check via
    exec in a throwaway namespace standing in for split_basis_overrides.py's module globals)."""
    dates, close = _bars([("2024-02-12", 306.75), ("2024-02-13", 202.5)])
    move = ack.LoggedMove("ABTC", date(2024, 2, 9), date(2024, 2, 13), 0.6601)
    measured = ack.measure_ack(move, dates, close)
    src = ack.render_entries({"ABTC": [measured]})
    ns = {"date": date, "KIND_ACKNOWLEDGE_JUMP": "acknowledge_jump",
         "BasisOverride": lambda *a: a}
    result = eval("(" + src.strip().rstrip(",") + ",)", ns)  # noqa: S307 (test-only, own source)
    entry = result[0]
    assert entry[0] == "ABTC" and entry[1] == date(2024, 2, 13)
    assert entry[2] == pytest.approx(202.5 / 306.75)
