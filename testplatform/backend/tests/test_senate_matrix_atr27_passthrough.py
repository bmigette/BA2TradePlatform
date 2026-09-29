"""``tools/run_senate_matrix.py`` goal2027atr Senate-lane passthrough (atr_grid_2027_design.md
section 4/§7, docs/strategy_research/atr_grid/atr_grid_2027_design.md).

The equity S1-S7 lane (``tools/run_screener_capband_matrix.py``) already forwards the GA-budget,
ATR-policy and market-condition flags PHASE=atr needs (see ``test_matrix_profit_cap_passthrough.py``
and ``test_screener_capband_matrix_exclude_symbols.py``). The Senate lane (``run_senate_matrix.py``)
lacked all of them; this module pins the same flags added there, mirroring the equity driver's
implementation and its digest-on-market-flags job-naming.

Loaded BY ABSOLUTE PATH (the ``_LAUNCHER_PATH`` pattern
``test_screener_capband_matrix_exclude_symbols.py`` uses), not a plain ``import``, because the
venv's editable finder resolves a bare ``import run_senate_matrix``/``import matrix_flags`` to
the MAIN checkout's ``tools/`` instead of this worktree's -- and this module needs the worktree's
copy (the one actually being patched here).

Pins, in the order a regression would bite:
  * an ordinary invocation (no new flags) builds the SAME argv/job-names as before this change
    (``--dry-run`` prints plain, undigested names; no --sizing-mode/--early-stop/--rm-toggle-
    policy/--stress-spread-bps/--no-robust-fitness tokens reach the launcher argv);
  * each new flag is forwarded to the launcher argv ONLY when given;
  * ``market_condition_passthrough`` is byte-identical in shape to the equity driver's function
    (same 5 flags, same "none of them -> []" behaviour);
  * a market-condition flag digest-suffixes the job name (``-d<12 hex>``), exactly like the
    equity driver, via the SAME shared ``matrix_flags.job_name_with_digest`` (not a
    hand-duplicated copy that could drift);
  * ``--sizing-mode`` requires the mode token in ``--name-suffix``, same guard as the equity
    driver's ``--sizing-mode``;
  * ``--universe-file`` reads an alternate universe (comment/blank lines skipped), so
    ``docs/strategy_research/atr_grid/senate_universe_2020_2025.txt`` (a provenance header
    followed by one symbol per line) parses correctly;
  * ``--rm-toggle-policy atr-searched`` is refused by the LAUNCHER unless the resolved job name
    contains ``-atr27`` -- this module does not re-implement that refusal (the launcher already
    owns it, see ``testplatform/ba2test_launcher.py:_refuse_atr_policy_without_job_name``), it
    only checks that a Senate PHASE name (``sen-<S>-atr27-riskatr...``) satisfies it.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

_DIGEST_RE = re.compile(r"-d[0-9a-f]{12}\b")

# tests/ -> backend/ -> testplatform/ -> repo root, then tools/ beside it.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_TOOLS = os.path.join(_REPO, "tools")
_SENATE_SCRIPT = os.path.join(_TOOLS, "run_senate_matrix.py")
_FLAGS_SCRIPT = os.path.join(_TOOLS, "matrix_flags.py")
_SCREENER_SCRIPT = os.path.join(_TOOLS, "run_screener_capband_matrix.py")
_UNIVERSE_2020_2025 = os.path.join(
    _REPO, "docs", "strategy_research", "atr_grid", "senate_universe_2020_2025.txt")


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def matrix_flags():
    return _load("matrix_flags_sen27", _FLAGS_SCRIPT)


@pytest.fixture
def driver(matrix_flags):  # noqa: ARG001 -- ensures matrix_flags is importable as a bare module
    # run_senate_matrix.py does `from matrix_flags import ...` as a plain sibling import
    # (tools/ is expected to be sys.path[0]); make that resolve to THIS worktree's copy.
    sys.path.insert(0, _TOOLS)
    try:
        return _load("run_senate_matrix_atr27", _SENATE_SCRIPT)
    finally:
        sys.path.remove(_TOOLS)


@pytest.fixture
def screener_driver(matrix_flags):  # noqa: ARG001
    sys.path.insert(0, _TOOLS)
    try:
        return _load("run_screener_capband_matrix_atr27", _SCREENER_SCRIPT)
    finally:
        sys.path.remove(_TOOLS)


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Capture every ``subprocess.run`` argv the driver's ``main()`` would launch."""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    # No such DB -> _completed_names() returns an empty set, so no job is skipped.
    monkeypatch.setenv("DB_FILE", str(tmp_path / "no-such-optimizations.db"))
    return calls


