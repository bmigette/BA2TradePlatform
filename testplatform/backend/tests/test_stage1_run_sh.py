"""``tools/stage1_run.sh`` wiring (F4, option-program-review-findings.md, 2026-08-30).

A thin config/wiring test: the script's only "behaviour" is which flags it hands
``run_options_matrix.py``, so this greps the actual file content the operator launches --
the same wiring-not-mechanism posture as ``test_equity_cap_launcher.py`` and
``test_launcher_screener_gate.py``, whose own mechanism tests cover ``--max-stock-price`` /
``--screener-gate-store`` and the per-strategy ``screener_gate_base`` overrides.
"""
import os

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCRIPT = os.path.join(_ROOT, "tools", "stage1_run.sh")


def _text() -> str:
    with open(_SCRIPT, encoding="utf-8") as f:
        return f.read()


def test_the_script_exists():
    assert os.path.isfile(_SCRIPT)


def test_population_and_generations_are_env_overridable_with_the_documented_defaults():
    text = _text()
    # 200 is the approved discovery budget (2026-09-12 review restored it; the 140 pilot value
    # is an explicit override, never the default).
    assert 'POP="${POP:-200}"' in text
    assert 'GEN="${GEN:-60}"' in text
    assert '--population "$POP"' in text
    assert '--generations "$GEN"' in text
    # The old hardcoded literals must be gone -- an override that coexists with a hardcoded
    # value would be silently ignored by whichever one loses the argparse race.
    assert "--population 200" not in text
    assert "--generations 60 --early-stop" not in text


def test_the_header_states_the_slot_policy_the_prewarm_and_the_pop_pilot():
    """The header is the operator's runbook for this host: the consumer count is tuned from
    measured PRIVATE bytes (RSS counts shared mapped pages), the prewarm is mandatory before a
    cold launch, and POP=140 remains an explicitly labelled pilot rather than the default."""
    text = _text()
    assert 'PARALLEL="${PARALLEL:-24}"' in text
    assert "smaps_rollup" in text
    assert "build_shared_arrays.py" in text
    assert "0 built / 98 opened" in text
    assert "POP=140" in text and "pilot" in text


def test_the_universe_price_caps_are_wired_through_the_gate_store_not_the_blanket_cap():
    """F4(a): --max-stock-price alone is the blanket default for EVERY structure; the design's
    per-structure caps (O_CSP/O_JL/O_RS $100, O_SSTD/O_SSTG $300) live as real
    _OPTION_STRATS[].screener_gate_base entries (ba2test_launcher.py) that win over the
    blanket default by precedence, so the script disables the blanket cap (0) and only
    supplies the store."""
    text = _text()
    assert "--screener-gate-store" in text
    assert "--max-stock-price 0" in text


def test_a_preflight_check_refuses_to_launch_without_the_gate_store():
    text = _text()
    assert "SCREENER_STORE" in text
    # Some existence check on the store path before the exec, not a silent proceed.
    assert "exit 1" in text
    assert text.index("exit 1") < text.index("exec ")


def test_the_market_condition_profile_is_off_by_default_and_adds_nothing_when_off():
    """Unset MARKET_CONDITION_PROFILE must leave this script the launch it has always been: the
    warm step and both flags live inside one guard, so nothing runs and nothing is passed."""
    text = _text()
    assert 'MARKET_CONDITION_PROFILE="${MARKET_CONDITION_PROFILE:-none}"' in text
    guard = 'if [ "$MARKET_CONDITION_PROFILE" != "none" ]; then'
    assert guard in text
    # Every warm command and both forwarded flags sit AFTER the guard and BEFORE the exec.
    body = text[text.index(guard):text.index("exec ")]
    for token in ("warm_market_conditions.py plan", "warm_market_conditions.py build",
                  "warm_market_conditions.py verify", "warm_market_conditions.py prepare-host"):
        assert token.split(".py ")[1] in body
    assert "--market-condition-profile" in body and "--market-condition-manifest" in body


