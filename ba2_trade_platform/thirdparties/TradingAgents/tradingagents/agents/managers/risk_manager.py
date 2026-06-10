from ...prompts import format_risk_manager_prompt, format_past_memories, NO_PAST_MEMORIES_TEXT
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response
from ba2_trade_platform.logger import logger
from ..utils.structured_outputs import RiskJudgeVerdict, render_risk_judge_verdict


def create_risk_manager(llm, memory, strategy_notes: str = ""):
    def risk_manager_node(state) -> dict:
        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        market_research_report = state["market_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        sentiment_report = state["sentiment_report"]
        macro_report = state.get("macro_report", "")
        trader_plan = state["investment_plan"]

        # Include the macro report in the situation used for past-lesson matching (PR-4)
        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}\n\n{macro_report}"

        past_memory_str = NO_PAST_MEMORIES_TEXT
        if memory is not None:
            past_memory_str = format_past_memories(
                memory.get_memories(curr_situation, aggregate_chunks=False)
            )

        prompt = format_risk_manager_prompt(
            trader_plan=trader_plan,
            past_memory_str=past_memory_str,
            history=history,
            strategy_notes=strategy_notes,
        )

        structured_verdict = None
        response_text = None
        try:
            structured_llm = llm.with_structured_output(RiskJudgeVerdict)
            structured_verdict = structured_llm.invoke(prompt)
            response_text = render_risk_judge_verdict(structured_verdict)
        except Exception as e:
            logger.warning(f"Risk manager structured-output failed ({type(e).__name__}: {e}); falling back to text")
            response = llm.invoke(prompt)
            response_text = extract_text_from_llm_response(response.content)

        new_risk_debate_state = {
            "judge_decision": response_text,
            "history": risk_debate_state["history"],
            "risky_history": risk_debate_state["risky_history"],
            "safe_history": risk_debate_state["safe_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_risky_response": risk_debate_state["current_risky_response"],
            "current_safe_response": risk_debate_state["current_safe_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }
        if structured_verdict is not None:
            new_risk_debate_state["risk_verdict_structured"] = structured_verdict.model_dump()

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": response_text,
            "risk_judge_verdict": structured_verdict.model_dump() if structured_verdict else {},
        }

    return risk_manager_node
