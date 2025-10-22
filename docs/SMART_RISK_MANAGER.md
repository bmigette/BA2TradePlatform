# Smart Risk Manager - Technical Specification

## Overview
The Smart Risk Manager is an AI-powered agentic system that uses LangGraph to analyze portfolio status and execute risk management actions autonomously. It operates when `risk_manager_mode` is set to "smart" and follows user-defined instructions from the `smart_risk_manager_user_instructions` setting.

---

## Architecture Components

### 1. SmartRiskExpertInterface (core/interfaces/SmartRiskExpertInterface.py)
Abstract interface that experts must implement to provide analysis data to the Smart Risk Manager.

**Methods:**
```python
@abstractmethod
def get_analysis_summary(self, market_analysis_id: int) -> str:
    """
    Get a concise summary of a market analysis.
    
    Args:
        market_analysis_id: ID of the MarketAnalysis record
        
    Returns:
        str: Human-readable summary (2-3 sentences) covering:
            - Symbol analyzed
            - Overall recommendation (buy/sell/hold)
            - Confidence level
            - Key insights
    """
    pass

@abstractmethod
def get_available_outputs(self, market_analysis_id: int) -> Dict[str, str]:
    """
    List all available analysis outputs with descriptions.
    
    Args:
        market_analysis_id: ID of the MarketAnalysis record
        
    Returns:
        Dict[str, str]: Map of output_key -> description
        Example for TradingAgents:
        {
            "analyst_fundamentals_output": "Fundamental analysis including P/E, revenue, earnings",
            "analyst_technical_output": "Technical indicators and chart patterns",
            "analyst_sentiment_output": "Market sentiment and news analysis",
            "analyst_risk_output": "Risk assessment and volatility analysis",
            "final_recommendation": "Synthesized recommendation from all analysts"
        }
    """
    pass

@abstractmethod
def get_output_detail(self, market_analysis_id: int, output_key: str) -> str:
    """
    Get the full content of a specific analysis output.
    
    Args:
        market_analysis_id: ID of the MarketAnalysis record
        output_key: Key of the output to retrieve (from get_available_outputs)
        
    Returns:
        str: Complete output content (can be multi-paragraph, includes all details)
        
    Raises:
        KeyError: If output_key is not valid for this analysis
    """
    pass
```

**Implementation Note:**
- TradingAgents will implement this interface first
- Future experts can implement their own output structure
- All outputs are returned as formatted strings for LLM consumption

---

### 2. SmartRiskManagerToolkit (core/SmartRiskManagerToolkit.py)
Provides LangChain-compatible tools for the agent graph.

**Tools:**

