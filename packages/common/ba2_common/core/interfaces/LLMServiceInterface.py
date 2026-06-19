"""LLM-service seam — keeps ba2_common free of langchain/openai.

Mirrors the two ModelFactory entry points package code uses
(ba2_trade_platform/core/ModelFactory.py: create_llm @135, do_llm_call_with_websearch @893).
Return types are Any so no langchain type leaks into ba2_common. The live platform
registers a ModelFactory-backed implementation via set_llm_service()."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Optional, List, Dict


class LLMServiceNotConfigured(RuntimeError):
    """Raised when expert code needs an LLM but no service is injected."""


class LLMServiceInterface(ABC):
    @abstractmethod
    def create_llm(
        self,
        model_selection: str,
        temperature: float = 0.0,
        streaming: Optional[bool] = None,
        callbacks: Optional[List[Any]] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
        track_usage: bool = True,
        use_case: str = "LangChain LLM Call",
        expert_instance_id: Optional[int] = None,
        account_id: Optional[int] = None,
        symbol: Optional[str] = None,
        market_analysis_id: Optional[int] = None,
        smart_risk_manager_job_id: Optional[int] = None,
        **extra_kwargs: Any,
    ) -> Any:
        """Return a chat-model object (langchain BaseChatModel in the live impl)."""

    @abstractmethod
    def do_llm_call_with_websearch(
        self,
        model_selection: str,
        prompt: str,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> str:
        """Return the model's text response with web search enabled."""


class _UnconfiguredLLMService(LLMServiceInterface):
    def create_llm(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured(
            "No LLMServiceInterface injected. The host app must call "
            "ba2_common.core.interfaces.LLMServiceInterface.set_llm_service(<svc>) at startup."
        )
    def do_llm_call_with_websearch(self, *a, **k):  # type: ignore[override]
        raise LLMServiceNotConfigured("No LLMServiceInterface injected.")


_llm_service: LLMServiceInterface = _UnconfiguredLLMService()


def set_llm_service(service: LLMServiceInterface) -> None:
    global _llm_service
    _llm_service = service


def get_llm_service() -> LLMServiceInterface:
    return _llm_service
