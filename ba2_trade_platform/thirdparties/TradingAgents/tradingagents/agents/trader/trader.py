import functools
from ...prompts import format_trader_context_prompt, format_trader_system_prompt
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response
from ba2_trade_platform.logger import logger
from ..utils.structured_outputs import TraderDecision, render_trader_decision


def create_trader(llm, memory):
    def trader_node(state, name):
        company_name = state["company_of_interest"]
        investment_plan = state["investment_plan"]
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"

        past_memory_str = "No past memories found."
        if memory is not None:
            past_memories = memory.get_memories(curr_situation, n_matches=2, aggregate_chunks=False)
            if past_memories:
                past_memory_str = ""
                for i, rec in enumerate(past_memories, 1):
                    past_memory_str += rec["recommendation"] + "\n\n"

        messages = [
            {
                "role": "system",
                "content": format_trader_system_prompt(past_memory_str=past_memory_str),
            },
            {
                "role": "user",
                "content": format_trader_context_prompt(
                    company_name=company_name,
                    investment_plan=investment_plan,
                ),
            },
        ]

        # Try structured output first (v0.2.4 pattern). Falls back to free-form
        # text if the provider/model doesn't support tool-calling/json-mode or
        # if the call/parse fails — the rest of the graph keeps working either
        # way because it only reads state["trader_investment_plan"] (text).
        decision_text = None
        try:
            structured_llm = llm.with_structured_output(TraderDecision)
            decision: TraderDecision = structured_llm.invoke(messages)
            decision_text = render_trader_decision(decision)
            return {
                "messages": [],
                "trader_investment_plan": decision_text,
                "trader_decision": decision.model_dump(),
                "sender": name,
            }
        except Exception as e:
            logger.warning(
                f"Trader structured-output failed ({type(e).__name__}: {e}); falling back to text"
            )

        result = llm.invoke(messages)
        return {
            "messages": [result],
            "trader_investment_plan": extract_text_from_llm_response(result.content),
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
