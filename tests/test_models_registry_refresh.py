"""Registry refresh 2026-08: new models present, EOL models gone, structure sane."""
import pytest

from ba2_common.core.models_registry import MODELS, PROVIDER_CONFIG

NEW = {
    "gpt5.6_sol": ("openai", "gpt-5.6-sol"),
    "gpt5.6_terra": ("openai", "gpt-5.6-terra"),
    "gpt5.6_luna": ("openai", "gpt-5.6-luna"),
    "grok4.5": ("xai", "grok-4.5"),
    "kimi_k3": ("moonshot", "kimi-k3"),
    "kimi_k2.7_code": ("moonshot", "kimi-k2.7-code"),
    "kimi_k2.7_code_highspeed": ("moonshot", "kimi-k2.7-code-highspeed"),
    "deepseek_v4_flash": ("deepseek", "deepseek-v4-flash"),
    "deepseek_v4_pro": ("deepseek", "deepseek-v4-pro"),
}

REMOVED = [
    "kimi_k2", "kimi_k2_thinking", "kimi_k2_thinking_turbo",
    "kimi_k2.5", "kimi_k2.5-nonthinking", "kimi_k1.5",
    "deepseek_v3.2", "deepseek_chat", "deepseek_reasoner", "deepseek_coder",
    "grok4.1_fast", "grok4.1_fast_reasoning",
    "o1", "o1_mini",
]

# Dated but NOT retired — must survive the cleanup.
KEPT = ["kimi_k2.6", "kimi_k2.6-nonthinking", "grok4", "grok4_fast",
        "grok4_fast_reasoning", "grok3", "grok3_mini", "gpt4o", "gpt4o_mini",
        "o3_mini", "o4_mini", "gpt5", "gpt5.4"]


@pytest.mark.parametrize("friendly,provider_pname", NEW.items())
def test_new_models_present(friendly, provider_pname):
    provider, pname = provider_pname
    entry = MODELS[friendly]
    assert entry["native_provider"] == provider
    assert entry["provider_names"][provider] == pname
    # native-only until the gateways list them
    assert set(entry["provider_names"]) == {provider}


@pytest.mark.parametrize("friendly", REMOVED)
def test_eol_models_removed(friendly):
    assert friendly not in MODELS


@pytest.mark.parametrize("friendly", KEPT)
def test_dated_but_live_models_kept(friendly):
    assert friendly in MODELS


def test_registry_structure_sane():
    for friendly, entry in MODELS.items():
        assert entry["native_provider"] in PROVIDER_CONFIG, friendly
        for prov in entry["provider_names"]:
            assert prov in PROVIDER_CONFIG, (friendly, prov)
        assert entry["context_size"] > 0, friendly


def test_preferred_order_lists_have_no_removed_models():
    import test_tools.test_models as tm
    for provider, order in tm.PREFERRED_ORDER.items():
        for friendly in order:
            assert friendly in MODELS, f"{provider}: stale preferred name {friendly}"
