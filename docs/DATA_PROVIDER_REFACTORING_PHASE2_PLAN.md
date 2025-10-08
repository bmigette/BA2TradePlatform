# Data Provider Refactoring - Phase 2: Implementation Plan

## Overview

Phase 2 focuses on implementing data providers with **native database persistence** while maintaining TradingAgents graph state compatibility.

## Key Design Decision: Hybrid Storage Approach

### The Problem
- **Current**: TradingAgents stores analysis data in graph state (ephemeral) and AnalysisOutput (database)
- **New**: Data providers fetch data independently of TradingAgents workflow
- **Challenge**: How to persist provider outputs and integrate with TradingAgents?

### The Solution: Dual Storage Pattern

```
┌─────────────────────────────────────────────────────────────┐
│                    Data Provider Call                        │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │  Provider fetches data       │
        │  (news, fundamentals, etc.)  │
        └──────────────┬───────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │  Format as dict OR markdown  │
        └──────────────┬───────────────┘
                       │
                       ├─────────────────────────────────────┐
                       ▼                                     ▼
        ┌──────────────────────────┐        ┌───────────────────────────┐
        │ Save to AnalysisOutput   │        │ Return to caller          │
        │ (database persistence)   │        │ (for graph state)         │
        │                          │        │                           │
        │ - market_analysis_id     │        │ - Use in LangGraph state  │
        │ - name: "alpaca_news"    │        │ - Pass to LLM agents      │
        │ - type: "news"           │        │ - Include in prompts      │
        │ - text: markdown/dict    │        │                           │
        └──────────────────────────┘        └───────────────────────────┘
                       │
                       └─────────────────────────────────────┘
                                     │
                                     ▼
                    ┌────────────────────────────────┐
                    │  Available for:                │
                    │  - UI display                  │
                    │  - Historical analysis         │
                    │  - Caching/reuse              │
                    │  - Audit trail                │
                    └────────────────────────────────┘
```

### Benefits

✅ **Persistent Storage**: All provider outputs saved to database automatically  
✅ **Graph State Compatibility**: Data still available for TradingAgents workflows  
✅ **Caching**: Avoid redundant API calls for same symbol/date  
✅ **Audit Trail**: Complete history of data fetched and used  
✅ **UI Access**: Direct database queries for displaying analysis data  
✅ **Reusability**: Same data can be used by multiple experts/analyses

## Enhanced AnalysisOutput Schema

### Current Schema
```python
class AnalysisOutput(SQLModel, table=True):
    id: int | None
    created_at: DateTime
    market_analysis_id: int  # FK to MarketAnalysis
    name: str                # e.g., "fundamentals_report"
    type: str                # e.g., "report"
    text: str | None
    blob: bytes | None
```

### Enhanced Schema (Proposed)
```python
class AnalysisOutput(SQLModel, table=True):
    id: int | None
    created_at: DateTime
    market_analysis_id: int | None  # FK to MarketAnalysis (nullable for standalone use)
    
    # Provider identification
    provider_category: str | None   # 'news', 'fundamentals_overview', 'indicators', etc.
    provider_name: str | None       # 'alpaca', 'yfinance', 'alphavantage', etc.
    
    # Data identification
    name: str                       # e.g., "AAPL_news_2025-10-08"
    type: str                       # e.g., "news", "fundamentals", "indicator"
    
    # Data content
    text: str | None                # Markdown or JSON string
    blob: bytes | None              # Binary data if needed
    
    # Metadata for caching and reuse
    symbol: str | None              # Stock symbol if applicable
    start_date: DateTime | None     # Date range start
    end_date: DateTime | None       # Date range end
    format_type: str | None         # 'dict' or 'markdown'
    
    # Additional metadata
    metadata: Dict[str, Any]        # Provider-specific metadata (JSON column)
```

## Provider Wrapper Pattern

### Base Provider Wrapper

