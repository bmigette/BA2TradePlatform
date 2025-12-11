#!/usr/bin/env python
"""
Migration script to convert legacy model settings to the new registry-based format.

Old format examples:
- "gpt-5-2025-08-07" -> "native/gpt5"
- "gpt-5-mini-2025-08-07" -> "native/gpt5_mini"
- "o4-mini" -> "native/o4_mini"
- "gpt-4o-mini" -> "native/gpt4o_mini"
- "claude-3-5-sonnet" -> "native/claude35_sonnet"
- "NagaAI/gpt-5" -> "nagaai/gpt5"

New format: "provider/friendly_name"
- provider: nagaai, openai, google, anthropic, openrouter, xai, moonshot, deepseek, or "native"
- friendly_name: The key from MODELS registry (e.g., gpt5, gpt5_mini, claude35_sonnet)

Usage:
    python migrate_model_settings.py [--dry-run] [--verbose]
"""

import os
import sys
import argparse
import re
from typing import Dict, List, Optional, Tuple

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import ExpertSetting, AppSetting
from ba2_trade_platform.core.models_registry import MODELS, PROVIDER_CONFIG
from ba2_trade_platform.logger import logger


# Settings keys that contain model values
MODEL_SETTING_KEYS = [
    'deep_think_llm',
    'quick_think_llm', 
    'risk_manager_model',
]


# Mapping from old provider-specific model names to friendly names
# This maps the actual model identifiers to our registry keys
OLD_MODEL_NAME_MAPPING: Dict[str, str] = {}

# Build the mapping dynamically from the registry
def build_model_name_mapping():
    """Build a mapping from provider-specific names to friendly names."""
    mapping = {}
    
    for friendly_name, model_info in MODELS.items():
        # Map all provider-specific names to this friendly name
        for provider, provider_model_name in model_info.get("provider_names", {}).items():
            # Store both the full name and lowercase version
            mapping[provider_model_name.lower()] = friendly_name
            mapping[provider_model_name] = friendly_name
            
            # Also store without version suffixes for common patterns
            # e.g., "gpt-5-2025-08-07" -> "gpt-5"
            base_name = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', provider_model_name)
            if base_name != provider_model_name:
                mapping[base_name.lower()] = friendly_name
                mapping[base_name] = friendly_name
    
    # Add some manual mappings for legacy formats
    legacy_mappings = {
        # GPT-5 family
        "gpt-5": "gpt5",
        "gpt5": "gpt5",
        "gpt-5-mini": "gpt5_mini",
        "gpt5-mini": "gpt5_mini",
        "gpt-5-nano": "gpt5_nano",
        "gpt5-nano": "gpt5_nano",
        "gpt-5-chat-latest": "gpt5_chat",
        
        # GPT-4o family
        "gpt-4o": "gpt4o",
        "gpt4o": "gpt4o",
        "gpt-4o-mini": "gpt4o_mini",
        "gpt4o-mini": "gpt4o_mini",
        
        # O-series (reasoning)
        "o1": "o1",
        "o1-mini": "o1_mini",
        "o1-preview": "o1_preview",
        "o3": "o3",
        "o3-mini": "o3_mini",
        "o4-mini": "o4_mini",
        "o4": "o4",
        
        # Claude family
        "claude-3-opus": "claude3_opus",
        "claude-3-sonnet": "claude3_sonnet", 
        "claude-3-haiku": "claude3_haiku",
        "claude-3-5-opus": "claude35_opus",
        "claude-3-5-sonnet": "claude35_sonnet",
        "claude-3-5-haiku": "claude35_haiku",
        "claude-3.5-sonnet": "claude35_sonnet",
        "claude-3.5-haiku": "claude35_haiku",
        "claude-4-opus": "claude4_opus",
        "claude-4-sonnet": "claude4_sonnet",
        
        # Gemini family
        "gemini-1.5-pro": "gemini15_pro",
        "gemini-1.5-flash": "gemini15_flash",
        "gemini-2.0-pro": "gemini20_pro",
        "gemini-2.0-flash": "gemini20_flash",
        "gemini-2.5-pro": "gemini25_pro",
        "gemini-2.5-flash": "gemini25_flash",
        "gemini-3-pro-preview": "gemini3_pro",
        "gemini-3-pro": "gemini3_pro",
        
        # Grok family
        "grok-2": "grok2",
        "grok-3": "grok3",
        "grok-3-mini": "grok3_mini",
        "grok-4": "grok4",
        "grok-4-0709": "grok4",
        "grok-4.1-fast-reasoning": "grok4.1_fast_reasoning",
        
        # DeepSeek family
        "deepseek-chat": "deepseek_chat",
        "deepseek-r1": "deepseek_r1",
        "deepseek-reasoner": "deepseek_reasoner",
        "deepseek-v3": "deepseek_v3",
        "deepseek-v3.2": "deepseek_v3.2",
        
        # Moonshot/Kimi family
        "moonshot-v1": "moonshot_v1",
        "kimi-1.5": "kimi15",
        "kimi-k2": "kimi_k2",
        "kimi-k2-thinking": "kimi_k2_thinking",
        
        # Qwen family
        "qwen3-max": "qwen3_max",
        "qwen3-next-80b-a3b-thinking": "qwen3_80b_thinking",
        "qwen-max": "qwen_max",
    }
    
    for old_name, friendly_name in legacy_mappings.items():
        mapping[old_name.lower()] = friendly_name
        mapping[old_name] = friendly_name
    
    return mapping


