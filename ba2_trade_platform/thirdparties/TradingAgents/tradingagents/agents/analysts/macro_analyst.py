from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from ...prompts import format_analyst_prompt, get_prompt
# Native tool call parsing for Kimi/DeepSeek models (can be removed when NagaAI fixes their API)
from ba2_trade_platform.core.native_tool_call_parser import wrap_llm_response_with_native_parsing


def create_macro_analyst(llm, toolkit, tools, parallel_tool_calls=False):
    """
    Create macro analyst node.
    
    Args:
        llm: Language model
        toolkit: Toolkit instance (backward compat, not used)
        tools: List of pre-defined tool objects
        parallel_tool_calls: Whether to enable parallel tool calling (default False)
    """
    def macro_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]

        # Get system prompt from centralized prompts
        system_message = get_prompt("macro_analyst")

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

        chain = prompt | llm.bind_tools(tools, parallel_tool_calls=parallel_tool_calls)

        result = chain.invoke(state["messages"])
        # Apply native tool call parsing for Kimi/DeepSeek models
        result = wrap_llm_response_with_native_parsing(result, getattr(llm, 'model_name', ''))

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "macro_report": report,
        }

    return macro_analyst_node