#### Portfolio & Account Tools
```python
def get_portfolio_status() -> Dict[str, Any]:
    """
    Get current portfolio status including all open positions.
    
    Returns:
        {
            "account_virtual_equity": float,
            "account_available_balance": float,
            "account_balance_pct_available": float,  # Percentage of equity available
            "open_positions": [
                {
                    "transaction_id": int,
                    "symbol": str,
                    "direction": str,  # "BUY" or "SELL"
                    "quantity": float,
                    "entry_price": float,
                    "current_price": float,
                    "unrealized_pnl": float,
                    "unrealized_pnl_pct": float,
                    "position_value": float,  # Current value of position
                    "tp_order": {  # Take profit order (if exists)
                        "order_id": int,
                        "price": float,
                        "quantity": float,
                        "status": str
                    } or None,
                    "sl_order": {  # Stop loss order (if exists)
                        "order_id": int,
                        "price": float,
                        "quantity": float,
                        "status": str
                    } or None
                }
            ],
            "total_unrealized_pnl": float,
            "total_position_value": float,
            "risk_metrics": {
                "max_drawdown_pct": float,  # Portfolio level
                "largest_position_pct": float,  # % of equity in largest position
                "num_positions": int
            }
        }
    
    Implementation:
        - Query Transaction table for OPEN transactions
        - Get related TradingOrder records for TP/SL
        - Calculate P&L using account.get_instrument_current_price()
        - Get account equity from account.get_account_info()
    """

def get_recent_analyses(symbol: Optional[str] = None, max_age_hours: int = 24) -> List[Dict[str, Any]]:
    """
    Get recent market analyses for open positions.
    
    Args:
        symbol: Specific symbol to query (None = all symbols with open positions)
        max_age_hours: Maximum age of analyses to return (default 24 hours)
        
    Returns:
        [
            {
                "analysis_id": int,
                "symbol": str,
                "timestamp": str,  # ISO format
                "age_hours": float,
                "expert_name": str,
                "expert_instance_id": int,
                "status": str,  # "COMPLETED", "FAILED", etc.
                "summary": str  # From SmartRiskExpertInterface.get_analysis_summary()
            }
        ]
        Sorted by timestamp DESC (most recent first)
    
    Implementation:
        - Query MarketAnalysis table joined with ExpertInstance
        - Filter by timestamp (NOW - max_age_hours)
        - If symbol provided, filter by symbol
        - If symbol=None, get all symbols from open positions
        - Call expert.get_analysis_summary() for each analysis
    """

def get_analysis_outputs(analysis_id: int) -> Dict[str, str]:
    """
    Get available outputs for a specific analysis.
    
    Args:
        analysis_id: MarketAnalysis ID
        
    Returns:
        Dict[output_key, description] from SmartRiskExpertInterface.get_available_outputs()
    
    Implementation:
        - Load MarketAnalysis record
        - Get expert instance
        - Call expert.get_available_outputs(analysis_id)
    """

def get_analysis_output_detail(analysis_id: int, output_key: str) -> str:
    """
    Get full detail of a specific analysis output.
    
    Args:
        analysis_id: MarketAnalysis ID
        output_key: Output identifier (from get_analysis_outputs)
        
    Returns:
        str: Complete output content
        
    Implementation:
        - Load MarketAnalysis record
        - Get expert instance
        - Call expert.get_output_detail(analysis_id, output_key)
    """

def get_historical_analyses(symbol: str, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
    """
    Get historical market analyses for deeper research.
    
    Args:
        symbol: Symbol to query
        limit: Max number of results (default 10)
        offset: Skip first N results (for pagination)
        
    Returns:
        Same format as get_recent_analyses()
        Ordered by timestamp DESC
        
    Implementation:
        - Query MarketAnalysis table for symbol
        - No time filter (gets all history)
        - Apply limit and offset for pagination
        - Return analysis summaries
    """
```

