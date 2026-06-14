"""Live ``LLMServiceInterface`` implementation for the seam defined in
``ba2_common.core.interfaces`` (Phase 6 Task 3).

``ba2_common`` is kept free of langchain/openai/ModelFactory: package expert code
(e.g. ``PennyMomentumTrader``'s mixins) that needs an LLM goes through the
abstract ``LLMServiceInterface`` instead of importing the live LLM stack. The
live platform supplies the concrete implementation here, ``ModelFactoryLLMService``,
which adapts the live ``ModelFactory.create_llm`` / ``do_llm_call_with_websearch``
classmethods **param-for-param**. It is injected once at startup via
``ba2_common.core.interfaces.set_llm_service(ModelFactoryLLMService())`` (wired in
``core/seam_wiring.py``).

The ``create_llm`` / ``do_llm_call_with_websearch`` parameter lists mirror both the
``LLMServiceInterface`` abstract signature and ``ModelFactory`` exactly (verified
against ``core/ModelFactory.py``: ``create_llm`` and ``do_llm_call_with_websearch``),
so no kwargs are dropped and usage tracking
(``expert_instance_id`` / ``account_id`` / ``symbol`` / ``market_analysis_id`` /
``smart_risk_manager_job_id``) keeps flowing to the live ``LLMUsageTracker``.

Note on the import path: ``set_llm_service`` / ``get_llm_service`` /
``LLMServiceInterface`` are exported at the *package* level
``ba2_common.core.interfaces`` (the package ``__init__`` re-binds the name
``LLMServiceInterface`` to the class), so the clean import is
``from ba2_common.core.interfaces import LLMServiceInterface`` — not the
submodule path.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ba2_common.core.interfaces import LLMServiceInterface

from .ModelFactory import ModelFactory


class ModelFactoryLLMService(LLMServiceInterface):
    """``LLMServiceInterface`` backed by the live ``ModelFactory``.

    Forwards verbatim to ``ModelFactory.create_llm`` /
    ``ModelFactory.do_llm_call_with_websearch`` (both classmethods).
    """

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
        return ModelFactory.create_llm(
            model_selection,
            temperature=temperature,
            streaming=streaming,
            callbacks=callbacks,
            model_kwargs=model_kwargs,
            track_usage=track_usage,
            use_case=use_case,
            expert_instance_id=expert_instance_id,
            account_id=account_id,
            symbol=symbol,
            market_analysis_id=market_analysis_id,
            smart_risk_manager_job_id=smart_risk_manager_job_id,
            **extra_kwargs,
        )

    def do_llm_call_with_websearch(
        self,
        model_selection: str,
        prompt: str,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> str:
        return ModelFactory.do_llm_call_with_websearch(
            model_selection,
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
