from langchain_core.messages import AIMessage
import time
import json
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from ...prompts import format_bear_researcher_prompt


def create_bear_researcher(llm, memory):
    def bear_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bear_history = investment_debate_state.get("bear_history", "")

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

        prompt = format_bear_researcher_prompt(
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

        argument = f"Bear Analyst: {response.content}"

        # Store bear messages as a list for proper conversation display
        bear_messages = investment_debate_state.get("bear_messages", [])
        bear_messages.append(argument)

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bear_messages": bear_messages,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "bull_messages": investment_debate_state.get("bull_messages", []),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
            "judge_decision": investment_debate_state.get("judge_decision", "")
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