#### Trading Action Tools
```python
def close_position(transaction_id: int, reason: str) -> Dict[str, Any]:
    """
    Close an open position completely.
    
    Args:
        transaction_id: ID of Transaction to close
        reason: Explanation for closure (logged)
        
    Returns:
        {
            "success": bool,
            "message": str,
            "order_id": int or None,
            "transaction_id": int
        }
        
    Implementation:
        - Load Transaction record
        - Get account instance
        - Call account.close_transaction(transaction_id)
        - Log action with reason to database
        - Return result
    """

def adjust_quantity(transaction_id: int, new_quantity: float, reason: str) -> Dict[str, Any]:
    """
    Adjust position size (partial close or add).
    
    Args:
        transaction_id: ID of Transaction to adjust
        new_quantity: New total quantity (can be < or > current)
        reason: Explanation for adjustment
        
    Returns:
        {
            "success": bool,
            "message": str,
            "order_id": int or None,
            "old_quantity": float,
            "new_quantity": float
        }
        
    Implementation:
        - Load Transaction record
        - Calculate quantity delta
        - If new_quantity < current: partial close
        - If new_quantity > current: add to position
        - Call appropriate account methods
        - Update transaction record
        - Log action
    """

def update_stop_loss(transaction_id: int, new_sl_price: float, reason: str) -> Dict[str, Any]:
    """
    Update stop loss order for a position.
    
    Args:
        transaction_id: ID of Transaction
        new_sl_price: New stop loss price
        reason: Explanation for change
        
    Returns:
        {
            "success": bool,
            "message": str,
            "order_id": int or None,
            "old_sl_price": float or None,
            "new_sl_price": float
        }
        
    Implementation:
        - Load Transaction and related TradingOrder (type=STOP_LOSS)
        - Get account instance
        - Call account.update_sl_order() or similar
        - Update TradingOrder record
        - Log action
    """

def update_take_profit(transaction_id: int, new_tp_price: float, reason: str) -> Dict[str, Any]:
    """
    Update take profit order for a position.
    
    Args:
        transaction_id: ID of Transaction
        new_tp_price: New take profit price
        reason: Explanation for change
        
    Returns:
        {
            "success": bool,
            "message": str,
            "order_id": int or None,
            "old_tp_price": float or None,
            "new_tp_price": float
        }
        
    Implementation:
        - Load Transaction and related TradingOrder (type=TAKE_PROFIT)
        - Get account instance
        - Call account.update_tp_order() or similar
        - Update TradingOrder record
        - Log action
    """

def open_new_position(
    symbol: str,
    direction: str,  # "BUY" or "SELL"
    quantity: float,
    tp_price: Optional[float],
    sl_price: Optional[float],
    reason: str
) -> Dict[str, Any]:
    """
    Open a new trading position.
    
    Args:
        symbol: Instrument symbol
        direction: "BUY" or "SELL"
        quantity: Position size
        tp_price: Take profit price (optional)
        sl_price: Stop loss price (optional)
        reason: Explanation for opening position
        
    Returns:
        {
            "success": bool,
            "message": str,
            "transaction_id": int or None,
            "order_id": int or None
        }
        
    Implementation:
        - Validate symbol exists and is enabled
        - Check account balance and risk limits
        - Get account instance
        - Submit market order via account interface
        - Create Transaction record
        - If TP/SL provided, create corresponding orders
        - Log action
        
    Note: This should respect expert's enable_buy/enable_sell settings
    """
```

#### Utility Tools
```python
def get_current_price(symbol: str) -> float:
    """
    Get current market price for a symbol.
    
    Args:
        symbol: Instrument symbol
        
    Returns:
        float: Current price
        
    Implementation:
        - Get account instance
        - Call account.get_instrument_current_price(symbol)
    """

def calculate_position_metrics(
    entry_price: float,
    current_price: float,
    quantity: float,
    direction: str
) -> Dict[str, float]:
    """
    Calculate position metrics without modifying anything.
    
    Args:
        entry_price: Entry price
        current_price: Current market price
        quantity: Position size
        direction: "BUY" or "SELL"
        
    Returns:
        {
            "unrealized_pnl": float,
            "unrealized_pnl_pct": float,
            "position_value": float
        }
        
    Implementation:
        - Use standard P&L calculations
        - BUY: (current_price - entry_price) * quantity
        - SELL: (entry_price - current_price) * quantity
    """
```

---

### 3. SmartRiskManagerGraph (core/SmartRiskManagerGraph.py)
LangGraph-based agentic workflow implementation.

**Graph Structure:**

```
[START]
   ↓
[initialize_context]  ← Load portfolio status, user instructions
   ↓
[analyze_portfolio]   ← Review positions, P&L, risk metrics
   ↓
[check_recent_analyses] ← Get recent market analyses for positions
   ↓
[agent_decision_loop]  ← LLM decides: research_more, take_action, or finish
   ↓
   ├→ [research_node]  ← Get historical analyses, detailed outputs
   │    ↓
   │    └→ [agent_decision_loop] (loop back)
   │
   ├→ [action_node]    ← Execute trading actions (close, adjust, etc.)
   │    ↓
   │    └→ [agent_decision_loop] (loop back)
   │
   └→ [finalize]       ← Summarize actions taken, exit
        ↓
      [END]
```

