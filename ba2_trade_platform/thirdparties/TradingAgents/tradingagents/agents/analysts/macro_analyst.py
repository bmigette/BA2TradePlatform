from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
import time
import json
from ...prompts import format_analyst_prompt, get_prompt


def create_macro_analyst(llm, toolkit):
    def macro_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]

        # Wrap toolkit methods with @tool decorator
        @tool
        def get_economic_indicators(end_date: str, lookback_days: int = None) -> str:
            """Get economic indicators like GDP, unemployment, inflation, etc."""
            return toolkit.get_economic_indicators(end_date, lookback_days)
        
        @tool
        def get_yield_curve(end_date: str, lookback_days: int = None) -> str:
            """Get Treasury yield curve data."""
            return toolkit.get_yield_curve(end_date, lookback_days)
        
        @tool
        def get_fed_calendar(end_date: str, lookback_days: int = None) -> str:
            """Get Federal Reserve calendar and meeting minutes."""
            return toolkit.get_fed_calendar(end_date, lookback_days)

        # Use wrapped tools
        tools = [
            get_economic_indicators,
            get_yield_curve,
            get_fed_calendar,
        ]

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

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "macro_report": report,
        }

    return macro_analyst_node