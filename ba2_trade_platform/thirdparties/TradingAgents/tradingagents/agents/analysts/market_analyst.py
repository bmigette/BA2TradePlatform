from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
import time
import json
from ...prompts import format_analyst_prompt, get_prompt


def create_market_analyst(llm, toolkit):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]

        # Wrap toolkit methods with @tool decorator
        @tool
        def get_ohlcv_data(symbol: str, start_date: str, end_date: str, interval: str = None) -> str:
            """Get OHLCV stock price data."""
            return toolkit.get_ohlcv_data(symbol, start_date, end_date, interval)
        
        @tool
        def get_indicator_data(symbol: str, indicator: str, start_date: str, end_date: str, interval: str = None) -> str:
            """Get technical indicator data."""
            return toolkit.get_indicator_data(symbol, indicator, start_date, end_date, interval)

        # Use wrapped tools
        tools = [
            get_ohlcv_data,
            get_indicator_data,
        ]

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

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content
       
        return {
            "messages": [result],
            "market_report": report,
        }

    return market_analyst_node