**State Schema:**
```python
class SmartRiskManagerState(TypedDict):
    # Context
    expert_instance_id: int
    account_id: int
    user_instructions: str  # From smart_risk_manager_user_instructions setting
    risk_manager_model: str  # From risk_manager_model setting
    
    # Portfolio Data
    portfolio_status: Dict[str, Any]  # From get_portfolio_status()
    open_positions: List[Dict[str, Any]]
    
    # Analysis Data
    recent_analyses: List[Dict[str, Any]]
    detailed_outputs_cache: Dict[int, Dict[str, str]]  # analysis_id -> {output_key: content}
    
    # Agent State
    messages: List[BaseMessage]  # LangChain message history
    agent_scratchpad: str  # Agent's reasoning notes
    next_action: str  # "research_more", "take_action", "finish"
    
    # Actions Taken
    actions_log: List[Dict[str, Any]]  # Record of all actions executed
    
    # Loop Control
    iteration_count: int
    max_iterations: int  # Default 10, prevent infinite loops
```

**Node Implementations:**

```python
def initialize_context(state: SmartRiskManagerState) -> SmartRiskManagerState:
    """
    Initialize the context with portfolio status and settings.
    
    Steps:
    1. Get expert instance and account
    2. Load user_instructions and risk_manager_model from settings
    3. Call get_portfolio_status()
    4. Initialize empty caches and logs
    5. Set iteration_count = 0, max_iterations = 10
    6. Create initial system message with user instructions
    
    Returns:
        Updated state with context loaded
    """

def analyze_portfolio(state: SmartRiskManagerState) -> SmartRiskManagerState:
    """
    Analyze current portfolio and generate initial assessment.
    
    Steps:
    1. Calculate portfolio-level metrics
    2. Identify positions with significant P&L
    3. Check risk concentrations
    4. Add analysis to agent_scratchpad
    5. Generate prompt for LLM to assess portfolio health
    
    Returns:
        Updated state with portfolio analysis
    """

def check_recent_analyses(state: SmartRiskManagerState) -> SmartRiskManagerState:
    """
    Load recent market analyses for all open positions.
    
    Steps:
    1. Get symbols from open_positions
    2. Call get_recent_analyses() for each symbol
    3. Store in recent_analyses
    4. Add summaries to agent_scratchpad
    
    Returns:
        Updated state with recent analyses loaded
    """

def agent_decision_loop(state: SmartRiskManagerState) -> SmartRiskManagerState:
    """
    Main agent reasoning loop - decides next action.
    
    Steps:
    1. Build prompt with:
       - User instructions
       - Portfolio status
       - Recent analyses summaries
       - Available tools
       - Previous actions taken
    2. Call LLM with tools available
    3. LLM decides to:
       - research_more: Need more analysis details
       - take_action: Execute trading action
       - finish: Done with risk management
    4. Update next_action in state
    5. Increment iteration_count
    
    Returns:
        Updated state with next_action set
        
    Note: Uses structured output or tool calling to ensure valid next_action
    """

def research_node(state: SmartRiskManagerState) -> SmartRiskManagerState:
    """
    Research mode - get detailed analysis outputs.
    
    Steps:
    1. LLM selects which analyses to investigate
    2. Call get_analysis_outputs() for selected analyses
    3. LLM selects which outputs to read in detail
    4. Call get_analysis_output_detail() for selected outputs
    5. Store in detailed_outputs_cache
    6. Add findings to agent_scratchpad
    7. Set next_action = None (force return to decision loop)
    
    Returns:
        Updated state with research completed
    """

def action_node(state: SmartRiskManagerState) -> SmartRiskManagerState:
    """
    Action mode - execute trading operations.
    
    Steps:
    1. LLM decides which action(s) to take
    2. LLM provides reasoning for each action
    3. Execute actions via toolkit tools:
       - close_position()
       - adjust_quantity()
       - update_stop_loss()
       - update_take_profit()
       - open_new_position()
    4. Record results in actions_log
    5. Update portfolio_status with new data
    6. Set next_action = None (force return to decision loop)
    
    Returns:
        Updated state with actions executed
        
    Note: Each action includes "reason" parameter for audit trail
    """

def finalize(state: SmartRiskManagerState) -> SmartRiskManagerState:
    """
    Finalize and summarize risk management session.
    
    Steps:
    1. Generate summary of all actions taken
    2. Calculate final portfolio metrics
    3. Create final report with:
       - Initial vs final portfolio status
       - Actions executed with reasons
       - Key decisions made
    4. Log session summary to database
    
    Returns:
        Final state with summary
    """
```

