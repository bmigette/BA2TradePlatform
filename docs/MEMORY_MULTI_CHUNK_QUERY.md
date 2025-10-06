# Memory Multi-Chunk Query Enhancement

## Overview
Enhanced `FinancialSituationMemory.get_memories()` to support two different strategies for handling long text queries that get split into multiple chunks.

## Problem
Previously, when a query text was too long and split into multiple chunks for embedding:
- All chunk embeddings were **averaged** using `np.mean()`
- This could lose important semantic information from individual chunks
- No way to leverage the fact that different chunks might match different stored memories

## Solution

Added an `aggregate_chunks` parameter to `get_memories()` that offers two strategies:

### Strategy 1: Averaged Embeddings (Default - `aggregate_chunks=True`)
**When to use**: Fast queries, general similarity matching

**How it works**:
1. Get embeddings for all chunks
2. Average them with `np.mean()`
3. Query ChromaDB once with averaged embedding
4. Return top N matches

**Pros**:
- ✅ Faster (single query)
- ✅ Simple, predictable behavior
- ✅ Good for general similarity

**Cons**:
- ❌ May lose specific semantic details from individual chunks
- ❌ All chunks weighted equally

**Code**:
```python
recommendations = matcher.get_memories(
    current_situation, 
    n_matches=2, 
    aggregate_chunks=True  # Default
)
```

### Strategy 2: Per-Chunk Querying (New - `aggregate_chunks=False`)
**When to use**: Long, complex queries where specific chunks matter

**How it works**:
1. Get embeddings for all chunks
2. Query ChromaDB **separately** for each chunk
3. Collect all results, deduplicate by document ID
4. Keep **best similarity score** if a document matches multiple chunks
5. Sort by similarity and return top N matches

**Pros**:
- ✅ Each chunk can find its best matches
- ✅ More accurate for long, multi-topic queries
- ✅ Deduplicates results intelligently
- ✅ Tracks which chunk produced the best match

**Cons**:
- ❌ Slower (multiple queries)
- ❌ More complex processing

**Code**:
```python
recommendations = matcher.get_memories(
    current_situation, 
    n_matches=2, 
    aggregate_chunks=False  # Use per-chunk strategy
)

# Results include 'chunk_match' field
for rec in recommendations:
    print(f"Best matched by chunk {rec['chunk_match']}")
```

## Implementation Details

### Function Signature
```python
def get_memories(self, current_situation, n_matches=1, aggregate_chunks=True):
    """Find matching recommendations using OpenAI embeddings
    
    Args:
        current_situation (str): The current financial situation to match
        n_matches (int): Number of matches to return per chunk (default: 1)
        aggregate_chunks (bool): If True, averages chunk embeddings before querying.
                                If False, queries with each chunk separately and merges results.
    
    Returns:
        list: List of matched results with similarity scores
    """
```

### Per-Chunk Strategy Algorithm

```python
if len(query_embeddings) > 1 and not aggregate_chunks:
    all_matches = {}  # Deduplicate by ID
    
    for chunk_idx, query_embedding in enumerate(query_embeddings):
        # Query with this chunk
        results = self.situation_collection.query(
            query_embeddings=[query_embedding],
            n_results=n_matches,
            include=["metadatas", "documents", "distances"],
        )
        
        # Collect matches, keeping best similarity per document
        for i in range(len(results["documents"][0])):
            doc_id = results["ids"][0][i]
            similarity = 1 - results["distances"][0][i]
            
            # Keep best score if document appears multiple times
            if doc_id not in all_matches or all_matches[doc_id]["similarity_score"] < similarity:
                all_matches[doc_id] = {
                    "matched_situation": results["documents"][0][i],
                    "recommendation": results["metadatas"][0][i]["recommendation"],
                    "similarity_score": similarity,
                    "chunk_match": chunk_idx  # Track which chunk matched best
                }
    
    # Sort by similarity and return top n_matches
    matched_results = sorted(all_matches.values(), 
                            key=lambda x: x["similarity_score"], 
                            reverse=True)[:n_matches]
```

## Use Cases

### Use Case 1: Short Query (< 24k characters)
**Situation**: Simple, focused query
**Strategy**: Either works (only 1 chunk, no difference)

```python
query = "High inflation with rising interest rates"
recs = matcher.get_memories(query, n_matches=3)
# Only 1 chunk, aggregate_chunks doesn't matter
```

