"""
ChromaDB Embedding Migration Script

This script migrates all existing ChromaDB collections to a new embedding model.
It:
1. Scans all existing ChromaDB directories
2. Loads documents from old collections
3. Re-embeds them with the new model (Local/all-mpnet-base-v2)
4. Saves to the new model-specific directory structure

Usage:
    .venv\Scripts\python.exe migrate_chromadb_embeddings.py [--dry-run] [--target-model MODEL]

Examples:
    # Dry run to see what would be migrated
    .venv\Scripts\python.exe migrate_chromadb_embeddings.py --dry-run
    
    # Migrate to default model (Local/all-mpnet-base-v2)
    .venv\Scripts\python.exe migrate_chromadb_embeddings.py
    
    # Migrate to specific model
    .venv\Scripts\python.exe migrate_chromadb_embeddings.py --target-model "Local/all-MiniLM-L6-v2"
"""

import os
import sys
import argparse
from pathlib import Path
from typing import List, Dict, Tuple, Optional
import json

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from ba2_trade_platform.config import CACHE_FOLDER
from ba2_trade_platform.logger import logger
from ba2_trade_platform.core.db import get_db, update_instance
from ba2_trade_platform.core.models import ExpertInstance, ExpertSetting
from sqlmodel import select
import chromadb
from chromadb.config import Settings


def update_expert_settings(target_model: str, expert_id: Optional[int] = None, dry_run: bool = False) -> int:
    """
    Update ExpertInstance settings to use the new embedding model.
    
    Args:
        target_model: Target embedding model (e.g., "Local/all-mpnet-base-v2")
        expert_id: Optional expert ID to update only that expert
        dry_run: If True, show what would be updated without actually updating
        
    Returns:
        Number of experts updated
    """
    session = get_db()
    
    try:
        # Find all TradingAgents experts (or specific expert)
        if expert_id:
            statement = select(ExpertInstance).where(
                ExpertInstance.id == expert_id,
                ExpertInstance.expert == "TradingAgents"
            )
        else:
            statement = select(ExpertInstance).where(
                ExpertInstance.expert == "TradingAgents"
            )
        
        experts = session.exec(statement).all()
        
        if not experts:
            logger.warning("No TradingAgents experts found in database")
            return 0
        
        updated_count = 0
        for expert in experts:
            # Get embedding_model setting for this expert
            setting_statement = select(ExpertSetting).where(
                ExpertSetting.instance_id == expert.id,
                ExpertSetting.key == "embedding_model"
            )
            setting = session.exec(setting_statement).first()
            
            current_model = setting.value_str if setting else "N/A"
            
            if current_model == target_model:
                logger.info(f"  Expert #{expert.id} ({expert.alias or 'Unnamed'}): Already using {target_model}, skipping")
                continue
            
            if dry_run:
                logger.info(f"  Expert #{expert.id} ({expert.alias or 'Unnamed'}): Would change from {current_model} → {target_model}")
            else:
                # Update or create the setting
                if setting:
                    setting.value_str = target_model
                else:
                    setting = ExpertSetting(
                        instance_id=expert.id,
                        key="embedding_model",
                        value_str=target_model
                    )
                    session.add(setting)
                
                session.commit()
                logger.info(f"  ✓ Expert #{expert.id} ({expert.alias or 'Unnamed'}): Updated from {current_model} → {target_model}")
            
            updated_count += 1
        
        return updated_count
        
    except Exception as e:
        logger.error(f"Failed to update expert settings: {e}", exc_info=True)
        return 0
    finally:
        session.close()


def sanitize_model_name(model_name: str) -> str:
    """Convert model name to filesystem-safe format."""
    return model_name.replace("/", "_").replace("\\", "_")


