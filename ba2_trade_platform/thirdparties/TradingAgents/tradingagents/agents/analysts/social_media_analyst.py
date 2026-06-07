from langchain_core.messages import SystemMessage, HumanMessage
from ...prompts import format_analyst_prompt, get_prompt
from ..utils.prefetch_context import gather_social_context
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response


def create_social_media_analyst(llm, toolkit, tools, parallel_tool_calls=False):
    """Create the social media analyst node.

    Pre-fetch (non-agentic): social sentiment + recent company news are gathered
    up-front and injected into the prompt; the LLM produces the report in a single
    turn. ``tools`` is accepted for signature compatibility but unused.
    """
    def social_media_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        system_message = get_prompt("social_media_analyst")
        prompt_config = format_analyst_prompt(
            system_prompt=system_message,
            tool_names=[],
            current_date=current_date,
            ticker=ticker,
            prefetch=True,
        )

        context = gather_social_context(toolkit, ticker, current_date)
        human = (
            f"Below is the social-media sentiment and recent company news gathered for "
            f"{ticker} as of {current_date}. Analyze it and produce your sentiment report.\n\n{context}"
        )

        result = llm.invoke([
            SystemMessage(content=prompt_config["system"]),
            HumanMessage(content=human),
        ])

        report = extract_text_from_llm_response(result.content)

        return {
            "messages": [result],
            "sentiment_report": report,
        }

    return social_media_analyst_node