**Conditional Edges:**
```python
def should_continue(state: SmartRiskManagerState) -> str:
    """
    Determine which node to execute next based on agent's decision.
    
    Logic:
    - If iteration_count >= max_iterations: return "finalize"
    - If next_action == "research_more": return "research_node"
    - If next_action == "take_action": return "action_node"
    - If next_action == "finish": return "finalize"
    - Else: return "agent_decision_loop" (shouldn't happen)
    
    Returns:
        Next node name
    """
```

**Graph Construction:**
```python
def build_smart_risk_manager_graph(expert_instance_id: int, account_id: int) -> StateGraph:
    """
    Build the complete LangGraph workflow.
    
    Returns:
        Compiled StateGraph ready for execution
    """
    workflow = StateGraph(SmartRiskManagerState)
    
    # Add nodes
    workflow.add_node("initialize_context", initialize_context)
    workflow.add_node("analyze_portfolio", analyze_portfolio)
    workflow.add_node("check_recent_analyses", check_recent_analyses)
    workflow.add_node("agent_decision_loop", agent_decision_loop)
    workflow.add_node("research_node", research_node)
    workflow.add_node("action_node", action_node)
    workflow.add_node("finalize", finalize)
    
    # Add edges
    workflow.set_entry_point("initialize_context")
    workflow.add_edge("initialize_context", "analyze_portfolio")
    workflow.add_edge("analyze_portfolio", "check_recent_analyses")
    workflow.add_edge("check_recent_analyses", "agent_decision_loop")
    
    # Conditional routing from agent_decision_loop
    workflow.add_conditional_edges(
        "agent_decision_loop",
        should_continue,
        {
            "research_node": "research_node",
            "action_node": "action_node",
            "finalize": "finalize",
            "agent_decision_loop": "agent_decision_loop"
        }
    )
    
    # Loop back to decision loop
    workflow.add_edge("research_node", "agent_decision_loop")
    workflow.add_edge("action_node", "agent_decision_loop")
    
    # End
    workflow.add_edge("finalize", END)
    
    return workflow.compile()
```

---

## Integration Points

### 1. Market Analysis Page - Manual Trigger
Add "Run Smart Risk Manager" button on Market Analysis page:
- Only visible when expert has `risk_manager_mode = "smart"`
- Triggers async execution of SmartRiskManagerGraph
- Shows progress dialog with streaming updates
- Displays final summary when complete

### 2. Scheduled Execution
Integrate with OPEN_POSITIONS scheduled jobs:
- When job runs, check `risk_manager_mode`
- If "smart": run SmartRiskManagerGraph instead of rule-based automation
- If "classic": continue with existing ruleset-based flow
- Log execution results to MarketAnalysis or separate SmartRiskSession table

### 3. Manual "Run Now" Button
In settings page, for experts with smart mode:
- Add button to manually trigger Smart Risk Manager
- Execute graph asynchronously
- Display results in dialog

---

## Data Models

### SmartRiskManagerJob (new model)
**Primary model for tracking Smart Risk Manager executions with links to analyzed market data.**