def _flag_value(cmd: list[str], flag: str):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


# --------------------------------------------------------------------------- #
# Byte-identity: no new flags -> no new tokens, no digest.
# --------------------------------------------------------------------------- #
def test_ordinary_invocation_is_unchanged(driver, captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_senate_matrix.py", "--strategies", "S2"])
    assert driver.main() == 0
    assert captured, "the driver launched no optimize job"
    cmd = captured[0]
    assert _flag_value(cmd, "--name") == "sen-S2"
    for flag in ("--sizing-mode", "--early-stop", "--early-stop-min-rel", "--rm-toggle-policy",
                 "--stress-spread-bps", "--no-robust-fitness", "--market-condition-profile",
                 "--market-condition-manifest", "--market-exit", "--market-condition-mode",
                 "--search-sl-loosen"):
        assert flag not in cmd, f"{flag} leaked into an ordinary invocation"


def test_dry_run_without_new_flags_prints_plain_undigested_names(driver, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv",
                        ["run_senate_matrix.py", "--strategies", "S2,S3", "--dry-run"])
    rc = driver.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "sen-S2" in out and "sen-S3" in out
    assert not _DIGEST_RE.search(out)


# --------------------------------------------------------------------------- #
# Each flag forwarded only when given.
# --------------------------------------------------------------------------- #
def test_early_stop_forwarded_only_when_given(driver, captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_senate_matrix.py", "--strategies", "S2",
        "--early-stop", "5", "--early-stop-min-rel", "0.01",
    ])
    assert driver.main() == 0
    cmd = captured[0]
    assert _flag_value(cmd, "--early-stop") == "5"
    assert _flag_value(cmd, "--early-stop-min-rel") == "0.01"

    captured.clear()
    monkeypatch.setattr(sys, "argv", ["run_senate_matrix.py", "--strategies", "S2"])
    assert driver.main() == 0
    assert "--early-stop" not in captured[0]
    assert "--early-stop-min-rel" not in captured[0]


def test_rm_toggle_policy_forwarded_only_when_given(driver, captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_senate_matrix.py", "--strategies", "S2", "--name-suffix=-atr27-riskatr",
        "--rm-toggle-policy", "atr-searched",
    ])
    assert driver.main() == 0
    cmd = captured[0]
    assert _flag_value(cmd, "--rm-toggle-policy") == "atr-searched"
    # The launcher's own refusal keys off the job NAME containing '-atr27' -- confirm the built
    # name satisfies it (this test does not re-implement the launcher's check).
    assert "-atr27" in _flag_value(cmd, "--name")

    captured.clear()
    monkeypatch.setattr(sys, "argv", ["run_senate_matrix.py", "--strategies", "S2"])
    assert driver.main() == 0
    assert "--rm-toggle-policy" not in captured[0]


def test_stress_spread_bps_forwarded_only_when_positive(driver, captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_senate_matrix.py", "--strategies", "S2",
        "--spread-bps", "9", "--stress-spread-bps", "13.5",
    ])
    assert driver.main() == 0
    cmd = captured[0]
    assert _flag_value(cmd, "--spread-bps") == "9.0"
    assert _flag_value(cmd, "--stress-spread-bps") == "13.5"

    captured.clear()
    monkeypatch.setattr(sys, "argv", ["run_senate_matrix.py", "--strategies", "S2"])
    assert driver.main() == 0
    assert "--stress-spread-bps" not in captured[0]


def test_robust_fitness_default_on_forwards_nothing_no_robust_forwards_the_flag(
        driver, captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_senate_matrix.py", "--strategies", "S2"])
    assert driver.main() == 0
    assert "--no-robust-fitness" not in captured[0]
    assert "--robust-fitness" not in captured[0]

    captured.clear()
    monkeypatch.setattr(sys, "argv",
                        ["run_senate_matrix.py", "--strategies", "S2", "--no-robust-fitness"])
    assert driver.main() == 0
    assert "--no-robust-fitness" in captured[0]


def test_sizing_mode_forwarded_and_requires_matching_name_suffix(driver, captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_senate_matrix.py", "--strategies", "S2",
        "--sizing-mode", "risk_atr", "--name-suffix=-goal2027atr-riskatr",
    ])
    assert driver.main() == 0
    cmd = captured[0]
    assert _flag_value(cmd, "--sizing-mode") == "risk_atr"

    # Without a matching --name-suffix token, argparse.error() (SystemExit) refuses to run --
    # same guard as the equity driver's --sizing-mode.
    monkeypatch.setattr(sys, "argv",
                        ["run_senate_matrix.py", "--strategies", "S2", "--sizing-mode", "risk_atr"])
    with pytest.raises(SystemExit):
        driver.main()


