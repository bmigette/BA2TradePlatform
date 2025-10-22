from langchain_core.messages import AIMessage
import time
import json
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from ...prompts import format_bull_researcher_prompt


def create_bull_researcher(llm, memory):
    def bull_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bull_history = investment_debate_state.get("bull_history", "")

        current_response = investment_debate_state.get("current_response", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        macro_report = state.get("macro_report", "")

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}\n\n{macro_report}"
        past_memories = memory.get_memories(curr_situation, n_matches=2, aggregate_chunks=False)

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        prompt = format_bull_researcher_prompt(
            market_research_report=market_research_report,
            sentiment_report=sentiment_report,
            news_report=news_report,
            fundamentals_report=fundamentals_report,
            macro_report=macro_report,
            history=history,
            current_response=current_response,
            past_memory_str=past_memory_str
        )

        response = llm.invoke(prompt)

        argument = f"Bull Analyst: {response.content}"

        # Store bull messages as a list for proper conversation display
        bull_messages = investment_debate_state.get("bull_messages", [])
        bull_messages.append(argument)

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bull_messages": bull_messages,
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bear_messages": investment_debate_state.get("bear_messages", []),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
            "judge_decision": investment_debate_state.get("judge_decision", "")
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bull_node