def parse_expert_path(path: Path) -> Optional[Tuple[int, Optional[str], Optional[str]]]:
    """
    Parse expert path to extract expert_id, model_name, and symbol.
    
    Returns:
        Tuple of (expert_id, model_name, symbol) or None if not a valid expert path
        
    Path structures:
        - chromadb/expert_{id}/                      -> (id, None, None)
        - chromadb/expert_{id}/{symbol}/             -> (id, None, symbol)  [OLD]
        - chromadb/expert_{id}/{model}/{symbol}/     -> (id, model, symbol) [NEW]
    """
    parts = path.parts
    
    # Find chromadb directory
    try:
        chromadb_idx = parts.index("chromadb")
    except ValueError:
        return None
    
    # Check if next part is expert_*
    if chromadb_idx + 1 >= len(parts):
        return None
    
    expert_part = parts[chromadb_idx + 1]
    if not expert_part.startswith("expert_"):
        return None
    
    try:
        expert_id = int(expert_part.replace("expert_", ""))
    except ValueError:
        return None
    
    # Determine structure based on number of parts after expert_*
    remaining_parts = parts[chromadb_idx + 2:]
    
    if len(remaining_parts) == 0:
        # chromadb/expert_{id}/ [OLD - no symbol, no model]
        return (expert_id, None, None)
    elif len(remaining_parts) == 1:
        # chromadb/expert_{id}/{something}/ - could be symbol (OLD) or model (NEW without symbol)
        # We'll assume OLD format (symbol) if it's not a known model pattern
        something = remaining_parts[0]
        if "_" in something and not something.isupper():
            # Likely a model name (e.g., all-mpnet-base-v2, text-embedding-3-small)
            return (expert_id, something, None)
        else:
            # Likely a symbol (e.g., AAPL, MSFT)
            return (expert_id, None, something)
    elif len(remaining_parts) == 2:
        # chromadb/expert_{id}/{model}/{symbol}/ [NEW]
        return (expert_id, remaining_parts[0], remaining_parts[1])
    else:
        # Unknown structure
        return None


def get_chromadb_collections(path: Path) -> List[str]:
    """Get all ChromaDB collection names from a directory."""
    try:
        chroma_settings = Settings(
            anonymized_telemetry=False,
            allow_reset=True,
            is_persistent=True
        )
        client = chromadb.PersistentClient(path=str(path), settings=chroma_settings)
        collections = client.list_collections()
        return [col.name for col in collections]
    except Exception as e:
        logger.error(f"Failed to list collections in {path}: {e}")
        return []


def find_all_chromadb_directories() -> List[Tuple[Path, int, Optional[str], Optional[str], List[str]]]:
    """
    Find all ChromaDB directories in the cache folder.
    
    Returns:
        List of tuples: (path, expert_id, model_name, symbol, collection_names)
    """
    chromadb_root = Path(CACHE_FOLDER) / "chromadb"
    
    if not chromadb_root.exists():
        logger.warning(f"ChromaDB root directory not found: {chromadb_root}")
        return []
    
    directories = []
    
    # Walk through all subdirectories
    for root, dirs, files in os.walk(chromadb_root):
        root_path = Path(root)
        
        # Check if this directory contains ChromaDB data (has chroma.sqlite3)
        if (root_path / "chroma.sqlite3").exists():
            parsed = parse_expert_path(root_path)
            if parsed:
                expert_id, model_name, symbol = parsed
                collections = get_chromadb_collections(root_path)
                
                if collections:
                    directories.append((root_path, expert_id, model_name, symbol, collections))
                    logger.info(f"Found ChromaDB: expert={expert_id}, model={model_name or 'OLD'}, symbol={symbol or 'N/A'}, collections={len(collections)}")
    
    return directories


def initialize_embedding_model(model_selection: str):
    """Initialize the embedding model (local or API-based)."""
    if "/" in model_selection:
        provider, model_name = model_selection.split("/", 1)
        provider = provider.lower()
    else:
        provider = "openai"
        model_name = model_selection
    
    if provider == "local":
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"Loading local embedding model: {model_name}")
            logger.info(f"  (This may take a few minutes on first run - downloading model...)")
            
            # Show progress bar during model download/loading
            model = SentenceTransformer(model_name, device='cpu')
            
            logger.info(f"  ✓ Model loaded successfully")
            return model, "local", model_name
        except ImportError:
            logger.error("sentence-transformers not installed. Install with: pip install sentence-transformers")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to load embedding model '{model_name}': {e}")
            sys.exit(1)
    else:
        logger.error("Migration only supports local embedding models (Local/ prefix)")
        sys.exit(1)