def parse_old_model_value(value: str) -> Tuple[Optional[str], str]:
    """
    Parse an old model value to extract provider and model name.
    
    Args:
        value: The old model value (e.g., "gpt-5-2025-08-07", "NagaAI/gpt-5")
        
    Returns:
        Tuple of (provider, model_name) where provider may be None for legacy format
    """
    if not value:
        return None, ""
    
    # Check if it's already in new format (provider/friendly_name)
    if "/" in value:
        parts = value.split("/", 1)
        provider = parts[0].lower()
        model_name = parts[1]
        return provider, model_name
    
    # Legacy format - just the model name
    return None, value


def convert_model_setting(old_value: str, model_mapping: Dict[str, str]) -> Optional[str]:
    """
    Convert an old model setting value to the new format.
    
    Args:
        old_value: The old model value
        model_mapping: Mapping from old names to friendly names
        
    Returns:
        New value in "provider/friendly_name" format, or None if already valid or should be skipped
    """
    if not old_value:
        return None
    
    # Skip "None" string values - these are intentionally unset
    if old_value.lower() == "none":
        return None
    
    provider, model_name = parse_old_model_value(old_value)
    
    # Check if it's already in new format with valid friendly name
    if provider and model_name in MODELS:
        # Already in new format with valid friendly name
        return None
    
    # Try to find the friendly name
    friendly_name = None
    
    # Check direct mapping
    if model_name.lower() in model_mapping:
        friendly_name = model_mapping[model_name.lower()]
    elif model_name in model_mapping:
        friendly_name = model_mapping[model_name]
    else:
        # Try removing common suffixes/prefixes
        cleaned = model_name.lower()
        cleaned = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', cleaned)  # Remove date suffix
        cleaned = re.sub(r'-latest$', '', cleaned)  # Remove -latest suffix
        cleaned = re.sub(r'-preview$', '', cleaned)  # Remove -preview suffix
        
        if cleaned in model_mapping:
            friendly_name = model_mapping[cleaned]
    
    if not friendly_name:
        # Couldn't map - return None to indicate manual review needed
        return None
    
    # Determine provider
    if provider:
        # Normalize provider name
        provider = provider.lower()
        if provider == "naga" or provider == "naga_ai" or provider == "nagaac":
            provider = "nagaai"
        elif provider not in PROVIDER_CONFIG and provider != "native":
            # Unknown provider, try to determine from model's native provider
            model_info = MODELS.get(friendly_name)
            if model_info:
                provider = "native"
    else:
        # No provider specified - use native
        provider = "native"
    
    return f"{provider}/{friendly_name}"


