from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage
import time
import json
from ...prompts import format_analyst_prompt, get_prompt
from ..utils.prefetch_context import gather_market_context
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response


def create_market_analyst(llm, toolkit, tools, parallel_tool_calls=False):
    """
    Create market analyst node with pre-defined tools.
    
    Args:
        llm: Language model for the analyst
        toolkit: Toolkit instance — used to pre-compute the deterministic
            risk-statistics block via gather_market_context(toolkit, ...)
        tools: List of pre-defined tool objects to use
        parallel_tool_calls: Whether to enable parallel tool calls (default False)
    """
    def market_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]

        # Get system prompt from centralized prompts
        system_message = get_prompt("market_analyst")

        # Format analyst collaboration prompt
        prompt_config = format_analyst_prompt(
            system_prompt=system_message,
            tool_names=[tool.name for tool in tools],
            current_date=current_date,
            ticker=ticker
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", prompt_config["system"]),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        # Google models don't support parallel_tool_calls parameter
        is_google = "google" in type(llm).__module__.lower()
        if is_google:
            chain = prompt | llm.bind_tools(tools)
        else:
            chain = prompt | llm.bind_tools(tools, parallel_tool_calls=parallel_tool_calls)

        # Deterministic risk-stats block (injected, no tool call needed): exact
        # realized vol / drawdown / VaR / beta figures for the analyst to read.
        # First entry only — on ReAct re-entries (tool results in state) the
        # block is already in the history; prepending again would accumulate
        # duplicate blocks.
        messages = list(state["messages"])
        reentry = any(getattr(m, "type", None) == "tool" for m in messages)
        if not reentry:
            risk_context = gather_market_context(toolkit, ticker, current_date)
            if risk_context:
                messages = [HumanMessage(
                    content=f"Pre-computed risk statistics for {ticker} as of {current_date} "
                            f"(deterministic, provider-computed — cite these exact figures, "
                            f"do not recompute them):\n\n{risk_context}"
                )] + messages

        result = chain.invoke(messages)

        report = ""

        if len(result.tool_calls) == 0:
            report = extract_text_from_llm_response(result.content)
       
        return {
            "messages": [result],
            "market_report": report,
            "market_input": prompt_config["system"],
        }

    return market_analyst_node