def migrate_collection(
    source_path: Path,
    collection_name: str,
    target_path: Path,
    embedding_model,
    dry_run: bool = False
) -> Tuple[int, int]:
    """
    Migrate a single collection to new embedding model.
    
    Returns:
        Tuple of (documents_count, success_count)
    """
    try:
        # Load source collection
        chroma_settings = Settings(
            anonymized_telemetry=False,
            allow_reset=True,
            is_persistent=True
        )
        source_client = chromadb.PersistentClient(path=str(source_path), settings=chroma_settings)
        source_collection = source_client.get_collection(name=collection_name)
        
        # Get all documents
        results = source_collection.get(include=["documents", "metadatas", "embeddings"])
        
        if not results or not results.get("documents"):
            logger.info(f"  Collection '{collection_name}' is empty, skipping")
            return 0, 0
        
        doc_count = len(results["documents"])
        logger.info(f"  Found {doc_count} documents in collection '{collection_name}'")
        
        if dry_run:
            logger.info(f"  [DRY RUN] Would migrate {doc_count} documents to {target_path}")
            return doc_count, 0
        
        # Create target directory
        target_path.mkdir(parents=True, exist_ok=True)
        
        # Initialize target collection
        target_client = chromadb.PersistentClient(path=str(target_path), settings=chroma_settings)
        
        # Check if collection already exists in target
        try:
            target_collection = target_client.get_collection(name=collection_name)
            logger.warning(f"  Collection '{collection_name}' already exists in target, will append")
        except:
            target_collection = target_client.create_collection(name=collection_name)
            logger.info(f"  Created target collection '{collection_name}'")
        
        # Re-embed and add documents
        success_count = 0
        batch_size = 100
        
        for i in range(0, doc_count, batch_size):
            batch_docs = results["documents"][i:i+batch_size]
            batch_metas = results["metadatas"][i:i+batch_size] if results.get("metadatas") else [{}] * len(batch_docs)
            batch_ids = results["ids"][i:i+batch_size]
            
            # Generate new embeddings
            try:
                batch_embeddings = embedding_model.encode(batch_docs, show_progress_bar=False).tolist()
                
                # Add to target collection
                target_collection.add(
                    documents=batch_docs,
                    embeddings=batch_embeddings,
                    metadatas=batch_metas,
                    ids=batch_ids
                )
                
                success_count += len(batch_docs)
                logger.info(f"  Migrated {success_count}/{doc_count} documents")
                
            except Exception as e:
                logger.error(f"  Failed to migrate batch {i}-{i+batch_size}: {e}")
                continue
        
        logger.info(f"  ✓ Successfully migrated {success_count}/{doc_count} documents")
        return doc_count, success_count
        
    except Exception as e:
        logger.error(f"  Failed to migrate collection '{collection_name}': {e}")
        return 0, 0


