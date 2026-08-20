"""Import-smoke tests for ba2_common's foundation leaf modules.

Crucially, models_registry is data-only: it lists langchain class *names* as
plain strings but must NOT import langchain. We assert that in a subprocess via
sys.modules (Amendment A1: the leak gate checks what an import PULLS, not whether
the dep happens to be installed in this venv).
"""
from ._leakgate import assert_no_leak


def test_core_leaf_modules_import():
    import ba2_common.core.types          # noqa: F401
    import ba2_common.core.option_types   # noqa: F401
    import ba2_common.core.date_utils     # noqa: F401
    import ba2_common.core.text_utils     # noqa: F401
    import ba2_common.core.provider_utils  # noqa: F401
    import ba2_common.core.models_registry  # noqa: F401  data-only; must NOT pull langchain


def test_models_registry_does_not_pull_langchain():
    """models_registry must stay langchain-free so the LLM seam can share it."""
    assert_no_leak(
        "ba2_common.core.models_registry",
        ["langchain", "langchain_core", "ba2_providers", "ba2_experts"],
    )