def test_a_manifest_with_the_profile_off_refuses_the_launch(tmp_path):
    """Review 2026-09-16, F2, in the wrapper's environment form.

    ``MARKET_CONDITION_MANIFEST`` exported with ``MARKET_CONDITION_PROFILE`` unset used to launch
    the whole grid UNGATED -- the matrix driver dropped the digest, the launcher never saw it, and
    the jobs took the ordinary ungated discovery names (so they could also be SKIPped against an
    existing ungated completion) while the environment said a snapshot was pinned.

    The guard itself is executed here, not just grepped: the slice of the real script from the two
    variable defaults down to the end of the refusal, run under bash with the offending
    environment and with the accepted ones.
    """
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not available on this host")
    text = _text()
    start = text.index('MARKET_CONDITION_PROFILE="${MARKET_CONDITION_PROFILE:-none}"')
    end = text.index('if [ "$MARKET_CONDITION_PROFILE" != "none" ]; then')
    fragment = text[start:end] + "\necho REACHED-THE-WARM-STEP\n"
    script = tmp_path / "guard.sh"
    script.write_text(fragment, encoding="utf-8", newline="\n")

    def run(env):
        return subprocess.run([bash, str(script)], capture_output=True, text=True,
                              env={**os.environ, **env})

    refused = run({"MARKET_CONDITION_MANIFEST": "abc123", "MARKET_CONDITION_PROFILE": ""})
    assert refused.returncode == 1
    assert "MARKET_CONDITION_PROFILE is unset/none" in refused.stderr
    assert "REACHED-THE-WARM-STEP" not in refused.stdout

    # ... and the two configurations that ARE meaningful still pass the guard.
    both = run({"MARKET_CONDITION_MANIFEST": "ohlcv-v1=abc123",
                "MARKET_CONDITION_PROFILE": "ohlcv-v1"})
    assert both.returncode == 0 and "REACHED-THE-WARM-STEP" in both.stdout
    neither = run({"MARKET_CONDITION_MANIFEST": "", "MARKET_CONDITION_PROFILE": ""})
    assert neither.returncode == 0 and "REACHED-THE-WARM-STEP" in neither.stdout


def test_the_warm_step_runs_once_before_the_matrix_in_the_documented_order():
    text = _text()
    body = text[:text.index("exec ")]
    order = [body.index(step) for step in
             (" plan --profile", " build --plan", " verify --manifest", " prepare-host --manifest")]
    assert order == sorted(order), "plan -> build -> verify -> prepare-host"
    # The digest the build publishes is what gets pinned into every job.
    assert "--print-digest" in body
    assert 'MC_ARGS=(--market-condition-profile "$MARKET_CONDITION_PROFILE" \\' in text
    assert '${MC_ARGS[@]+"${MC_ARGS[@]}"}' in text


def test_every_warm_step_aborts_the_launch_rather_than_running_a_gated_grid_half_warmed():
    """`set -euo pipefail` plus an explicit message per step: a failed plan/build/verify/prepare
    must not fall through into 32 jobs that each rediscover the same missing feature store."""
    text = _text()
    body = text[text.index('if [ "$MARKET_CONDITION_PROFILE" != "none" ]; then'):text.index("exec ")]
    assert body.count("exit 1") >= 4
    assert "PLAN is actionable" in body and "VERIFY failed" in body and "PREPARE-HOST failed" in body
    assert "--cache-only" in body  # never fetch while a grid waits


def test_a_dry_run_prints_the_warm_commands_before_the_matrix_command():
    """--dry-run must SHOW the preparation it would do, not silently skip to the matrix."""
    text = _text()
    body = text[text.index('if [ "$MARKET_CONDITION_PROFILE" != "none" ]; then'):text.index("exec ")]
    assert 'case " $* " in *" --dry-run "*) MC_DRY=1 ;; esac' in body
    dry = body[body.index('if [ "$MC_DRY" = "1" ]; then'):body.index("  else")]
    for step in ("plan --profile", "build --plan", "verify --manifest", "prepare-host --manifest"):
        assert step in dry, step
    assert "echo" in dry and "$MC_PYTHON $MC_WARM" in dry


def test_elitism_is_not_hardcoded_here_it_relies_on_the_launchers_fixed_default():
    """F4's elitism fix lives in ba2test_launcher.py's --elitism-percent default (10.0, was a
    hardcoded 0.1) -- see test_elitism_percent_launcher.py. stage1_run.sh does not override it,
    so it must not silently pin the old broken value either."""
    assert "elitism" not in _text().lower()
