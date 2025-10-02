import chromadb
from chromadb.config import Settings
from openai import OpenAI
import numpy as np
from ... import logger as ta_logger


class FinancialSituationMemory:
    def __init__(self, name, config, symbol=None, market_analysis_id=None):
        if config["backend_url"] == "http://localhost:11434/v1":
            self.embedding = "nomic-embed-text"
        else:
            self.embedding = "text-embedding-3-small"
            
        # Get API key from config
        try:
            from ...dataflows.config import get_openai_api_key
            api_key = get_openai_api_key()
        except ImportError:
            api_key = None
            
        self.client = OpenAI(base_url=config["backend_url"], api_key=api_key)
        # Use EphemeralClient for in-memory storage (ChromaDB 1.0+)
        self.chroma_client = chromadb.EphemeralClient()
        
        # Create unique collection name to avoid collisions
        if market_analysis_id and symbol:
            collection_name = f"{name}_{symbol}_{market_analysis_id}"
        elif symbol:
            from datetime import datetime
            date_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
            collection_name = f"{name}_{symbol}_{date_suffix}"
        else:
            # Fallback for backward compatibility
            from datetime import datetime

            date_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
            collection_name = f"{name}_{date_suffix}"
        
        # Try to get existing collection or create new one
        try:
            self.situation_collection = self.chroma_client.get_collection(name=collection_name)
        except:
            self.situation_collection = self.chroma_client.create_collection(name=collection_name)

    def get_embedding(self, text):
        """Get OpenAI embedding for a text, handling long texts by splitting and averaging embeddings"""
        # text-embedding-3-small has a max context length of 8192 tokens
        # Conservative estimate: ~3 characters per token for safety margin
        max_chars = 24000  # ~8000 tokens * 3 chars/token
        
        if len(text) <= max_chars:
            # Text is short enough, get embedding directly
            response = self.client.embeddings.create(
                model=self.embedding, input=text
            )
            return response.data[0].embedding
        
        # Text is too long, split into chunks and average embeddings
        ta_logger.info(f"Text length {len(text)} exceeds limit, splitting into chunks for embedding")
        
        # Split text into overlapping chunks to preserve context
        chunk_size = max_chars - 1000  # Leave some buffer
        overlap = 500  # Overlap between chunks to preserve context
        chunks = []
        
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            
            # Try to break at sentence boundaries to preserve meaning
            if end < len(text):
                # Look for sentence ending within last 200 chars
                sentence_break = text.rfind('.', end - 200, end)
                if sentence_break > start:
                    end = sentence_break + 1
            
            chunks.append(text[start:end])
            start = max(start + chunk_size - overlap, end)
        
        ta_logger.info(f"Split text into {len(chunks)} chunks for embedding")
        
        # Get embeddings for all chunks
        chunk_embeddings = []
        for i, chunk in enumerate(chunks):
            try:
                response = self.client.embeddings.create(
                    model=self.embedding, input=chunk
                )
                chunk_embeddings.append(response.data[0].embedding)
            except Exception as e:
                ta_logger.error(f"Failed to get embedding for chunk {i}: {e}", exc_info=True)
                continue
        
        if not chunk_embeddings:
            raise ValueError("Failed to get embeddings for any chunks")
        
        # Average the embeddings (simple approach)
        averaged_embedding = np.mean(chunk_embeddings, axis=0).tolist()
        
        return averaged_embedding
    
    def add_situations(self, situations_and_advice):
        """Add financial situations and their corresponding advice. Parameter is a list of tuples (situation, rec)"""

        situations = []
        advice = []
        ids = []
        embeddings = []

        offset = self.situation_collection.count()

        for i, (situation, recommendation) in enumerate(situations_and_advice):
            situations.append(situation)
            advice.append(recommendation)
            ids.append(str(offset + i))
            embeddings.append(self.get_embedding(situation))

        self.situation_collection.add(
            documents=situations,
            metadatas=[{"recommendation": rec} for rec in advice],
            embeddings=embeddings,
            ids=ids,
        )

    def get_memories(self, current_situation, n_matches=1):
        """Find matching recommendations using OpenAI embeddings"""
        query_embedding = self.get_embedding(current_situation)

        results = self.situation_collection.query(
            query_embeddings=[query_embedding],
            n_results=n_matches,
            include=["metadatas", "documents", "distances"],
        )

        matched_results = []
        for i in range(len(results["documents"][0])):
            matched_results.append(
                {
                    "matched_situation": results["documents"][0][i],
                    "recommendation": results["metadatas"][0][i]["recommendation"],
                    "similarity_score": 1 - results["distances"][0][i],
                }
            )

        return matched_results


if __name__ == "__main__":
    # Example usage
    matcher = FinancialSituationMemory()

    # Example data
    example_data = [
        (
            "High inflation rate with rising interest rates and declining consumer spending",
            "Consider defensive sectors like consumer staples and utilities. Review fixed-income portfolio duration.",
        ),
        (
            "Tech sector showing high volatility with increasing institutional selling pressure",
            "Reduce exposure to high-growth tech stocks. Look for value opportunities in established tech companies with strong cash flows.",
        ),
        (
            "Strong dollar affecting emerging markets with increasing forex volatility",
            "Hedge currency exposure in international positions. Consider reducing allocation to emerging market debt.",
        ),
        (
            "Market showing signs of sector rotation with rising yields",
            "Rebalance portfolio to maintain target allocations. Consider increasing exposure to sectors benefiting from higher rates.",
        ),
    ]

    # Add the example situations and recommendations
    matcher.add_situations(example_data)

    # Example query
    current_situation = """
    Market showing increased volatility in tech sector, with institutional investors 
    reducing positions and rising interest rates affecting growth stock valuations
    """

    try:
        recommendations = matcher.get_memories(current_situation, n_matches=2)

        for i, rec in enumerate(recommendations, 1):
            print(f"\nMatch {i}:")
            print(f"Similarity Score: {rec['similarity_score']:.2f}")
            print(f"Matched Situation: {rec['matched_situation']}")
            print(f"Recommendation: {rec['recommendation']}")

    except Exception as e:
        print(f"Error during recommendation: {str(e)}")