```python
from ba2_trade_platform.core.interfaces import DataProviderInterface
from ba2_trade_platform.core.models import AnalysisOutput, MarketAnalysis
from ba2_trade_platform.core.db import get_db, add_instance
from sqlmodel import Session, select
from datetime import datetime
from typing import Dict, Any, Optional, Literal
import json

class ProviderWithPersistence:
    """
    Wrapper for data providers that automatically saves outputs to database.
    
    This class wraps any DataProviderInterface and automatically persists
    the output to AnalysisOutput table while still returning the data
    for use in graph state or other workflows.
    """
    
    def __init__(
        self, 
        provider: DataProviderInterface,
        category: str,
        market_analysis_id: Optional[int] = None
    ):
        """
        Initialize with a provider instance.
        
        Args:
            provider: The actual data provider instance
            category: Provider category ('news', 'indicators', etc.)
            market_analysis_id: Optional link to MarketAnalysis for workflow tracking
        """
        self.provider = provider
        self.category = category
        self.market_analysis_id = market_analysis_id
    
    def fetch_and_save(
        self,
        method_name: str,
        output_name: str,
        **kwargs
    ) -> Dict[str, Any] | str:
        """
        Call a provider method and automatically save the output.
        
        Args:
            method_name: Name of provider method to call (e.g., 'get_company_news')
            output_name: Name for the saved output (e.g., 'AAPL_news_7days')
            **kwargs: Arguments to pass to the provider method
        
        Returns:
            The provider's output (dict or markdown string)
        
        Example:
            >>> wrapper = ProviderWithPersistence(alpaca_news, "news", analysis_id=123)
            >>> news = wrapper.fetch_and_save(
            ...     "get_company_news",
            ...     "AAPL_news_recent",
            ...     symbol="AAPL",
            ...     end_date=datetime.now(),
            ...     lookback_days=7,
            ...     format_type="markdown"
            ... )
        """
        # Call the provider method
        method = getattr(self.provider, method_name)
        result = method(**kwargs)
        
        # Extract metadata from kwargs
        symbol = kwargs.get('symbol')
        start_date = kwargs.get('start_date')
        end_date = kwargs.get('end_date')
        format_type = kwargs.get('format_type', 'markdown')
        
        # Calculate start_date if using lookback
        if not start_date and 'lookback_days' in kwargs and end_date:
            from datetime import timedelta
            start_date = end_date - timedelta(days=kwargs['lookback_days'])
        elif not start_date and 'lookback_periods' in kwargs:
            # For financial statements, store the period count in metadata
            pass
        
        # Prepare text content
        if isinstance(result, dict):
            text_content = json.dumps(result, indent=2, default=str)
        else:
            text_content = result
        
        # Create AnalysisOutput record
        analysis_output = AnalysisOutput(
            market_analysis_id=self.market_analysis_id,
            provider_category=self.category,
            provider_name=self.provider.get_provider_name(),
            name=output_name,
            type=self.category,
            text=text_content,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            format_type=format_type,
            metadata={
                'method': method_name,
                'kwargs': {k: str(v) for k, v in kwargs.items()},
                'provider_features': self.provider.get_supported_features()
            }
        )
        
        # Save to database
        add_instance(analysis_output)
        
        return result
    
    def check_cache(
        self,
        output_name: str,
        max_age_hours: int = 24
    ) -> Optional[Dict[str, Any] | str]:
        """
        Check if cached output exists and is still fresh.
        
        Args:
            output_name: Name of the output to check
            max_age_hours: Maximum age in hours for cache validity
        
        Returns:
            Cached output if found and fresh, None otherwise
        """
        from datetime import timedelta
        
        engine = get_db()
        with Session(engine.bind) as session:
            statement = select(AnalysisOutput).where(
                AnalysisOutput.name == output_name,
                AnalysisOutput.provider_category == self.category,
                AnalysisOutput.provider_name == self.provider.get_provider_name()
            ).order_by(AnalysisOutput.created_at.desc())
            
            output = session.exec(statement).first()
            
            if output:
                age = datetime.now(timezone.utc) - output.created_at
                if age < timedelta(hours=max_age_hours):
                    # Return cached data in original format
                    if output.format_type == 'dict':
                        return json.loads(output.text)
                    else:
                        return output.text
        
        return None
```

## Integration with TradingAgents

### Modified Graph State Usage

```python
# In TradingAgents expert integration

from ba2_trade_platform.modules.dataproviders import get_provider
from ba2_trade_platform.core.interfaces import ProviderWithPersistence

class TradingAgentsExpert(MarketExpertInterface):
    
    def get_prediction_for_instrument(
        self, 
        symbol: str, 
        market_analysis_id: int,
        **kwargs
    ):
        # Get market analysis from DB
        market_analysis = get_instance(MarketAnalysis, market_analysis_id)
        
        # Initialize providers with persistence
        news_provider = get_provider("news", "alpaca")
        news_wrapper = ProviderWithPersistence(
            news_provider, 
            "news",
            market_analysis_id=market_analysis_id
        )
        
        # Check cache first
        news_cache_key = f"{symbol}_news_{datetime.now().date()}"
        news = news_wrapper.check_cache(news_cache_key, max_age_hours=6)
        
        if not news:
            # Fetch and auto-save
            news = news_wrapper.fetch_and_save(
                "get_company_news",
                news_cache_key,
                symbol=symbol,
                end_date=datetime.now(),
                lookback_days=7,
                format_type="markdown"
            )
        
        # Update graph state with the data
        state = market_analysis.state or {}
        state['news_report'] = news
        
        # Save state back to DB
        market_analysis.state = state
        update_instance(market_analysis)
        
        # Continue with TradingAgents workflow...
        graph = self._create_trading_graph()
        result = graph.invoke(state)
        
        # Result already has news in state, and it's also saved in DB
        return result
```

## Implementation Roadmap

### Phase 2A: Foundation (Week 1)
1. ✅ Create enhanced AnalysisOutput schema migration
2. ✅ Implement ProviderWithPersistence wrapper
3. ✅ Add caching mechanism
4. ✅ Create provider helper utilities

