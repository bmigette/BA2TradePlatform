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
        def get_balance_sheet(symbol: str, frequency: str, end_date: str, lookback_periods: int = None) -> str:
            """Get balance sheet data for a company."""
            return toolkit.get_balance_sheet(symbol, frequency, end_date, lookback_periods)
        
        @tool
        def get_income_statement(symbol: str, frequency: str, end_date: str, lookback_periods: int = None) -> str:
            """Get income statement data for a company."""
            return toolkit.get_income_statement(symbol, frequency, end_date, lookback_periods)
        
        @tool
        def get_cashflow_statement(symbol: str, frequency: str, end_date: str, lookback_periods: int = None) -> str:
            """Get cash flow statement data for a company."""
            return toolkit.get_cashflow_statement(symbol, frequency, end_date, lookback_periods)
        
        @tool
        def get_insider_transactions(symbol: str, end_date: str, lookback_days: int = None) -> str:
            """Get insider trading transactions for a company."""
            return toolkit.get_insider_transactions(symbol, end_date, lookback_days)
        
        @tool
        def get_insider_sentiment(symbol: str, end_date: str, lookback_days: int = None) -> str:
            """Get insider sentiment analysis for a company."""
            return toolkit.get_insider_sentiment(symbol, end_date, lookback_days)
        
        @tool
        def get_past_earnings(symbol: str, end_date: str, lookback_periods: int = 8, frequency: str = "quarterly") -> str:
            """Get historical earnings data showing actual vs estimated EPS for the past 2 years."""
            return toolkit.get_past_earnings(symbol, end_date, lookback_periods, frequency)
        
        @tool
        def get_earnings_estimates(symbol: str, as_of_date: str, lookback_periods: int = 4, frequency: str = "quarterly") -> str:
            """Get forward earnings estimates from analysts for the next 4 quarters."""
            return toolkit.get_earnings_estimates(symbol, as_of_date, lookback_periods, frequency)

        # Use wrapped tools
        tools = [
            get_balance_sheet,
            get_income_statement,
            get_cashflow_statement,
            get_insider_transactions,
            get_insider_sentiment,
            get_past_earnings,
            get_earnings_estimates,
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
