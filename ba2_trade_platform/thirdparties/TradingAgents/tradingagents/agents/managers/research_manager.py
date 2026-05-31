from ...prompts import format_research_manager_prompt
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response
from ba2_trade_platform.logger import logger
from ..utils.structured_outputs import InvestmentJudgeVerdict, render_investment_judge_verdict


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
            for rec in past_memories:
                past_memory_str += rec["recommendation"] + "\n\n"

        prompt = format_research_manager_prompt(
            past_memory_str=past_memory_str,
            history=history,
        )

        structured_verdict = None
        response_text = None
        try:
            structured_llm = llm.with_structured_output(InvestmentJudgeVerdict)
            structured_verdict = structured_llm.invoke(prompt)
            response_text = render_investment_judge_verdict(structured_verdict)
        except Exception as e:
            logger.warning(f"Research manager structured-output failed ({type(e).__name__}: {e}); falling back to text")
            response = llm.invoke(prompt)
            response_text = extract_text_from_llm_response(response.content)

        new_investment_debate_state = {
            "judge_decision": response_text,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": response_text,
            "count": investment_debate_state["count"],
        }
        # Preserve any structured arguments captured by bull/bear in this debate
        for k in ("bull_argument_structured", "bear_argument_structured"):
            if k in investment_debate_state:
                new_investment_debate_state[k] = investment_debate_state[k]
        if structured_verdict is not None:
            new_investment_debate_state["judge_verdict_structured"] = structured_verdict.model_dump()

        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": response_text,
            "investment_judge_verdict": structured_verdict.model_dump() if structured_verdict else {},
        }

    return research_manager_node
