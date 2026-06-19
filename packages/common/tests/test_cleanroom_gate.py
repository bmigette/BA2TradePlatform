"""Task 7 acceptance: the ba2_common clean-room gate.

Importing the full public ba2_common surface (the package, every interface base,
the ruleset engine TradeConditions, and the classic risk engine
TradeRiskManagement) in a SINGLE fresh interpreter must NOT pull any of the
forbidden provider / LLM / live-platform / UI packages into sys.modules.

Per Amendment A1 this is a *sys.modules* gate, not a "package not installed"
gate: fmpsdk / nicegui / langchain_core ARE installed in the live test venv, so a
real back-edge from ba2_common into any of them would be caught here. The gate is
proven non-vacuous by test_gate_is_non_vacuous below (a deliberate import of a
forbidden package makes the same check report LEAK).
"""
import os
import subprocess
import sys
import textwrap

# The Task 7 plan Step-1 module set: package + config/logger + core leaves +
# the two seam-bearing engines (interfaces, TradeConditions, TradeRiskManagement)
# plus the rest of the ruleset engine and rules_export_import.
CLEANROOM_MODULES = [
    "ba2_common",
    "ba2_common.config",
    "ba2_common.logger",
    "ba2_common.core.types",
    "ba2_common.core.models",
    "ba2_common.core.db",
    "ba2_common.core.utils",
    "ba2_common.core.position_sizing",
    "ba2_common.core.weinstein",
    "ba2_common.core.interfaces",
    "ba2_common.core.TradeConditions",
    "ba2_common.core.TradeActions",
    "ba2_common.core.TradeActionEvaluator",
    "ba2_common.core.TradeRiskManagement",
    "ba2_common.core.rules_export_import",
]

# fmpsdk / nicegui / langchain_core are the REAL installed packages in this venv;
# langchain (umbrella) and ba2_providers/ba2_experts/ba2_trade_platform are not,
# but listing them keeps the gate honest if they ever get installed.
FORBIDDEN = [
    "ba2_providers",
    "ba2_experts",
    "langchain",
    "langchain_core",
    "fmpsdk",
    "nicegui",
    "ba2_trade_platform",
]

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_gate(import_lines):
    """Run import_lines in a fresh interpreter; return its stdout ('CLEAN'/'LEAK:...')."""
    code = textwrap.dedent(
        f"""
        import importlib, sys
        for _m in {import_lines!r}:
            importlib.import_module(_m)
        _bad = [m for m in {FORBIDDEN!r}
                if any(k == m or k.startswith(m + '.') for k in sys.modules)]
        print('LEAK:' + ','.join(_bad) if _bad else 'CLEAN')
        """
    )
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _REPO + (os.pathsep + existing if existing else "")
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    return out


def test_cleanroom_no_provider_llm_ui_leak():
    """ba2_common + interfaces + TradeConditions + TradeRiskManagement (+ the rest of
    the public surface) pull NONE of the forbidden provider/LLM/UI/live modules."""
    out = _run_gate(CLEANROOM_MODULES)
    assert out.stdout.strip() == "CLEAN", (
        f"clean-room gate leaked: {out.stdout.strip()!r}\nstderr={out.stderr}"
    )


def test_gate_is_non_vacuous():
    """Sanity: deliberately importing a forbidden package (fmpsdk, which IS installed)
    makes the identical sys.modules check report LEAK. Proves the CLEAN result above
    is real and not a false pass from the package merely being absent."""
    out = _run_gate(["ba2_common", "fmpsdk"])
    assert out.stdout.strip().startswith("LEAK:fmpsdk"), (
        f"gate failed to detect a deliberate leak: {out.stdout.strip()!r}\n"
        f"stderr={out.stderr}"
    )
