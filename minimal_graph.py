import operator
from typing import TypedDict, Annotated, List, Union
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.messages import BaseMessage, AIMessage, HumanMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI # Requires OPENAI_API_KEY environment variable

# --- 1. Define Tools (Outside the class, as they are pure functions) ---

@tool
def get_stock_price(ticker: str) -> str:
    """Retrieves the latest stock price for a given ticker symbol."""
    if ticker.upper() == "AAPL":
        return "AAPL PRICE: $175.50 (Data point 1 of 2: Price collected)"
    elif ticker.upper() == "TSLA":
        return "TSLA PRICE: $250.00 (Data point 1 of 2: Price collected)"
    return f"Could not find price for {ticker}. Try another ticker."

@tool
def get_financial_news(ticker: str) -> str:
    """Retrieves recent news headlines for a given ticker symbol."""
    if ticker.upper() == "AAPL":
        return "AAPL NEWS: Strong earnings report and new product launch rumors. (Data point 2 of 2: News collected)"
    elif ticker.upper() == "TSLA":
        return "TSLA NEWS: Regulatory approval for new factory and a major recall. (Data point 2 of 2: News collected)"
    return f"No recent news for {ticker}. Data incomplete."

ALL_TOOLS = [get_stock_price, get_financial_news]

# --- 2. Define the Graph State ---

class AgentState(TypedDict):
    """Represents the state of the agent in the graph."""
    messages: Annotated[List[BaseMessage], add_messages]
    iterations: int
    max_iterations: int

# ----------------------------------------------------------------------
# --- 3. Trading Agent Class ---
# ----------------------------------------------------------------------

class TradingAgentGraph:
    """
    A LangGraph-based agent for iterative financial data collection and trading recommendation.
    """
    
    # Define the System Prompt as a class constant
    SYSTEM_PROMPT = (
        "You are a sophisticated Financial Research Agent. Your sole goal is to gather all "
        "necessary financial data (stock price AND news) before providing a final, confident trading recommendation. "
        "Use your tools iteratively to collect all required data points. "
        "**Only when you have used ALL relevant tools and received their results** "
        "should you stop calling tools and generate a comprehensive final answer."
    )
    
    def __init__(self, model_name: str = "gpt-4o", max_iterations: int = 5):
        # 3a. Initialize LLM only ONCE
        self.llm = ChatOpenAI(model=model_name, temperature=0)
        
        # 3b. Bind the System Prompt and Tools to the LLM
        self.llm_with_tools = self.llm.with_messages(
            [SystemMessage(content=self.SYSTEM_PROMPT)]
        ).bind_tools(ALL_TOOLS)
        
        self.max_iterations = max_iterations
        self.app = self._build_graph()

    # --- 4. Node Definitions (Methods) ---

    def _call_agent(self, state: AgentState) -> AgentState:
        """
        Invokes the LLM to determine the next action (tool call or final answer).
        Uses the pre-initialized LLM bound with tools and prompt.
        """
        state['iterations'] += 1
        
        # Check for max iterations as a safeguard
        if state['iterations'] > self.max_iterations:
            print("\n🛑 MAX ITERATIONS REACHED. FORCING CONCLUSION.")
            final_msg = AIMessage(content="Maximum data collection attempts reached. Proceeding with a provisional trade recommendation based on the limited data collected.")
            return {"messages": [final_msg]}
        
        print(f"\n🧠 Calling Agent (Iteration {state['iterations']}/{self.max_iterations})")
        
        messages = state["messages"]
        response = self.llm_with_tools.invoke(messages)
        
        return {"messages": [response]}

    # --- 5. Conditional Edge Logic (Method) ---

    def _should_continue(self, state: AgentState) -> str:
        """
        Conditional logic to determine the next step in the graph.
        Returns: 'tools' to continue the loop, or END to stop.
        """
        messages = state["messages"]
        last_message = messages[-1]

        # Stop condition 1: Max iterations reached (handled in _call_agent)
        if state['iterations'] >= self.max_iterations:
            return END

        # Stop condition 2: The LLM returned a final answer (no tool calls)
        if not last_message.tool_calls:
            print("✅ Agent decided on a final answer (no more tools needed). Ending graph.")
            return END

        # Loop condition: The LLM requested tool calls
        print("🛠️ Agent requested tool calls. Routing to tools node.")
        return "tools"

    # --- 6. Graph Builder ---

    def _build_graph(self):
        """Builds and compiles the LangGraph workflow."""
        workflow = StateGraph(AgentState)

        # ToolNode needs the list of tools
        tool_node = ToolNode(ALL_TOOLS)
        
        # Add nodes (The agent node uses the class method _call_agent)
        workflow.add_node("agent", self._call_agent)
        workflow.add_node("tools", tool_node)

        # Set the starting point
        workflow.set_entry_point("agent")

        # Create the conditional edge/self-loop
        workflow.add_conditional_edges(
            "agent",
            self._should_continue,
            {"tools": "tools", END: END}
        )

        # After 'tools' execute, always loop back to the 'agent'
        workflow.add_edge("tools", "agent")

        return workflow.compile()

    # --- 7. Execution Method ---

    def run(self, user_query: str) -> dict:
        """
        Runs the compiled graph with a given user query.
        """
        initial_input = {
            "messages": [HumanMessage(content=user_query)],
            "iterations": 0,
            "max_iterations": self.max_iterations,
        }
        
        print("--- 🚀 STARTING TRADING AGENT EXECUTION ---")
        final_state = self.app.invoke(initial_input)
        
        return final_state

# --- 8. Example Usage ---

if __name__ == "__main__":
    
    # Instantiate the agent. The LLM is created here once.
    trading_agent = TradingAgentGraph(max_iterations=5)

    query = "What is a good trading action for AAPL right now? Find both its current price and recent news before deciding."
    
    # Run the agent
    final_state = trading_agent.run(query)

    # Display the final recommendation
    print("\n--- 💰 FINAL RECOMMENDATION ---")
    final_message: BaseMessage = final_state["messages"][-1]
    print(final_message.content)
    print(f"\nTotal Loop Iterations: {final_state['iterations']}")