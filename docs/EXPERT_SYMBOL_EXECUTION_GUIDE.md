# EXPERT Symbol Job Execution - Complete Implementation

## Overview
This document explains how EXPERT and DYNAMIC symbol jobs work with the `should_expand_instrument_jobs` property.

## Key Concept: Two-Phase Approach

### Phase 1: Job Creation (Scheduling)
Jobs are **always created** with special symbols when using expert/dynamic methods:
- ✅ EXPERT symbol jobs are created for expert-driven selection
- ✅ DYNAMIC symbol jobs are created for AI-driven selection
- ⚡ Created at schedule time, not execution time

### Phase 2: Job Execution (Runtime)
The `should_expand_instrument_jobs` property controls **execution behavior**:

## Execution Behavior Matrix

| Selection Method | should_expand | Job Creation | Execution Behavior |
|-----------------|---------------|--------------|-------------------|
| `expert` | `True` | ✅ Creates EXPERT job | Calls `get_recommended_instruments()`, creates individual jobs for each instrument, returns immediately |
| `expert` | `False` | ✅ Creates EXPERT job | Passes "EXPERT" symbol to expert's `run_analysis("EXPERT")` - expert handles internally |
| `dynamic` | `True` | ✅ Creates DYNAMIC job | Uses AI to select instruments, creates individual jobs for each |
| `dynamic` | `False` | ✅ Creates DYNAMIC job | Same as True - always expands (ignores property) |
| `static` | `True` | ✅ Creates jobs for each enabled instrument | Analyzes specific symbols directly |
| `static` | `False` | ❌ No jobs created | Expert handles its own scheduling |

## Implementation Details

### 1. Job Creation (`_get_enabled_instruments`)

```python
if instrument_selection_method == 'expert' and can_recommend_instruments:
    # Always create EXPERT job
    logger.info(f"Expert uses expert-driven selection - creating EXPERT job")
    return ["EXPERT"]
elif instrument_selection_method == 'dynamic':
    # Always create DYNAMIC job
    logger.info(f"Expert uses dynamic selection - creating DYNAMIC job")
    return ["DYNAMIC"]
```

**Key Points:**
- EXPERT/DYNAMIC jobs are **always created** during scheduling
- The `should_expand_instrument_jobs` property does **not** affect job creation
- Jobs are created with special symbols that will be handled at execution time

### 2. Job Execution (`_execute_expert_driven_analysis`)

```python
def _execute_expert_driven_analysis(self, expert_instance_id: int, subtype: str):
    should_expand = expert_properties.get('should_expand_instrument_jobs', True)
    
    if not should_expand:
        # Case A: Pass EXPERT symbol directly to expert
        logger.info("Executing analysis with EXPERT symbol")
        self.submit_market_analysis(
            expert_instance_id=expert_instance_id,
            symbol="EXPERT",  # Special symbol passed to expert
            subtype=subtype
        )
        return  # Done - expert handles EXPERT symbol internally
    
    # Case B: Expand into individual instrument jobs
    logger.info("Expanding into individual instrument jobs")
    recommended_instruments = expert.get_recommended_instruments()
    
    for instrument in recommended_instruments:
        self.submit_market_analysis(
            expert_instance_id=expert_instance_id,
            symbol=instrument,  # Real instrument symbol
            subtype=subtype
        )
```

**Key Points:**
- **Case A (`should_expand=False`)**: 
  - EXPERT symbol is passed directly to expert's `run_analysis("EXPERT", subtype)` method
  - Expert must handle the special "EXPERT" symbol internally
  - Expert can analyze multiple instruments in a single job
  - Useful for experts that need to analyze their portfolio holistically

- **Case B (`should_expand=True`)**:
  - Calls expert's `get_recommended_instruments()` method
  - Creates individual analysis jobs for each recommended instrument
  - Each instrument is analyzed separately
  - More granular, allows parallel processing

## Use Cases

### Use Case 1: Portfolio-Wide Analysis (`should_expand=False`)

**Example: Risk Management Expert**
```python
class RiskManagementExpert(MarketExpertInterface):
    @classmethod
    def get_expert_properties(cls):
        return {
            'can_recommend_instruments': True,
            'should_expand_instrument_jobs': False  # Handle EXPERT symbol directly
        }
    
    def run_analysis(self, symbol: str, subtype: str):
        if symbol == "EXPERT":
            # Analyze entire portfolio risk exposure
            all_positions = self.get_all_open_positions()
            correlations = self.calculate_portfolio_correlations(all_positions)
            risk_score = self.assess_overall_risk(correlations)
            # Make decisions based on portfolio-wide analysis
            return self.generate_portfolio_recommendations(risk_score)
        else:
            # Handle individual symbol analysis
            return self.analyze_individual_symbol(symbol)
```

**Benefits:**
- ✅ Single holistic view of entire portfolio
- ✅ Can analyze correlations between instruments
- ✅ More efficient for portfolio-wide decisions
- ✅ Avoids creating many individual jobs

### Use Case 2: Individual Instrument Analysis (`should_expand=True`)

**Example: Technical Analysis Expert**
```python
class TechnicalAnalysisExpert(MarketExpertInterface):
    @classmethod
    def get_expert_properties(cls):
        return {
            'can_recommend_instruments': True,
            'should_expand_instrument_jobs': True  # Expand into individual jobs
        }
    
    def get_recommended_instruments(self) -> List[str]:
        # Select instruments to analyze
        return ['AAPL', 'GOOGL', 'MSFT', 'NVDA', 'TSLA']
    
    def run_analysis(self, symbol: str, subtype: str):
        # Analyze each instrument independently
        chart_patterns = self.detect_patterns(symbol)
        indicators = self.calculate_indicators(symbol)
        return self.generate_recommendation(chart_patterns, indicators)
```

**Benefits:**
- ✅ Each instrument analyzed independently
- ✅ Parallel processing possible
- ✅ Clear separation of concerns
- ✅ Easier to track individual instrument performance

## Expert Implementation Requirements

### For `should_expand=False` Experts
Must handle the "EXPERT" symbol in `run_analysis()`:

```python
def run_analysis(self, symbol: str, subtype: str) -> dict:
    if symbol == "EXPERT":
        # Handle expert-driven analysis
        instruments = self.get_recommended_instruments()
        # Analyze multiple instruments together
        return self.portfolio_analysis(instruments)
    else:
        # Handle regular symbol analysis
        return self.individual_analysis(symbol)
```

### For `should_expand=True` Experts
Only need to implement `get_recommended_instruments()`:

```python
def get_recommended_instruments(self) -> List[str]:
    # Return list of instruments to analyze
    return ['AAPL', 'GOOGL', 'MSFT']

def run_analysis(self, symbol: str, subtype: str) -> dict:
    # Only handles real instrument symbols
    return self.analyze(symbol)
```

## Summary

✅ **EXPERT jobs are always created** during scheduling
✅ **Execution behavior controlled** by `should_expand_instrument_jobs`
✅ **Flexibility for different expert types**:
   - Portfolio-wide analysis (expand=False)
   - Individual instrument analysis (expand=True)
✅ **No changes needed** for experts that don't use expert-driven selection