def migrate_expert_settings(dry_run: bool = True, verbose: bool = False) -> Dict[str, any]:
    """
    Migrate all expert settings with model values to new format.
    
    Args:
        dry_run: If True, don't actually update the database
        verbose: If True, print detailed information
        
    Returns:
        Dictionary with migration statistics
    """
    model_mapping = build_model_name_mapping()
    
    stats = {
        "total_checked": 0,
        "already_valid": 0,
        "migrated": 0,
        "failed": [],
        "changes": [],
    }
    
    with get_db() as session:
        # Find all settings with model-related keys
        settings = session.query(ExpertSetting).filter(
            ExpertSetting.key.in_(MODEL_SETTING_KEYS)
        ).all()
        
        for setting in settings:
            stats["total_checked"] += 1
            old_value = setting.value_str
            
            if not old_value:
                continue
            
            new_value = convert_model_setting(old_value, model_mapping)
            
            if new_value is None:
                # Check if already valid or intentionally unset
                if old_value.lower() == "none":
                    stats["already_valid"] += 1
                    if verbose:
                        print(f"  [SKIP] Expert {setting.instance_id} / {setting.key}: '{old_value}' (intentionally unset)")
                    continue
                    
                provider, model_name = parse_old_model_value(old_value)
                if provider and model_name in MODELS:
                    stats["already_valid"] += 1
                    if verbose:
                        print(f"  [OK] Expert {setting.instance_id} / {setting.key}: '{old_value}' (already valid)")
                else:
                    # Couldn't convert
                    stats["failed"].append({
                        "instance_id": setting.instance_id,
                        "key": setting.key,
                        "value": old_value,
                    })
                    print(f"  [WARN] Expert {setting.instance_id} / {setting.key}: '{old_value}' - could not convert, manual review needed")
            else:
                stats["migrated"] += 1
                stats["changes"].append({
                    "instance_id": setting.instance_id,
                    "key": setting.key,
                    "old_value": old_value,
                    "new_value": new_value,
                })
                
                print(f"  [MIGRATE] Expert {setting.instance_id} / {setting.key}: '{old_value}' -> '{new_value}'")
                
                if not dry_run:
                    setting.value_str = new_value
        
        if not dry_run:
            session.commit()
            print("\nDatabase changes committed.")
        else:
            print("\n[DRY RUN] No database changes made.")
    
    return stats


def migrate_app_settings(dry_run: bool = True, verbose: bool = False) -> Dict[str, any]:
    """
    Migrate app-level settings with model values to new format.
    
    Args:
        dry_run: If True, don't actually update the database
        verbose: If True, print detailed information
        
    Returns:
        Dictionary with migration statistics
    """
    model_mapping = build_model_name_mapping()
    
    # App-level model settings (if any)
    app_model_keys = [
        'default_deep_think_llm',
        'default_quick_think_llm',
        'default_risk_manager_model',
    ]
    
    stats = {
        "total_checked": 0,
        "already_valid": 0,
        "migrated": 0,
        "failed": [],
        "changes": [],
    }
    
    with get_db() as session:
        settings = session.query(AppSetting).filter(
            AppSetting.key.in_(app_model_keys)
        ).all()
        
        for setting in settings:
            stats["total_checked"] += 1
            old_value = setting.value_str
            
            if not old_value:
                continue
            
            new_value = convert_model_setting(old_value, model_mapping)
            
            if new_value is None:
                # Check if already valid or intentionally unset
                if old_value.lower() == "none":
                    stats["already_valid"] += 1
                    if verbose:
                        print(f"  [SKIP] AppSetting {setting.key}: '{old_value}' (intentionally unset)")
                    continue
                    
                provider, model_name = parse_old_model_value(old_value)
                if provider and model_name in MODELS:
                    stats["already_valid"] += 1
                    if verbose:
                        print(f"  [OK] AppSetting {setting.key}: '{old_value}' (already valid)")
                else:
                    stats["failed"].append({
                        "key": setting.key,
                        "value": old_value,
                    })
                    print(f"  [WARN] AppSetting {setting.key}: '{old_value}' - could not convert")
            else:
                stats["migrated"] += 1
                stats["changes"].append({
                    "key": setting.key,
                    "old_value": old_value,
                    "new_value": new_value,
                })
                
                print(f"  [MIGRATE] AppSetting {setting.key}: '{old_value}' -> '{new_value}'")
                
                if not dry_run:
                    setting.value_str = new_value
        
        if not dry_run and stats["migrated"] > 0:
            session.commit()
    
    return stats