### Phase 2B: First Provider Implementation (Week 1-2)
1. **AlpacaNewsProvider** - Company and market news
   - Use Alpaca Markets News API
   - Implement both dict and markdown formats
   - Add sentiment parsing if available
   
2. **Integration with TradingAgents**
   - Modify news fetching to use AlpacaNewsProvider
   - Update graph state initialization
   - Test with existing workflows

### Phase 2C: Additional Providers (Week 2-3)
3. **AlphaVantageIndicatorsProvider** - Technical indicators
4. **YFinanceIndicatorsProvider** - Calculated indicators
5. **FREDMacroProvider** - Economic data

### Phase 2D: Migration & UI (Week 3-4)
6. Migrate remaining TradingAgents data fetching to providers
7. Add provider selection to expert settings UI
8. Create provider management UI page

## Migration Strategy

### Backward Compatibility

**Keep existing TradingAgents interface.py** as a compatibility layer:

```python
# tradingagents/dataflows/interface.py (legacy)

from ba2_trade_platform.modules.dataproviders import get_provider
from ba2_trade_platform.core.interfaces import ProviderWithPersistence

def get_news(symbol: str, end_date: str, lookback_days: int = 7, vendors=None):
    """Legacy function - now uses provider system under the hood."""
    
    # Determine which provider to use
    provider_name = vendors[0] if vendors else "alpaca"
    
    # Get provider
    news_provider = get_provider("news", provider_name)
    
    # Fetch data (no persistence for legacy calls)
    return news_provider.get_company_news(
        symbol=symbol,
        end_date=datetime.fromisoformat(end_date),
        lookback_days=lookback_days,
        format_type="markdown"
    )
```

## Database Migration

```python
# alembic/versions/xxx_enhance_analysis_output.py

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

def upgrade() -> None:
    # Add new columns
    op.add_column('analysisoutput', sa.Column('provider_category', sa.String(), nullable=True))
    op.add_column('analysisoutput', sa.Column('provider_name', sa.String(), nullable=True))
    op.add_column('analysisoutput', sa.Column('symbol', sa.String(), nullable=True))
    op.add_column('analysisoutput', sa.Column('start_date', sa.DateTime(), nullable=True))
    op.add_column('analysisoutput', sa.Column('end_date', sa.DateTime(), nullable=True))
    op.add_column('analysisoutput', sa.Column('format_type', sa.String(), nullable=True))
    op.add_column('analysisoutput', sa.Column('metadata', sa.JSON(), nullable=True))
    
    # Make market_analysis_id nullable for standalone use
    op.alter_column('analysisoutput', 'market_analysis_id', nullable=True)

def downgrade() -> None:
    # Remove new columns
    op.drop_column('analysisoutput', 'metadata')
    op.drop_column('analysisoutput', 'format_type')
    op.drop_column('analysisoutput', 'end_date')
    op.drop_column('analysisoutput', 'start_date')
    op.drop_column('analysisoutput', 'symbol')
    op.drop_column('analysisoutput', 'provider_name')
    op.drop_column('analysisoutput', 'provider_category')
    
    # Restore market_analysis_id non-nullable
    op.alter_column('analysisoutput', 'market_analysis_id', nullable=False)
```

## Testing Strategy

### Unit Tests
```python
def test_provider_with_persistence():
    """Test that provider output is saved to database."""
    provider = get_provider("news", "alpaca")
    wrapper = ProviderWithPersistence(provider, "news")
    
    result = wrapper.fetch_and_save(
        "get_company_news",
        "TEST_AAPL_news",
        symbol="AAPL",
        end_date=datetime.now(),
        lookback_days=7,
        format_type="dict"
    )
    
    # Verify result returned
    assert result is not None
    
    # Verify saved to database
    engine = get_db()
    with Session(engine.bind) as session:
        output = session.exec(
            select(AnalysisOutput).where(AnalysisOutput.name == "TEST_AAPL_news")
        ).first()
        
        assert output is not None
        assert output.provider_category == "news"
        assert output.provider_name == "alpaca"
        assert output.symbol == "AAPL"
```

### Integration Tests
```python
def test_tradingagents_with_providers():
    """Test TradingAgents workflow with new provider system."""
    expert = TradingAgentsExpert(instance_id=1)
    
    result = expert.get_prediction_for_instrument(
        symbol="AAPL",
        market_analysis_id=123
    )
    
    # Verify recommendation created
    assert result is not None
    
    # Verify provider outputs saved
    outputs = get_market_analysis_outputs(123)
    assert any(o.provider_category == "news" for o in outputs)
```

## Next Steps

Ready to implement? Let's start with:
1. ✅ Enhance AnalysisOutput model
2. ✅ Create ProviderWithPersistence wrapper
3. ✅ Implement AlpacaNewsProvider
4. ✅ Test integration with TradingAgents

Which would you like to tackle first?