### Use Case 2: Long Analysis Report (> 24k characters)
**Situation**: Comprehensive market analysis with multiple topics
**Strategy**: Use `aggregate_chunks=False` for best results

```python
query = """
Comprehensive 10-page market analysis covering:
- Macroeconomic conditions and inflation trends
- Sector-specific performance analysis
- Technical indicators across multiple timeframes
- Sentiment analysis from social media and news
- Geopolitical risk assessment
... [24k+ characters]
"""

# Better approach: query with each chunk separately
recs = matcher.get_memories(query, n_matches=5, aggregate_chunks=False)

for rec in recs:
    print(f"Match from chunk {rec['chunk_match']}: {rec['similarity_score']:.2f}")
```

### Use Case 3: Real-time Performance Critical
**Situation**: Need fast response time
**Strategy**: Use `aggregate_chunks=True` (default)

```python
# Fast path: average embeddings, single query
recs = matcher.get_memories(long_query, n_matches=2, aggregate_chunks=True)
```

## Performance Comparison

| Strategy | Chunks | Queries | Time Complexity | Best For |
|----------|--------|---------|----------------|----------|
| Averaged (`True`) | N | 1 | O(1) | Fast, general matching |
| Per-Chunk (`False`) | N | N | O(N) | Accurate, multi-topic queries |

**Example**:
- 3 chunks, averaged: 1 ChromaDB query
- 3 chunks, per-chunk: 3 ChromaDB queries (3x slower, but more accurate)

## Return Value Format

### With `aggregate_chunks=True` (Default)
```python
[
    {
        "matched_situation": "...",
        "recommendation": "...",
        "similarity_score": 0.85
    },
    ...
]
```

### With `aggregate_chunks=False`
```python
[
    {
        "matched_situation": "...",
        "recommendation": "...",
        "similarity_score": 0.85,
        "chunk_match": 2  # Best match came from chunk index 2
    },
    ...
]
```

## Example Usage

```python
from tradingagents.agents.utils.memory import FinancialSituationMemory

# Initialize memory
matcher = FinancialSituationMemory(
    name="market_analysis",
    config=config,
    symbol="AAPL",
    expert_instance_id=123
)

# Add some training data
matcher.add_situations([
    ("High inflation scenario...", "Defensive positioning recommended"),
    ("Tech sector volatility...", "Reduce exposure to growth stocks"),
])

# Query with long text
long_analysis = """
[Very long market analysis with multiple topics spanning 30k+ characters]
"""

# Option 1: Fast, averaged approach
fast_results = matcher.get_memories(
    long_analysis, 
    n_matches=3, 
    aggregate_chunks=True  # Default
)

# Option 2: Accurate, per-chunk approach
accurate_results = matcher.get_memories(
    long_analysis, 
    n_matches=3, 
    aggregate_chunks=False  # Query each chunk separately
)

# Check which chunks matched
for rec in accurate_results:
    print(f"Chunk {rec['chunk_match']} matched with score {rec['similarity_score']:.2f}")
```

## Backward Compatibility

✅ **Fully backward compatible**
- Default behavior unchanged (`aggregate_chunks=True`)
- Existing code continues to work without modifications
- New parameter is optional

## Testing

Run the example at the bottom of `memory.py`:

```bash
cd ba2_trade_platform/thirdparties/TradingAgents/tradingagents/agents/utils
python memory.py
```

Output shows both strategies side-by-side:
```
=== Option 1: Averaged Embeddings ===
Match 1:
Similarity Score: 0.85
...

=== Option 2: Per-Chunk Querying (merged results) ===
Match 1:
Similarity Score: 0.87
Best Chunk Match: 1
...
```

## Future Enhancements

Potential improvements:
1. **Weighted averaging**: Weight chunks by importance/length
2. **Chunk-level filtering**: Filter chunks before querying
3. **Parallel queries**: Use asyncio for concurrent chunk queries
4. **Smart deduplication**: Use Reciprocal Rank Fusion (RRF) instead of best-score
5. **Configurable strategies**: Add more query strategies (e.g., "union", "intersection")

## Related Files

- **Modified**: `tradingagents/agents/utils/memory.py`
  - Added `aggregate_chunks` parameter to `get_memories()`
  - Implemented per-chunk query strategy
  - Enhanced logging and documentation