def print_summary(expert_stats: Dict, app_stats: Dict):
    """Print migration summary."""
    print("\n" + "=" * 60)
    print("MIGRATION SUMMARY")
    print("=" * 60)
    
    print("\nExpert Settings:")
    print(f"  Total checked:   {expert_stats['total_checked']}")
    print(f"  Already valid:   {expert_stats['already_valid']}")
    print(f"  Migrated:        {expert_stats['migrated']}")
    print(f"  Failed:          {len(expert_stats['failed'])}")
    
    print("\nApp Settings:")
    print(f"  Total checked:   {app_stats['total_checked']}")
    print(f"  Already valid:   {app_stats['already_valid']}")
    print(f"  Migrated:        {app_stats['migrated']}")
    print(f"  Failed:          {len(app_stats['failed'])}")
    
    if expert_stats['failed'] or app_stats['failed']:
        print("\n" + "-" * 60)
        print("ITEMS REQUIRING MANUAL REVIEW:")
        print("-" * 60)
        
        for item in expert_stats['failed']:
            print(f"  Expert {item['instance_id']} / {item['key']}: '{item['value']}'")
        
        for item in app_stats['failed']:
            print(f"  AppSetting {item['key']}: '{item['value']}'")
        
        print("\nThese values could not be automatically mapped to the new format.")
        print("Please update them manually in the database or through the UI.")


def list_available_models():
    """Print all available models in the registry."""
    print("\nAvailable Models in Registry:")
    print("-" * 60)
    
    for friendly_name, info in sorted(MODELS.items()):
        display_name = info.get("display_name", friendly_name)
        native = info.get("native_provider", "unknown")
        providers = list(info.get("provider_names", {}).keys())
        print(f"  {friendly_name:25} ({display_name}) - Native: {native}, Providers: {', '.join(providers)}")


def main():
    parser = argparse.ArgumentParser(
        description='Migrate model settings to new registry-based format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Dry run (default) - see what would change without modifying database
    python migrate_model_settings.py
    
    # Actually perform the migration
    python migrate_model_settings.py --apply
    
    # Verbose output showing all settings checked
    python migrate_model_settings.py --verbose
    
    # List all available models in the registry
    python migrate_model_settings.py --list-models
"""
    )
    
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually apply the migration (default is dry-run)'
    )
    
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show verbose output including already-valid settings'
    )
    
    parser.add_argument(
        '--list-models',
        action='store_true',
        help='List all available models in the registry and exit'
    )
    
    args = parser.parse_args()
    
    if args.list_models:
        list_available_models()
        return
    
    dry_run = not args.apply
    
    print("=" * 60)
    print("MODEL SETTINGS MIGRATION")
    print("=" * 60)
    
    if dry_run:
        print("\n[DRY RUN MODE] - No changes will be made to the database")
        print("Use --apply to actually perform the migration\n")
    else:
        print("\n[APPLY MODE] - Changes WILL be written to the database\n")
    
    print("\nMigrating Expert Settings...")
    print("-" * 40)
    expert_stats = migrate_expert_settings(dry_run=dry_run, verbose=args.verbose)
    
    print("\nMigrating App Settings...")
    print("-" * 40)
    app_stats = migrate_app_settings(dry_run=dry_run, verbose=args.verbose)
    
    print_summary(expert_stats, app_stats)
    
    if dry_run and (expert_stats['migrated'] > 0 or app_stats['migrated'] > 0):
        print("\n" + "=" * 60)
        print("To apply these changes, run:")
        print("  python migrate_model_settings.py --apply")
        print("=" * 60)


if __name__ == "__main__":
    main()
