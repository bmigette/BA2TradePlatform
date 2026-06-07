from langchain_core.messages import SystemMessage, HumanMessage
from ...prompts import format_analyst_prompt, get_prompt
from ..utils.prefetch_context import gather_fundamentals_context
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response


def create_fundamentals_analyst(llm, toolkit, tools, parallel_tool_calls=False):
    """Create the fundamentals analyst node.

    Pre-fetch (non-agentic): all fundamental data is gathered up-front via the
    toolkit and injected into the prompt, then the LLM produces the report in a
    single turn. ``tools`` is accepted for signature compatibility but unused.
    """
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        system_message = get_prompt("fundamentals_analyst")
        prompt_config = format_analyst_prompt(
            system_prompt=system_message,
            tool_names=[],
            current_date=current_date,
            ticker=ticker,
            prefetch=True,
        )

        context = gather_fundamentals_context(toolkit, ticker, current_date)
        human = (
            f"Below is the comprehensive fundamental data gathered for {ticker} as of "
            f"{current_date}. Analyze it and produce your fundamentals report.\n\n{context}"
        )

        result = llm.invoke([
            SystemMessage(content=prompt_config["system"]),
            HumanMessage(content=human),
        ])

        report = extract_text_from_llm_response(result.content)

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
