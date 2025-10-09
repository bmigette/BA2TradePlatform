from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
import time
import json
from ...prompts import format_analyst_prompt, get_prompt


def create_fundamentals_analyst(llm, toolkit):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]

        # Wrap toolkit methods with @tool decorator
        @tool
        def get_balance_sheet(symbol: str, end_date: str, lookback_periods: int = None) -> str:
            """Get balance sheet data for a company."""
            return toolkit.get_balance_sheet(symbol, end_date, lookback_periods)
        
        @tool
        def get_income_statement(symbol: str, end_date: str, lookback_periods: int = None) -> str:
            """Get income statement data for a company."""
            return toolkit.get_income_statement(symbol, end_date, lookback_periods)
        
        @tool
        def get_cashflow_statement(symbol: str, end_date: str, lookback_periods: int = None) -> str:
            """Get cash flow statement data for a company."""
            return toolkit.get_cashflow_statement(symbol, end_date, lookback_periods)
        
        @tool
        def get_insider_transactions(symbol: str, end_date: str, lookback_days: int = None) -> str:
            """Get insider trading transactions for a company."""
            return toolkit.get_insider_transactions(symbol, end_date, lookback_days)
        
        @tool
        def get_insider_sentiment(symbol: str, end_date: str, lookback_days: int = None) -> str:
            """Get insider sentiment analysis for a company."""
            return toolkit.get_insider_sentiment(symbol, end_date, lookback_days)

        # Use wrapped tools
        tools = [
            get_balance_sheet,
            get_income_statement,
            get_cashflow_statement,
            get_insider_transactions,
            get_insider_sentiment,
        ]

        # Get system prompt from centralized prompts
        system_message = get_prompt("fundamentals_analyst")

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
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
