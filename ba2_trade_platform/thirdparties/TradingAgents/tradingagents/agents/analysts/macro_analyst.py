from langchain_core.messages import SystemMessage, HumanMessage
from ...prompts import format_analyst_prompt, get_prompt
from ..utils.prefetch_context import gather_macro_context
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response


def create_macro_analyst(llm, toolkit, tools, parallel_tool_calls=False):
    """Create the macro analyst node.

    Pre-fetch (non-agentic): economic indicators + yield curve + Fed calendar are
    gathered up-front and injected into the prompt; the LLM produces the report in a
    single turn. ``tools`` is accepted for signature compatibility but unused.
    """
    def macro_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        system_message = get_prompt("macro_analyst")
        prompt_config = format_analyst_prompt(
            system_prompt=system_message,
            tool_names=[],
            current_date=current_date,
            ticker=ticker,
            prefetch=True,
        )

        context = gather_macro_context(toolkit, current_date)
        human = (
            f"Below is the macroeconomic data gathered as of {current_date}. Analyze it and "
            f"produce your macro report (consider its implications for {ticker}).\n\n{context}"
        )

        result = llm.invoke([
            SystemMessage(content=prompt_config["system"]),
            HumanMessage(content=human),
        ])

        report = extract_text_from_llm_response(result.content)

        return {
            "messages": [result],
            "macro_report": report,
            "macro_input": f"{prompt_config['system']}\n\n===== DATA PROVIDED TO ANALYST =====\n\n{human}",
        }

    return macro_analyst_node
