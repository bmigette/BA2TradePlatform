import time
import json
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
from ...prompts import format_research_manager_prompt
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response


def create_research_manager(llm, memory):
    def research_manager_node(state) -> dict:
        history = state["investment_debate_state"].get("history", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        investment_debate_state = state["investment_debate_state"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"

        past_memory_str = ""
        if memory is not None:
            past_memories = memory.get_memories(curr_situation, n_matches=2, aggregate_chunks=False)
            for i, rec in enumerate(past_memories, 1):
                past_memory_str += rec["recommendation"] + "\n\n"

        prompt = format_research_manager_prompt(
            past_memory_str=past_memory_str,
            history=history
        )
        response = llm.invoke(prompt)
        
        # Extract text from response (handles Gemini's list format)
        response_text = extract_text_from_llm_response(response.content)

        new_investment_debate_state = {
            "judge_decision": response_text,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": response_text,
            "count": investment_debate_state["count"],
        }

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": response_text,
        }

    return research_manager_node