def main():
    parser = argparse.ArgumentParser(
        description="Migrate ChromaDB embeddings to a new model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without actually migrating"
    )
    parser.add_argument(
        "--target-model",
        type=str,
        default="Local/all-mpnet-base-v2",
        help="Target embedding model (default: Local/all-mpnet-base-v2)"
    )
    parser.add_argument(
        "--expert-id",
        type=int,
        help="Only migrate specific expert ID (optional)"
    )
    parser.add_argument(
        "--skip-db-update",
        action="store_true",
        help="Skip updating expert settings in database (only migrate ChromaDB data)"
    )
    parser.add_argument(
        "--update-db-only",
        action="store_true",
        help="Only update expert settings in database (skip ChromaDB migration)"
    )
    
    args = parser.parse_args()
    
    # Validate mutually exclusive options
    if args.skip_db_update and args.update_db_only:
        parser.error("--skip-db-update and --update-db-only are mutually exclusive")
    
    logger.info("=" * 80)
    logger.info("ChromaDB Embedding Migration Script")
    logger.info("=" * 80)
    logger.info(f"Target model: {args.target_model}")
    logger.info(f"Dry run: {args.dry_run}")
    if args.update_db_only:
        logger.info(f"Mode: Update database settings only")
    elif args.skip_db_update:
        logger.info(f"Mode: Migrate ChromaDB data only")
    else:
        logger.info(f"Mode: Full migration (ChromaDB + database settings)")
    logger.info(f"Cache folder: {CACHE_FOLDER}")
    logger.info("")
    
    # If update-db-only mode, skip ChromaDB migration
    if args.update_db_only:
        logger.info("Skipping ChromaDB migration (--update-db-only mode)")
        logger.info("")
        
        # Go straight to database update
        logger.info("=" * 80)
        logger.info("Updating Expert Settings in Database")
        logger.info("=" * 80)
        
        updated_experts = update_expert_settings(
            target_model=args.target_model,
            expert_id=args.expert_id,
            dry_run=args.dry_run
        )
        
        logger.info("")
        logger.info("=" * 80)
        logger.info("Database Update Summary")
        logger.info("=" * 80)
        
        if updated_experts > 0:
            if args.dry_run:
                logger.info(f"[DRY RUN] Would update {updated_experts} expert(s)")
            else:
                logger.info(f"✓ Updated {updated_experts} expert(s) to use {args.target_model}")
        else:
            logger.info("No experts needed updating")
        
        logger.info("")
        if not args.dry_run:
            logger.info("✓ Database update complete!")
            logger.info("")
            logger.info("Next steps:")
            logger.info("  1. Run ChromaDB migration if not done yet:")
            logger.info(f"     .venv\\Scripts\\python.exe migrate_chromadb_embeddings.py --skip-db-update")
            logger.info("  2. Restart any running TradingAgents analysis to use new embeddings")
        else:
            logger.info("[DRY RUN] No changes were made. Run without --dry-run to update database.")
        
        return
    
    # Initialize embedding model
    if not args.dry_run:
        embedding_model, provider, model_name = initialize_embedding_model(args.target_model)
        target_model_path = sanitize_model_name(model_name)
        logger.info(f"Embedding model loaded: {model_name} (provider: {provider})")
        logger.info("")
    else:
        target_model_path = sanitize_model_name(args.target_model.split("/")[1] if "/" in args.target_model else args.target_model)
        embedding_model = None
    
    # Find all ChromaDB directories
    logger.info("Scanning for ChromaDB directories...")
    chromadb_dirs = find_all_chromadb_directories()
    
    if not chromadb_dirs:
        logger.warning("No ChromaDB directories found!")
        return
    
    logger.info(f"Found {len(chromadb_dirs)} ChromaDB directories")
    logger.info("")
    
    # Filter by expert_id if specified
    if args.expert_id:
        chromadb_dirs = [d for d in chromadb_dirs if d[1] == args.expert_id]
        logger.info(f"Filtered to expert_id={args.expert_id}: {len(chromadb_dirs)} directories")
        logger.info("")
    
    # Migrate each directory
    total_docs = 0
    total_migrated = 0
    
    for source_path, expert_id, old_model, symbol, collections in chromadb_dirs:
        logger.info(f"Processing: expert={expert_id}, model={old_model or 'OLD'}, symbol={symbol or 'N/A'}")
        
        # Skip if already using target model
        if old_model == target_model_path:
            logger.info(f"  Already using target model, skipping")
            logger.info("")
            continue
        
        # Build target path
        chromadb_root = Path(CACHE_FOLDER) / "chromadb"
        if symbol:
            target_path = chromadb_root / f"expert_{expert_id}" / target_model_path / symbol
        else:
            target_path = chromadb_root / f"expert_{expert_id}" / target_model_path
        
        logger.info(f"  Source: {source_path}")
        logger.info(f"  Target: {target_path}")
        logger.info(f"  Collections: {', '.join(collections)}")
        
        # Migrate each collection
        for collection_name in collections:
            docs, migrated = migrate_collection(
                source_path,
                collection_name,
                target_path,
                embedding_model,
                dry_run=args.dry_run
            )
            total_docs += docs
            total_migrated += migrated
        
        logger.info("")
    
    # Summary
    logger.info("=" * 80)
    logger.info("Migration Summary")
    logger.info("=" * 80)
    logger.info(f"Total documents found: {total_docs}")
    if not args.dry_run:
        logger.info(f"Total documents migrated: {total_migrated}")
        logger.info(f"Success rate: {(total_migrated/total_docs*100):.1f}%" if total_docs > 0 else "N/A")
    else:
        logger.info(f"[DRY RUN] Would migrate {total_docs} documents")
    logger.info("")
    
    # Update expert settings in database
    if not args.skip_db_update:
        logger.info("=" * 80)
        logger.info("Updating Expert Settings in Database")
        logger.info("=" * 80)
        
        updated_experts = update_expert_settings(
            target_model=args.target_model,
            expert_id=args.expert_id,
            dry_run=args.dry_run
        )
        
        if updated_experts > 0:
            if args.dry_run:
                logger.info(f"[DRY RUN] Would update {updated_experts} expert(s)")
            else:
                logger.info(f"✓ Updated {updated_experts} expert(s) to use {args.target_model}")
        else:
            logger.info("No experts needed updating")
        logger.info("")
    
    if not args.dry_run:
        logger.info("✓ Migration complete!")
        logger.info("")
        logger.info("Next steps:")
        if args.skip_db_update:
            logger.info("  1. Manually update expert settings in Expert Settings page")
            logger.info(f"     Change 'embedding_model' to: {args.target_model}")
        else:
            logger.info("  1. Expert settings have been updated automatically ✓")
        logger.info("  2. Restart any running TradingAgents analysis to use new embeddings")
        logger.info("  3. Verify new embeddings work correctly")
        logger.info("  4. Delete old ChromaDB directories after verification:")
        logger.info("")
        for source_path, expert_id, old_model, symbol, collections in chromadb_dirs:
            if old_model != target_model_path:
                logger.info(f"     {source_path}")
    else:
        logger.info("[DRY RUN] No changes were made. Run without --dry-run to perform migration.")


if __name__ == "__main__":
    main()
