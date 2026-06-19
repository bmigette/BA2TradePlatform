import pytest

def test_unconfigured_instance_resolver_raises():
    from ba2_common.core.instance_resolver import (
        get_instance_resolver, set_instance_resolver, InstanceResolverNotConfigured)
    with pytest.raises(InstanceResolverNotConfigured):
        get_instance_resolver().get_expert_instance(1)

    class Fake:
        def get_expert_instance(self, i): return f"expert-{i}"
        def get_account_instance(self, i): return f"acct-{i}"
        def get_account_instance_from_transaction(self, t): return "acct-from-txn"
    set_instance_resolver(Fake())
    assert get_instance_resolver().get_expert_instance(7) == "expert-7"

def test_unconfigured_llm_service_raises():
    from ba2_common.core.interfaces.LLMServiceInterface import (
        get_llm_service, set_llm_service, LLMServiceInterface, LLMServiceNotConfigured)
    with pytest.raises(LLMServiceNotConfigured):
        get_llm_service().create_llm("openai/gpt5")

    class FakeLLM(LLMServiceInterface):
        def create_llm(self, model_selection, **k): return ("llm", model_selection)
        def do_llm_call_with_websearch(self, model_selection, prompt, **k): return "answer"
    set_llm_service(FakeLLM())
    assert get_llm_service().do_llm_call_with_websearch("openai/gpt5", "hi") == "answer"