```python
class SmartRiskManagerJob(SQLModel, table=True):
    """
    Tracks Smart Risk Manager execution sessions.
    Links to MarketAnalysis records that were analyzed during the session.
    """
    __tablename__ = "smartriskmanagerjob"
    
    # Primary Key
    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Relations
    expert_instance_id: int = Field(foreign_key="expertinstance.id", index=True)
    account_id: int = Field(foreign_key="accountdefinition.id", index=True)
    
    # Execution Context
    run_date: datetime = Field(default_factory=datetime.utcnow, index=True)
    model_used: str  # Snapshot of risk_manager_model at execution time (e.g., "NagaAI/gpt-5-2025-08-07")
    user_instructions: str  # Snapshot of smart_risk_manager_user_instructions at execution time
    
    # State Preservation
    graph_state: str = Field(sa_column=Column(JSON))  # Complete LangGraph state as JSON
    # Contains: portfolio_status, open_positions, recent_analyses, messages, 
    #          agent_scratchpad, actions_log, iteration_count, etc.
    
    # Execution Metrics
    duration_seconds: float = Field(default=0.0)
    iteration_count: int = Field(default=0)
    
    # Portfolio Snapshot
    initial_portfolio_value: float  # Virtual equity at start
    final_portfolio_value: float  # Virtual equity at end
    
    # Results
    actions_taken_count: int = Field(default=0)  # Number of trading actions executed
    actions_summary: str  # Human-readable summary of actions taken
    
    # Status & Error Handling
    status: str = Field(default="RUNNING")  # "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED", "TIMEOUT"
    error_message: Optional[str] = Field(default=None)
    
    # Relationships
    # 1:N relationship with MarketAnalysis - tracks which analyses were consulted
    market_analyses: List["SmartRiskManagerJobAnalysis"] = Relationship(back_populates="smart_risk_job")


class SmartRiskManagerJobAnalysis(SQLModel, table=True):
    """
    Junction table linking SmartRiskManagerJob to MarketAnalysis records.
    Tracks which market analyses were consulted during the smart risk manager session.
    """
    __tablename__ = "smartriskmanagerjobanalysis"
    
    # Composite Primary Key
    id: Optional[int] = Field(default=None, primary_key=True)
    smart_risk_job_id: int = Field(foreign_key="smartriskmanagerjob.id", index=True)
    market_analysis_id: int = Field(foreign_key="marketanalysis.id", index=True)
    
    # Metadata
    consulted_at: datetime = Field(default_factory=datetime.utcnow)
    outputs_accessed: Optional[str] = Field(sa_column=Column(JSON), default=None)  
    # JSON list of output keys accessed (e.g., ["analyst_fundamentals_output", "final_recommendation"])
    
    # Relationships
    smart_risk_job: "SmartRiskManagerJob" = Relationship(back_populates="market_analyses")
    # market_analysis: "MarketAnalysis" = Relationship()  # Uncomment if bidirectional needed
```

**Usage Notes:**

1. **Creating a Job Record:**
   ```python
   job = SmartRiskManagerJob(
       expert_instance_id=expert.id,
       account_id=account.id,
       model_used=expert.settings["risk_manager_model"],
       user_instructions=expert.settings["smart_risk_manager_user_instructions"],
       graph_state={},  # Initialize empty, will be updated
       initial_portfolio_value=portfolio_status["account_virtual_equity"],
       final_portfolio_value=0.0,  # Updated at end
       actions_summary=""
   )
   job_id = add_instance(job)
   ```

2. **Updating State During Execution:**
   ```python
   # After each node execution
   job.graph_state = state.dict()  # Serialize complete state
   job.iteration_count = state["iteration_count"]
   update_instance(job)
   ```

3. **Linking Market Analyses:**
   ```python
   # When an analysis is consulted
   link = SmartRiskManagerJobAnalysis(
       smart_risk_job_id=job.id,
       market_analysis_id=analysis_id,
       outputs_accessed=["analyst_fundamentals_output", "final_recommendation"]
   )
   add_instance(link)
   ```

4. **Finalizing Job:**
   ```python
   job.status = "COMPLETED"
   job.final_portfolio_value = final_portfolio_status["account_virtual_equity"]
   job.actions_taken_count = len(state["actions_log"])
   job.duration_seconds = (datetime.utcnow() - job.run_date).total_seconds()
   update_instance(job)
   ```

