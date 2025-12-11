#!/usr/bin/env python
"""
Migration script to convert model settings from nagaai to native providers.

This script performs the following migrations:
1. nagaai/gpt* models -> openai/gpt* (OpenAI native)
2. nagaai/grok* models -> xai/grok* (xAI native)
3. nagaai/gemini* models -> google/gemini* (Google native)
4. nagaai/deepseek* models -> deepseek/deepseek* (DeepSeek native)
5. nagaai/kimi* or nagaai/moonshot* models -> moonshot/kimi* (Moonshot native)
6. All gpt5, gpt5_mini, gpt5_nano variants -> gpt5.2, gpt5.2_mini equivalents

Usage:
    python migrate_to_native_providers.py              # Dry run (preview changes)
    python migrate_to_native_providers.py --apply      # Apply changes
    python migrate_to_native_providers.py --verbose    # Show all settings checked
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
from ba2_trade_platform.core.models_registry import (
    MODELS, PROVIDER_CONFIG,
    PROVIDER_OPENAI, PROVIDER_NAGAAI, PROVIDER_GOOGLE, 
    PROVIDER_XAI, PROVIDER_MOONSHOT, PROVIDER_DEEPSEEK
)
from ba2_trade_platform.logger import logger


# Settings keys that contain model values
MODEL_SETTING_KEYS = [
    'deep_think_llm',
    'quick_think_llm', 
    'risk_manager_model',
    'dataprovider_websearch_model',
]


# GPT-5 to GPT-5.2 migration mapping
GPT5_TO_GPT52_MAPPING = {
    "gpt5": "gpt5.2",
    "gpt5_mini": "gpt5.2_mini",
    "gpt5_nano": "gpt5.2_mini",  # Map nano to mini since 5.2 doesn't have nano
    "gpt5_chat": "gpt5.2",       # Map chat to base 5.2
    "gpt5_codex": "gpt5.2",      # Map codex to base 5.2
    "gpt5.1": "gpt5.2",          # Map 5.1 to 5.2
}


# Provider mapping based on model family
def get_native_provider_for_model(friendly_name: str) -> Optional[str]:
    """
    Determine the native provider for a model based on its friendly name.
    
    Returns the provider string or None if should stay on nagaai.
    """
    # GPT models -> OpenAI
    if friendly_name.startswith("gpt") or friendly_name.startswith("o1") or \
       friendly_name.startswith("o3") or friendly_name.startswith("o4"):
        return PROVIDER_OPENAI
    
    # Grok models -> xAI
    if friendly_name.startswith("grok"):
        return PROVIDER_XAI
    
    # Gemini models -> Google
    if friendly_name.startswith("gemini"):
        return PROVIDER_GOOGLE
    
    # DeepSeek models -> DeepSeek
    if friendly_name.startswith("deepseek"):
        return PROVIDER_DEEPSEEK
    
    # Kimi/Moonshot models -> Moonshot
    if friendly_name.startswith("kimi") or friendly_name.startswith("moonshot"):
        return PROVIDER_MOONSHOT
    
    # Other models stay on nagaai (claude, qwen, llama, etc.)
    return None


def parse_model_value(value: str) -> Tuple[Optional[str], str]:
    """
    Parse a model value to extract provider and friendly name.
    
    Args:
        value: The model value (e.g., "nagaai/gpt5", "openai/gpt4o")
        
    Returns:
        Tuple of (provider, friendly_name)
    """
    if not value or "/" not in value:
        return None, value
    
    parts = value.split("/", 1)
    provider = parts[0].lower()
    friendly_name = parts[1]
    return provider, friendly_name


def migrate_model_value(old_value: str) -> Tuple[Optional[str], str]:
    """
    Migrate a model value to use native provider and upgrade GPT-5 to GPT-5.2.
    
    Args:
        old_value: The current model value
        
    Returns:
        Tuple of (new_value, reason) where new_value is None if no migration needed
    """
    if not old_value:
        return None, "empty value"
    
    provider, friendly_name = parse_model_value(old_value)
    
    if not provider:
        return None, "not in provider/model format"
    
    new_provider = provider
    new_friendly_name = friendly_name
    reasons = []
    
    # Step 1: Upgrade GPT-5 to GPT-5.2
    if friendly_name in GPT5_TO_GPT52_MAPPING:
        new_friendly_name = GPT5_TO_GPT52_MAPPING[friendly_name]
        reasons.append(f"upgraded {friendly_name} -> {new_friendly_name}")
    
    # Step 2: Migrate to native provider if currently on nagaai
    if provider == PROVIDER_NAGAAI:
        native_provider = get_native_provider_for_model(new_friendly_name)
        if native_provider:
            # Verify the model exists for this provider
            model_info = MODELS.get(new_friendly_name)
            if model_info and native_provider in model_info.get("provider_names", {}):
                new_provider = native_provider
                reasons.append(f"migrated {provider} -> {native_provider}")
    
    # Check if anything changed
    if new_provider == provider and new_friendly_name == friendly_name:
        return None, "no migration needed"
    
    new_value = f"{new_provider}/{new_friendly_name}"
    reason = ", ".join(reasons)
    return new_value, reason


def migrate_expert_settings(dry_run: bool = True, verbose: bool = False) -> Dict:
    """
    Migrate all expert settings with model values.
    
    Args:
        dry_run: If True, don't actually update the database
        verbose: If True, print detailed information
        
    Returns:
        Dictionary with migration statistics
    """
    stats = {
        "total_checked": 0,
        "migrated": 0,
        "skipped": 0,
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
                stats["skipped"] += 1
                continue
            
            new_value, reason = migrate_model_value(old_value)
            
            if new_value is None:
                stats["skipped"] += 1
                if verbose:
                    print(f"  [SKIP] Expert {setting.instance_id} / {setting.key}: '{old_value}' ({reason})")
            else:
                stats["migrated"] += 1
                stats["changes"].append({
                    "type": "expert",
                    "instance_id": setting.instance_id,
                    "key": setting.key,
                    "old_value": old_value,
                    "new_value": new_value,
                    "reason": reason,
                })
                
                print(f"  [MIGRATE] Expert {setting.instance_id} / {setting.key}:")
                print(f"            '{old_value}' -> '{new_value}'")
                print(f"            Reason: {reason}")
                
                if not dry_run:
                    setting.value_str = new_value
        
        if not dry_run:
            session.commit()
    
    return stats


def migrate_app_settings(dry_run: bool = True, verbose: bool = False) -> Dict:
    """
    Migrate all app settings with model values.
    
    Args:
        dry_run: If True, don't actually update the database
        verbose: If True, print detailed information
        
    Returns:
        Dictionary with migration statistics
    """
    stats = {
        "total_checked": 0,
        "migrated": 0,
        "skipped": 0,
        "changes": [],
    }
    
    with get_db() as session:
        # Find all app settings with model-related keys
        settings = session.query(AppSetting).filter(
            AppSetting.key.in_(MODEL_SETTING_KEYS)
        ).all()
        
        for setting in settings:
            stats["total_checked"] += 1
            old_value = setting.value_str
            
            if not old_value:
                stats["skipped"] += 1
                continue
            
            new_value, reason = migrate_model_value(old_value)
            
            if new_value is None:
                stats["skipped"] += 1
                if verbose:
                    print(f"  [SKIP] AppSetting '{setting.key}': '{old_value}' ({reason})")
            else:
                stats["migrated"] += 1
                stats["changes"].append({
                    "type": "app",
                    "key": setting.key,
                    "old_value": old_value,
                    "new_value": new_value,
                    "reason": reason,
                })
                
                print(f"  [MIGRATE] AppSetting '{setting.key}':")
                print(f"            '{old_value}' -> '{new_value}'")
                print(f"            Reason: {reason}")
                
                if not dry_run:
                    setting.value_str = new_value
        
        if not dry_run:
            session.commit()
    
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Migrate model settings to native providers and upgrade GPT-5 to GPT-5.2"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply the migrations (default is dry-run)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show all settings, including those that don't need migration"
    )
    
    args = parser.parse_args()
    dry_run = not args.apply
    
    print("=" * 70)
    print("Model Settings Migration: Native Providers + GPT-5 -> GPT-5.2")
    print("=" * 70)
    print()
    
    if dry_run:
        print("🔍 DRY RUN MODE - No changes will be made")
        print("   Run with --apply to actually migrate settings")
    else:
        print("⚠️  APPLY MODE - Changes will be saved to database")
    print()
    
    print("Migrations to be performed:")
    print("  • nagaai/gpt* -> openai/gpt* (OpenAI native)")
    print("  • nagaai/grok* -> xai/grok* (xAI native)")
    print("  • nagaai/gemini* -> google/gemini* (Google native)")
    print("  • nagaai/deepseek* -> deepseek/deepseek* (DeepSeek native)")
    print("  • nagaai/kimi* -> moonshot/kimi* (Moonshot native)")
    print("  • gpt5/gpt5_mini/gpt5.1 -> gpt5.2/gpt5.2_mini")
    print()
    
    # Migrate Expert Settings
    print("-" * 70)
    print("Expert Settings:")
    print("-" * 70)
    expert_stats = migrate_expert_settings(dry_run=dry_run, verbose=args.verbose)
    
    # Migrate App Settings
    print()
    print("-" * 70)
    print("App Settings:")
    print("-" * 70)
    app_stats = migrate_app_settings(dry_run=dry_run, verbose=args.verbose)
    
    # Summary
    print()
    print("=" * 70)
    print("Summary:")
    print("=" * 70)
    
    total_checked = expert_stats["total_checked"] + app_stats["total_checked"]
    total_migrated = expert_stats["migrated"] + app_stats["migrated"]
    total_skipped = expert_stats["skipped"] + app_stats["skipped"]
    
    print(f"  Total settings checked: {total_checked}")
    print(f"  Settings migrated: {total_migrated}")
    print(f"  Settings skipped: {total_skipped}")
    print()
    
    if dry_run and total_migrated > 0:
        print("To apply these migrations, run:")
        print("  python migrate_to_native_providers.py --apply")
    elif not dry_run and total_migrated > 0:
        print("✅ Migrations applied successfully!")
    elif total_migrated == 0:
        print("✅ No migrations needed - all settings are already up to date!")


if __name__ == "__main__":
    main()
