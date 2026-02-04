import chromadb
from chromadb.config import Settings
from openai import OpenAI
import numpy as np
from ... import logger
import os
from langchain_text_splitters import RecursiveCharacterTextSplitter


class FinancialSituationMemory:
    def __init__(self, name, config, symbol=None, market_analysis_id=None, expert_instance_id=None):
        # Get embedding model from config
        # Format: "Provider/model_name" e.g., "OpenAI/text-embedding-3-small", "NagaAI/text-embedding-3-small", or "Local/all-MiniLM-L6-v2"
        embedding_selection = config.get("embedding_model", "OpenAI/text-embedding-3-small")
        
        # Parse the embedding model selection to get provider and model name
        if "/" in embedding_selection:
            provider, model_name = embedding_selection.split("/", 1)
            provider = provider.lower()
        else:
            # Assume OpenAI if no provider prefix
            provider = "openai"
            model_name = embedding_selection
        
        self.embedding = model_name
        self.embedding_provider = provider
        
        # Initialize embedding client based on provider
        if provider == "local":
            # Use local sentence-transformers model (no API calls)
            try:
                from sentence_transformers import SentenceTransformer
                import torch

                # Determine device
                device = "cuda" if torch.cuda.is_available() else "cpu"

                def load_model(force_redownload=False):
                    """Load the embedding model, optionally forcing redownload."""
                    cache_folder = None
                    if force_redownload:
                        # Clear cached model to force fresh download
                        import shutil
                        from huggingface_hub import snapshot_download
                        from huggingface_hub.constants import HF_HUB_CACHE
                        model_cache_path = os.path.join(HF_HUB_CACHE, f"models--{model_name.replace('/', '--')}")
                        if os.path.exists(model_cache_path):
                            logger.warning(f"Clearing corrupted model cache: {model_cache_path}")
                            shutil.rmtree(model_cache_path, ignore_errors=True)

                    return SentenceTransformer(
                        model_name,
                        device=device,
                        trust_remote_code=True,
                        cache_folder=cache_folder
                    )

                try:
                    # First attempt: normal load
                    self.local_embedding_model = load_model()
                except NotImplementedError as e:
                    # Handle meta tensor error - usually means corrupted model files
                    if "meta tensor" in str(e):
                        logger.warning(f"Meta tensor error (likely corrupted cache), re-downloading model: {e}")
                        self.local_embedding_model = load_model(force_redownload=True)
                    else:
                        raise

                self.client = None  # No OpenAI client needed
                logger.info(f"Initialized local embedding model: {model_name} on {device}")
            except ImportError:
                logger.error("sentence-transformers not installed. Install with: pip install sentence-transformers")
                raise ImportError("sentence-transformers package required for local embeddings. Install with: pip install sentence-transformers")
        else:
            # Use OpenAI-compatible API provider
            self.local_embedding_model = None
            
            # Get provider configuration from models_registry
            try:
                from ba2_trade_platform.core.models_registry import PROVIDER_CONFIG
                from ba2_trade_platform.config import get_app_setting
                
                provider_config = PROVIDER_CONFIG.get(provider, {})
                embedding_backend_url = provider_config.get("base_url", "https://api.openai.com/v1")
                api_key_setting = provider_config.get("api_key_setting", "openai_api_key")
                
                # Get API key from app settings
                api_key = get_app_setting(api_key_setting)
                if not api_key:
                    # Fallback to openai_api_key
                    api_key = get_app_setting("openai_api_key")
                    
            except ImportError:
                # Fallback for older configurations
                from ...dataflows.config import get_openai_api_key
                embedding_backend_url = "https://api.openai.com/v1"
                api_key = get_openai_api_key()
                
            self.client = OpenAI(base_url=embedding_backend_url, api_key=api_key)
        
        # Use persistent client with expert-specific subdirectory
        from ba2_trade_platform.config import CACHE_FOLDER
        
        # Sanitize model name for use in filesystem path (replace / with _)
        model_path_name = model_name.replace("/", "_").replace("\\", "_")
        
        if expert_instance_id:
            # Include symbol and embedding model in path to avoid ChromaDB instance conflicts
            # This ensures different embedding models have separate databases
            if symbol:
                persist_directory = os.path.join(CACHE_FOLDER, "chromadb", f"expert_{expert_instance_id}", model_path_name, symbol)
            else:
                persist_directory = os.path.join(CACHE_FOLDER, "chromadb", f"expert_{expert_instance_id}", model_path_name)
        else:
            # Fallback for backward compatibility
            persist_directory = os.path.join(CACHE_FOLDER, "chromadb", "default", model_path_name)
        
        # Create directory if it doesn't exist
        os.makedirs(persist_directory, exist_ok=True)
        
        # Use PersistentClient for disk storage with explicit settings to avoid tenant issues
        # ChromaDB has a bug with tenant validation in some versions, so we use Settings to bypass it
        chroma_settings = Settings(
            anonymized_telemetry=False,
            allow_reset=True,
            is_persistent=True
        )
        
        try:
            self.chroma_client = chromadb.PersistentClient(
                path=persist_directory,
                settings=chroma_settings
            )
        except Exception as e:
            logger.warning(f"Failed to create PersistentClient with settings: {e}, falling back to simple initialization")
            # Fallback: try without settings
            self.chroma_client = chromadb.PersistentClient(path=persist_directory)
        
        # Create collection name without analysis_id (only name and symbol)
        if symbol:
            collection_name = f"{name}_{symbol}"
        else:
            collection_name = name
        
        # Sanitize collection name (ChromaDB requires alphanumeric, underscore, hyphen)
        collection_name = collection_name.replace(' ', '_').replace('.', '_')
        
        # Try to get existing collection or create new one
        try:
            self.situation_collection = self.chroma_client.get_collection(name=collection_name)
            logger.debug(f"Retrieved existing ChromaDB collection: {collection_name}")
        except:
            self.situation_collection = self.chroma_client.create_collection(name=collection_name)
            logger.debug(f"Created new ChromaDB collection: {collection_name}")

    def get_embedding(self, text):
        """Get embeddings for a text, using RecursiveCharacterTextSplitter for long texts.
        
        Supports both API-based (OpenAI, etc.) and local (sentence-transformers) embedding models.
        
        Returns:
            list: List of embeddings (one per chunk). If text is short, returns list with single embedding.
        """
        if self.embedding_provider == "local":
            # Use local sentence-transformers model
            # Most sentence-transformer models have a max length of 256-512 tokens
            max_chars = 1500  # ~500 tokens * 3 chars/token (conservative for local models)
            
            if len(text) <= max_chars:
                # Text is short enough, get embedding directly
                embedding = self.local_embedding_model.encode(text, convert_to_numpy=True)
                return [embedding.tolist()]
            
            # Text is too long, use RecursiveCharacterTextSplitter
            logger.info(f"Text length {len(text)} exceeds limit, splitting into chunks for local embedding")
            
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1200,  # ~400 tokens, well under most model limits
                chunk_overlap=200,
                length_function=len,
                separators=["\n\n", "\n", ". ", " ", ""]
            )
            
            chunks = text_splitter.split_text(text)
            logger.info(f"Split text into {len(chunks)} chunks for local embedding")
            
            # Get embeddings for all chunks
            chunk_embeddings = []
            for chunk in chunks:
                try:
                    embedding = self.local_embedding_model.encode(chunk, convert_to_numpy=True)
                    chunk_embeddings.append(embedding.tolist())
                except Exception as e:
                    logger.error(f"Failed to get local embedding for chunk: {e}", exc_info=True)
                    continue
            
            return chunk_embeddings
        
        else:
            # Use API-based embeddings (OpenAI, NagaAI, etc.)
            # text-embedding-3-small has a max context length of 8192 tokens
            # Conservative estimate: ~2 characters per token (safe for JSON, code, special chars)
            max_chars = 16000  # ~8000 tokens * 2 chars/token (conservative)
            
            if len(text) <= max_chars:
                # Text is short enough, get embedding directly
                response = self.client.embeddings.create(
                    model=self.embedding, input=text
                )
                return [response.data[0].embedding]
            
            # Text is too long, use RecursiveCharacterTextSplitter
            logger.info(f"Text length {len(text)} exceeds limit, splitting into chunks for embedding")
            
            # Use RecursiveCharacterTextSplitter for intelligent chunking
            # chunk_size of 14000 chars ≈ 7000 tokens, leaving buffer for 8192 limit
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=14000,  # Conservative: ~7000 tokens, well under 8192 limit
                chunk_overlap=500,  # Overlap to preserve context
                length_function=len,
                separators=["\n\n", "\n", ". ", " ", ""]  # Try to split at natural boundaries
            )
            
            chunks = text_splitter.split_text(text)
            logger.info(f"Split text into {len(chunks)} chunks for embedding")
            
            # Get embeddings for all chunks
            chunk_embeddings = []
            for i, chunk in enumerate(chunks):
                try:
                    response = self.client.embeddings.create(
                        model=self.embedding, input=chunk
                    )
                    chunk_embeddings.append(response.data[0].embedding)
                except Exception as e:
                    logger.error(f"Failed to get embedding for chunk {i}: {e}", exc_info=True)
                    continue
        
        if not chunk_embeddings:
            raise ValueError("Failed to get embeddings for any chunks")
        
        return chunk_embeddings
    
    def add_situations(self, situations_and_advice):
        """Add financial situations and their corresponding advice. Parameter is a list of tuples (situation, rec)"""

        situations = []
        advice = []
        ids = []
        embeddings = []

        offset = self.situation_collection.count()
        current_id = offset

        for situation, recommendation in situations_and_advice:
            # Get embeddings (returns list of embeddings for chunks)
            situation_embeddings = self.get_embedding(situation)
            
            # Add each chunk as a separate document
            for chunk_idx, embedding in enumerate(situation_embeddings):
                situations.append(situation)  # Store full situation for each chunk
                advice.append(recommendation)
                ids.append(str(current_id))
                embeddings.append(embedding)
                current_id += 1

        self.situation_collection.add(
            documents=situations,
            metadatas=[{"recommendation": rec} for rec in advice],
            embeddings=embeddings,
            ids=ids,
        )

    def get_memories(self, current_situation, n_matches=1, aggregate_chunks=False):
        """Find matching recommendations using OpenAI embeddings
        
        Args:
            current_situation (str): The current financial situation to match
            n_matches (int): Number of matches to return per chunk (default: 1)
            aggregate_chunks (bool): If True, averages chunk embeddings before querying (faster but less accurate).
                                    If False, queries with each chunk separately and merges results (default, more accurate).
        
        Returns:
            list: List of matched results with similarity scores
        """
        # Get embeddings (returns list)
        query_embeddings = self.get_embedding(current_situation)
        
        if len(query_embeddings) > 1 and not aggregate_chunks:
            # Query with each chunk separately and merge results
            logger.debug(f"Querying with {len(query_embeddings)} chunks separately")
            all_matches = {}  # Use dict to deduplicate by ID
            
            for chunk_idx, query_embedding in enumerate(query_embeddings):
                results = self.situation_collection.query(
                    query_embeddings=[query_embedding],
                    n_results=n_matches,
                    include=["metadatas", "documents", "distances"],
                )
                
                # Collect matches from this chunk
                for i in range(len(results["documents"][0])):
                    doc_id = results["ids"][0][i] if "ids" in results else str(i)
                    similarity = 1 - results["distances"][0][i]
                    
                    # Keep best similarity score if document appears in multiple chunk queries
                    if doc_id not in all_matches or all_matches[doc_id]["similarity_score"] < similarity:
                        all_matches[doc_id] = {
                            "matched_situation": results["documents"][0][i],
                            "recommendation": results["metadatas"][0][i]["recommendation"],
                            "similarity_score": similarity,
                            "chunk_match": chunk_idx  # Track which chunk matched
                        }
            
            # Sort by similarity and return top n_matches
            matched_results = sorted(all_matches.values(), key=lambda x: x["similarity_score"], reverse=True)[:n_matches]
            
        else:
            # Average embeddings if multiple chunks (original behavior)
            if len(query_embeddings) > 1:
                logger.debug(f"Averaging {len(query_embeddings)} chunk embeddings")
                query_embedding = np.mean(query_embeddings, axis=0).tolist()
            else:
                query_embedding = query_embeddings[0]

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
        # Option 1: Average chunk embeddings (default, faster)
        print("\n=== Option 1: Averaged Embeddings ===")
        recommendations = matcher.get_memories(current_situation, n_matches=2, aggregate_chunks=True)

        for i, rec in enumerate(recommendations, 1):
            print(f"\nMatch {i}:")
            print(f"Similarity Score: {rec['similarity_score']:.2f}")
            print(f"Matched Situation: {rec['matched_situation']}")
            print(f"Recommendation: {rec['recommendation']}")
        
        # Option 2: Query with each chunk separately and merge results (more accurate for long texts)
        print("\n\n=== Option 2: Per-Chunk Querying (merged results) ===")
        recommendations = matcher.get_memories(current_situation, n_matches=2, aggregate_chunks=False)

        for i, rec in enumerate(recommendations, 1):
            print(f"\nMatch {i}:")
            print(f"Similarity Score: {rec['similarity_score']:.2f}")
            print(f"Matched Situation: {rec['matched_situation']}")
            print(f"Recommendation: {rec['recommendation']}")
            if 'chunk_match' in rec:
                print(f"Best Chunk Match: {rec['chunk_match']}")

    except Exception as e:
        print(f"Error during recommendation: {str(e)}")
