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
from ._leakgate import PROBE_PATH, check_leak, probe_verdict, MARK

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

# fmpsdk / nicegui / langchain_core are REAL installed packages in this venv, and
# ba2_providers / ba2_experts / ba2_trade_platform are all importable in the probe
# (see _leakgate.PROBE_PATH) -- so every entry here is a module the child COULD
# have imported, which is what makes the gate non-vacuous.
FORBIDDEN = [
    "ba2_providers",
    "ba2_experts",
    "langchain",
    "langchain_core",
    "fmpsdk",
    "nicegui",
    "ba2_trade_platform",
]

def test_cleanroom_no_provider_llm_ui_leak():
    """ba2_common + interfaces + TradeConditions + TradeRiskManagement (+ the rest of
    the public surface) pull NONE of the forbidden provider/LLM/UI/live modules."""
    verdict = check_leak(CLEANROOM_MODULES, FORBIDDEN)
    assert verdict == "CLEAN", f"clean-room gate leaked: {verdict!r}"


def test_gate_is_non_vacuous():
    """Sanity: deliberately importing a forbidden package (fmpsdk, which IS installed)
    makes the identical sys.modules check report LEAK. Proves the CLEAN result above
    is real and not a false pass from the package merely being absent."""
    verdict = check_leak(["ba2_common", "fmpsdk"], FORBIDDEN)
    assert verdict.startswith("LEAK:fmpsdk"), (
        f"gate failed to detect a deliberate leak: {verdict!r}"
    )


def test_gate_catches_a_real_live_platform_leak():
    """Stronger non-vacuity control: point the SAME gate at a module that really
    does have the back-edges (`ba2_trade_platform.core.utils`, the live shim, which
    pulls providers + experts + langchain + fmpsdk). If this ever reports CLEAN the
    gate has stopped discriminating and every other result here is worthless."""
    verdict = check_leak("ba2_trade_platform.core.utils", FORBIDDEN)
    assert verdict.startswith("LEAK:"), (
        "the gate reported a known-leaky module as clean -- it no longer "
        f"discriminates: {verdict!r}"
    )
    for expected in ("ba2_providers", "ba2_experts", "ba2_trade_platform"):
        assert expected in verdict, f"gate missed the {expected} back-edge: {verdict!r}"


def test_probe_resolves_ba2_common_to_this_checkout():
    """Guard against the failure mode this gate is built on top of: the probe must
    import the ba2_common living in THIS repo, not a stale editable install (the
    venv's .pth files point at sibling checkouts that need not exist) and not
    nothing at all. A probe that cannot import ba2_common must fail loudly, never
    report CLEAN."""
    resolved = probe_verdict(
        f"import ba2_common; print({MARK!r} + ba2_common.__file__)"
    )
    assert resolved.startswith(PROBE_PATH[0]), (
        f"probe resolved ba2_common to {resolved!r}, expected it under "
        f"{PROBE_PATH[0]!r}"
    )
