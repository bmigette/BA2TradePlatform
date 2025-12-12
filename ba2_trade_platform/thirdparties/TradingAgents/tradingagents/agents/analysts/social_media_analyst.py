from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from ...prompts import format_analyst_prompt, get_prompt


def create_social_media_analyst(llm, toolkit, tools, parallel_tool_calls=False):
    """
    Create social media analyst node.
    
    Args:
        llm: Language model
        toolkit: Toolkit instance (backward compat, not used)
        tools: List of pre-defined tool objects
        parallel_tool_calls: Whether to enable parallel tool calling (default False)
    """
    def social_media_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]

        # Get system prompt from centralized prompts
        system_message = get_prompt("social_media_analyst")

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

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "sentiment_report": report,
        }

    return social_media_analyst_node
