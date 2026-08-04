from langchain_core.messages import SystemMessage, HumanMessage
from ...prompts import format_analyst_prompt, get_prompt
from ..utils.prefetch_context import gather_fundamentals_context
from ba2_trade_platform.core.text_utils import extract_text_from_llm_response
from ba2_trade_platform.core.prompt_caching import apply_anthropic_prompt_caching


def create_fundamentals_analyst(llm, toolkit, tools, parallel_tool_calls=False):
    """Create the fundamentals analyst node.

    HYBRID: all fundamental data is pre-fetched and injected (deterministic, one
    round-trip of data gathering), and the COMPUTE tools (valuation, bond,
    arithmetic) are bound so the LLM can run exact math with its own assumptions
    instead of computing in its head. Report-only turns end the loop (no tool
    calls); tool calls route through tools_fundamentals and back.
    """
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        system_message = get_prompt("fundamentals_analyst")
        prompt_config = format_analyst_prompt(
            system_prompt=system_message,
            tool_names=[tool.name for tool in tools],
            current_date=current_date,
            ticker=ticker,
            prefetch=True,
        )

        # Google models don't support parallel_tool_calls parameter.
        # Bind + invoke directly (messages are built explicitly below) rather
        # than piping through a ChatPromptTemplate.
        is_google = "google" in type(llm).__module__.lower()
        if is_google:
            bound_llm = llm.bind_tools(tools)
        else:
            bound_llm = llm.bind_tools(tools, parallel_tool_calls=parallel_tool_calls)

        def _gather_human():
            context = gather_fundamentals_context(toolkit, ticker, current_date)
            return (
                f"Below is the comprehensive fundamental data gathered for {ticker} as of "
                f"{current_date}. Analyze it and produce your fundamentals report. The "
                f"valuation snapshot uses DEFAULT assumptions — if your view differs, re-run "
                f"the valuation tools with your own explicit assumptions instead of "
                f"computing in your head.\n\n{context}"
            )

        # Re-entry after tools_fundamentals ran: the tool calls and their results
        # are already in state["messages"] and MUST reach the LLM (otherwise it
        # never sees the ToolMessages and re-issues the same calls until the
        # recursion limit). Do NOT re-gather the context (duplicate provider
        # calls) — recover the human block from the input snapshot this node
        # wrote on the first pass.
        _MARKER = "===== DATA PROVIDED TO ANALYST =====\n\n"
        reentry = any(getattr(m, "type", None) == "tool"
                      for m in state.get("messages", []))
        if reentry:
            snapshot = state.get("fundamentals_input", "")
            human = snapshot.split(_MARKER, 1)[1] if _MARKER in snapshot \
                else _gather_human()  # marker absent (older state) — re-gather
            messages = [
                SystemMessage(content=prompt_config["system"]),
                HumanMessage(content=human),
            ] + list(state["messages"])
        else:
            human = _gather_human()
            messages = [
                SystemMessage(content=prompt_config["system"]),
                HumanMessage(content=human),
            ]

        messages = apply_anthropic_prompt_caching(messages, llm)
        result = bound_llm.invoke(messages)

        report = ""
        if len(result.tool_calls) == 0:
            report = extract_text_from_llm_response(result.content)

        return {
            "messages": [result],
            "fundamentals_report": report,
            "fundamentals_input": f"{prompt_config['system']}\n\n===== DATA PROVIDED TO ANALYST =====\n\n{human}",
        }

    return fundamentals_analyst_node