5. **Querying Historical Jobs:**
   ```python
   # Get recent jobs for an expert
   jobs = session.exec(
       select(SmartRiskManagerJob)
       .where(SmartRiskManagerJob.expert_instance_id == expert_id)
       .order_by(SmartRiskManagerJob.run_date.desc())
       .limit(10)
   ).all()
   
   # Get all analyses consulted in a job
   analyses = session.exec(
       select(MarketAnalysis)
       .join(SmartRiskManagerJobAnalysis)
       .where(SmartRiskManagerJobAnalysis.smart_risk_job_id == job_id)
   ).all()
   ```

**Benefits:**
- Complete audit trail of all Smart Risk Manager executions
- Ability to replay/debug sessions using saved graph_state
- Track which market analyses influenced decisions
- Performance monitoring (duration, tokens, cost)
- Historical analysis of decision quality and outcomes

---

## Error Handling

### Graceful Degradation
1. **API Failures**: If LLM API fails, log error and skip this execution
2. **Tool Errors**: Catch exceptions in tools, return error in result dict
3. **Infinite Loops**: Enforce max_iterations limit (default 10)
4. **Invalid Actions**: Validate before execution, reject if constraints violated

### Logging
- Log all tool calls and results to app.debug.log
- Log final session summary to app.log
- Store session record in database for audit trail

### Safety Constraints
1. Check account balance before opening positions
2. Validate TP/SL prices are reasonable (not too close to current price)
3. Respect expert's enable_buy/enable_sell settings
4. Apply position size limits from max_virtual_equity_per_instrument_percent
5. Require explicit confirmation for large actions (>10% of portfolio)

---

## Testing Strategy

### Unit Tests
- Test each tool function independently
- Mock database and account calls
- Verify correct data transformations

### Integration Tests
- Test complete graph execution with mock data
- Verify state transitions
- Test error handling paths

### Manual Testing
- Test with paper trading account first
- Monitor first few executions closely
- Validate actions align with user instructions

---

## Performance Considerations

### Caching
- Cache market analysis summaries in state
- Cache detailed outputs to avoid redundant queries
- Reuse portfolio status within same session

### Timeouts
- Set reasonable timeout for each node (e.g., 30 seconds)
- Total session timeout: 5 minutes maximum
- Stream progress updates to UI for user feedback

### Cost Management
- Track token usage per session
- Implement budget limits (e.g., max $0.50 per session)
- Use smaller models for simple decisions
- Use larger models only for complex analysis

---

## Future Enhancements

1. **Multi-Expert Coordination**: Smart Risk Manager coordinates multiple experts
2. **Learning from History**: Use past session results to improve decisions
3. **Custom Constraints**: User-defined rules that Smart Risk Manager must follow
4. **Portfolio Rebalancing**: Optimize across multiple positions simultaneously
5. **Market Regime Detection**: Adjust strategy based on market conditions
6. **Risk Budgeting**: Allocate risk across positions dynamically

---

## Implementation Checklist

### Phase 1: Foundation (Week 1)
- [ ] Create SmartRiskExpertInterface
- [ ] Implement interface in TradingAgents expert
- [ ] Create SmartRiskManagerToolkit with all tools
- [ ] Unit test all toolkit functions

### Phase 2: Graph Implementation (Week 2)
- [ ] Implement SmartRiskManagerGraph with all nodes
- [ ] Add conditional routing logic
- [ ] Implement state management
- [ ] Add error handling and safety checks

### Phase 3: Integration (Week 3)
- [ ] Integrate with OPEN_POSITIONS scheduled jobs
- [ ] Add manual trigger button to Market Analysis page
- [ ] Add "Run Now" button in settings
- [ ] Implement progress streaming to UI

### Phase 4: Testing & Refinement (Week 4)
- [ ] Comprehensive testing with paper account
- [ ] Fine-tune prompts and decision logic
- [ ] Add logging and monitoring
- [ ] Create user documentation

### Phase 5: Production (Week 5)
- [ ] Deploy to production with feature flag
- [ ] Monitor first executions closely
- [ ] Gather user feedback
- [ ] Iterate based on real-world usage