from ...prompts import format_bear_researcher_prompt, format_past_memories, NO_PAST_MEMORIES_TEXT
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response
from ba2_trade_platform.logger import logger
from ..utils.structured_outputs import ResearcherArgument, render_researcher_argument


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

        past_memory_str = NO_PAST_MEMORIES_TEXT
        if memory is not None:
            past_memory_str = format_past_memories(
                memory.get_memories(curr_situation, aggregate_chunks=False)
            )

        prompt = format_bear_researcher_prompt(
            market_research_report=market_research_report,
            sentiment_report=sentiment_report,
            news_report=news_report,
            fundamentals_report=fundamentals_report,
            macro_report=macro_report,
            history=history,
            current_response=current_response,
            past_memory_str=past_memory_str,
        )

        structured_arg = None
        response_text = None
        try:
            structured_llm = llm.with_structured_output(ResearcherArgument)
            structured_arg = structured_llm.invoke(prompt)
            if structured_arg.stance != "BEAR":
                structured_arg = structured_arg.model_copy(update={"stance": "BEAR"})
            response_text = render_researcher_argument(structured_arg)
        except Exception as e:
            logger.warning(f"Bear researcher structured-output failed ({type(e).__name__}: {e}); falling back to text")
            response = llm.invoke(prompt)
            response_text = extract_text_from_llm_response(response.content)

        argument = f"Bear Analyst: {response_text}"

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
            "judge_decision": investment_debate_state.get("judge_decision", ""),
        }
        if structured_arg is not None:
            new_investment_debate_state["bear_argument_structured"] = structured_arg.model_dump()

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
