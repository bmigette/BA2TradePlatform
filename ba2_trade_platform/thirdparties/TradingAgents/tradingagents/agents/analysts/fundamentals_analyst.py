from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
import time
import json
from ...prompts import format_analyst_prompt, get_prompt


def create_fundamentals_analyst(llm, toolkit):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        company_name = state["company_of_interest"]

        # Use new toolkit methods for fundamentals data
        tools = [
            toolkit.get_balance_sheet,
            toolkit.get_income_statement,
            toolkit.get_cashflow_statement,
            toolkit.get_insider_transactions,
            toolkit.get_insider_sentiment,
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
