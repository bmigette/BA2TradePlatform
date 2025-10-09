from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
import time
import json
from ...prompts import format_analyst_prompt, get_prompt


def create_news_analyst(llm, toolkit):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        # Wrap toolkit method with @tool decorator
        @tool
        def get_global_news(symbol: str, end_date: str, lookback_days: int = None) -> str:
            """Get global news articles about a company."""
            return toolkit.get_global_news(symbol, end_date, lookback_days)

        # Use wrapped tools
        tools = [
            get_global_news,
        ]

        # Get system prompt from centralized prompts
        system_message = get_prompt("news_analyst")

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

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "news_report": report,
        }

    return news_analyst_node