# --------------------------------------------------------------------------- #
# market_condition_passthrough: mirrors the equity driver's function.
# --------------------------------------------------------------------------- #
def _mc_args(**over):
    base = dict(market_condition_profile=None, market_condition_manifest=None, market_exit=None,
                market_condition_mode=None, search_sl_loosen=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_market_condition_passthrough_empty_when_no_flag_given(driver, screener_driver):
    assert driver.market_condition_passthrough(_mc_args()) == []
    assert screener_driver.market_condition_passthrough(_mc_args()) == []


def test_market_condition_passthrough_matches_the_equity_driver_shape(driver, screener_driver):
    args = _mc_args(market_condition_profile="ohlcv-v1,ta-structure-v1",
                    market_condition_manifest="ohlcv-v1=abc,ta-structure-v1=def",
                    market_exit="exit,stop,tp", market_condition_mode="all-off",
                    search_sl_loosen=True)
    assert driver.market_condition_passthrough(args) == \
        screener_driver.market_condition_passthrough(args)


def test_market_condition_profile_forwarded_and_digests_the_job_name(driver, captured, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "run_senate_matrix.py", "--strategies", "S1",
        "--market-condition-profile", "ohlcv-v1,ta-structure-v1",
        "--market-condition-manifest", "ohlcv-v1=abc,ta-structure-v1=def",
        "--market-exit", "exit,stop,tp", "--search-sl-loosen",
    ])
    assert driver.main() == 0
    cmd = captured[0]
    assert _flag_value(cmd, "--market-condition-profile") == "ohlcv-v1,ta-structure-v1"
    assert _flag_value(cmd, "--market-condition-manifest") == "ohlcv-v1=abc,ta-structure-v1=def"
    assert _flag_value(cmd, "--market-exit") == "exit,stop,tp"
    assert "--search-sl-loosen" in cmd
    name = _flag_value(cmd, "--name")
    assert name.startswith("sen-S1-d")
    assert _DIGEST_RE.search(name)


def test_job_name_digest_shared_with_equity_driver(driver, screener_driver):
    """The digest math itself (matrix_flags.job_name_with_digest) is ONE implementation: the
    Senate driver's ``_job_name`` and the equity driver's ``_job_name`` must produce identical
    output for identical (name, cmd) input, not two hand-copied sha256 recipes that could drift
    apart."""
    cmd = ["ba2-test.exe", "optimize", "--expert", "FMPSenateTraderWeight", "--universe", "AAPL",
          "--name", "sen-S1", "--market-condition-profile", "ohlcv-v1"]
    assert driver._job_name("sen-S1", cmd) == screener_driver._job_name("sen-S1", cmd)


# --------------------------------------------------------------------------- #
# --universe-file
# --------------------------------------------------------------------------- #
def test_universe_file_default_is_unchanged(driver):
    assert driver._universe() == driver._universe(driver._UNIVERSE_FILE)


def test_universe_file_skips_comment_and_blank_lines(driver, tmp_path):
    p = tmp_path / "uni.txt"
    p.write_text("# header line one\n# header line two\n\nAAPL\nMSFT\n\n# trailing comment\nTSLA\n",
                encoding="utf-8")
    assert driver._universe(str(p)) == "AAPL,MSFT,TSLA"


@pytest.mark.skipif(not os.path.exists(_UNIVERSE_2020_2025),
                    reason="senate_universe_2020_2025.txt not present in this checkout")
def test_universe_file_reads_the_2020_2025_derivation(driver):
    uni = driver._universe(_UNIVERSE_2020_2025)
    syms = uni.split(",")
    assert len(syms) > 500  # 729 at derivation time; a loose floor so re-derivation doesn't pin
    assert "#" not in uni
    assert syms == sorted(syms), "the file's symbols are expected sorted"


def test_universe_file_forwarded_end_to_end(driver, captured, monkeypatch, tmp_path):
    p = tmp_path / "uni.txt"
    p.write_text("# comment\nAAA\nBBB\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "run_senate_matrix.py", "--strategies", "S2", "--universe-file", str(p),
    ])
    assert driver.main() == 0
    cmd = captured[0]
    assert _flag_value(cmd, "--universe") == "AAA,BBB"
