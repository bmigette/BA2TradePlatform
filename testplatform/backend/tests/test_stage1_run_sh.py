"""``tools/stage1_run.sh`` wiring (F4, option-program-review-findings.md, 2026-08-30).

A thin config/wiring test: the script's only "behaviour" is which flags it hands
``run_options_matrix.py``, so this greps the actual file content the operator launches --
the same wiring-not-mechanism posture as ``test_equity_cap_launcher.py`` and
``test_launcher_screener_gate.py``, whose own mechanism tests cover ``--max-stock-price`` /
``--screener-gate-store`` and the per-strategy ``screener_gate_base`` overrides.
"""
import os

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


def test_elitism_is_not_hardcoded_here_it_relies_on_the_launchers_fixed_default():
    """F4's elitism fix lives in ba2test_launcher.py's --elitism-percent default (10.0, was a
    hardcoded 0.1) -- see test_elitism_percent_launcher.py. stage1_run.sh does not override it,
    so it must not silently pin the old broken value either."""
    assert "elitism" not in _text().lower()
