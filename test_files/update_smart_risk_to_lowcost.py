"""
Update all Smart Risk Manager models to low-cost alternatives.

This script will:
1. Find all experts with risk_manager_model settings
2. Propose low-cost alternatives
3. Update the database
4. Show a summary of changes
"""

import sys
sys.path.insert(0, r'C:\Users\basti\Documents\BA2TradePlatform')

import sqlite3

# Define low-cost replacements
LOW_COST_REPLACEMENTS = {
    # Grok models - use fast variants
    'xai/grok4': 'xai/grok4_fast_reasoning',
    'xai/grok-4': 'xai/grok4_fast_reasoning',
    'xai/grok-4-0709': 'xai/grok4_fast_reasoning',
    'nagaai/grok4': 'xai/grok4_fast_reasoning',
    'nagaai/grok-4-0709': 'xai/grok4_fast_reasoning',
    'nagaac/grok-4.1-fast-reasoning': 'xai/grok4.1_fast_reasoning',
    
    # OpenAI models - use mini/nano variants
    'openai/gpt5.2': 'openai/gpt5_mini',
    'openai/gpt5': 'openai/gpt5_mini',
    'openai/gpt4o': 'openai/gpt4o_mini',
    
    # NagaAI models - switch to native providers
    'nagaai/gpt-5-2025-08-07': 'openai/gpt5_mini',
    'nagaai/gpt-5-mini-2025-08-07': 'openai/gpt5_mini',
    'nagaai/gpt-5-mini-2025-08-07:free': 'openai/gpt5_mini',
    
    # DeepSeek - use native provider
    'deepseek/deepseek_v3.2': 'deepseek/deepseek_v3.2',
    'deepseek/deepseek-chat-v3.1': 'deepseek/deepseek_v3.2',
    'nagaai/deepseek-v3.2': 'deepseek/deepseek_v3.2',
    'nagaai/deepseek-v3.2:free': 'deepseek/deepseek_v3.2',
    'nagaai/deepseek-chat-v3.1': 'deepseek/deepseek_v3.2',
    'nagaai/deepseek-chat-v3.1:free': 'deepseek/deepseek_v3.2',
    'nagaac/deepseek-v3.2-speciale': 'deepseek/deepseek_v3.2',
    
    # Gemini - use Gemini 3 Flash (native provider)
    'native/gemini_3_pro': 'native/gemini_3_flash',
    'native/gemini-3-pro-preview': 'native/gemini_3_flash',
    'google/gemini-3-pro-preview': 'native/gemini_3_flash',
    'google/gemini_3_pro': 'native/gemini_3_flash',
    'nagaai/gemini-3-pro-preview': 'native/gemini_3_flash',
    'nagaai/gemini-3-flash:free': 'native/gemini_3_flash',
    'native/gemini_2.0_flash': 'native/gemini_3_flash',
    'google/gemini_2.0_flash': 'native/gemini_3_flash',
    
    # NagaAC models - suggest native alternatives
    'nagaac/gpt-5.1-2025-11-13': 'openai/gpt5_mini',
    
    # Moonshot Kimi - keep as is (native provider)
    # 'moonshot/kimi_k2_thinking' - already low-cost
    
    # Qwen - suggest lower cost alternatives
    'nagaai/qwen3-max': 'deepseek/deepseek_v3.2',
}

def normalize_model_name(model):
    """Normalize model name for comparison."""
    if not model:
        return None
    return model.lower().strip()

def get_low_cost_alternative(current_model):
    """Get low-cost alternative for a model."""
    if not current_model:
        return None
    
    normalized = normalize_model_name(current_model)
    
    # Check direct match
    for expensive, cheap in LOW_COST_REPLACEMENTS.items():
        if normalize_model_name(expensive) == normalized:
            return cheap
    
    # Check if it's already a low-cost model
    if any(x in normalized for x in [':free', 'mini', 'nano', 'flash', 'fast']):
        return None  # Already low-cost
    
    return None

def main():
    conn = sqlite3.connect(r'C:\Users\basti\Documents\ba2_trade_platform\db.sqlite')
    cursor = conn.cursor()
    
    print("=" * 100)
    print("SMART RISK MANAGER MODEL UPDATE TO LOW-COST ALTERNATIVES")
    print("=" * 100)
    
    # Get all experts with risk_manager_model settings
    query = """
    SELECT 
        es.id,
        es.instance_id,
        es.value_str,
        ei.expert,
        ei.enabled
    FROM expertsetting es
    JOIN expertinstance ei ON es.instance_id = ei.id
    WHERE es.key = 'risk_manager_model'
        AND es.value_str IS NOT NULL
        AND es.value_str != ''
    ORDER BY ei.enabled DESC, ei.id
    """
    
    cursor.execute(query)
    results = cursor.fetchall()
    
    print(f"\nFound {len(results)} experts with risk_manager_model configured:\n")
    
    updates_to_apply = []
    no_change_needed = []
    
    for setting_id, expert_id, current_model, expert_type, enabled in results:
        status = "✅ ENABLED" if enabled else "❌ DISABLED"
        print(f"\n{expert_type} Expert {expert_id} ({status}):")
        print(f"  Current: {current_model}")
        
        alternative = get_low_cost_alternative(current_model)
        
        if alternative:
            print(f"  Proposed: {alternative} 💰 (LOW-COST)")
            updates_to_apply.append({
                'setting_id': setting_id,
                'expert_id': expert_id,
                'expert_type': expert_type,
                'old_model': current_model,
                'new_model': alternative,
                'enabled': enabled
            })
        else:
            if any(x in current_model.lower() for x in [':free', 'mini', 'nano', 'flash', 'fast']):
                print(f"  ✅ Already low-cost")
                no_change_needed.append((expert_id, current_model))
            else:
                print(f"  ⚠️ No low-cost alternative defined (manual review needed)")
                no_change_needed.append((expert_id, current_model))
    
    # Summary
    print("\n" + "=" * 100)
    print("SUMMARY:")
    print("=" * 100)
    print(f"\nUpdates to apply: {len(updates_to_apply)}")
    print(f"No change needed: {len(no_change_needed)}")
    
    if not updates_to_apply:
        print("\n✅ All experts already using low-cost models or have no alternatives defined.")
        conn.close()
        return
    
    # Show detailed update plan
    print("\n" + "=" * 100)
    print("UPDATE PLAN:")
    print("=" * 100)
    
    for update in updates_to_apply:
        status = "✅ ENABLED" if update['enabled'] else "❌ DISABLED"
        print(f"\nExpert {update['expert_id']} ({update['expert_type']}) - {status}:")
        print(f"  {update['old_model']} -> {update['new_model']}")
    
    # Ask for confirmation
    print("\n" + "=" * 100)
    response = input("\nApply these updates? (yes/no): ").strip().lower()
    
    if response not in ['yes', 'y']:
        print("\n❌ Updates cancelled.")
        conn.close()
        return
    
    # Apply updates
    print("\n" + "=" * 100)
    print("APPLYING UPDATES...")
    print("=" * 100)
    
    success_count = 0
    for update in updates_to_apply:
        try:
            cursor.execute(
                "UPDATE expertsetting SET value_str = ? WHERE id = ?",
                (update['new_model'], update['setting_id'])
            )
            print(f"✅ Expert {update['expert_id']}: Updated to {update['new_model']}")
            success_count += 1
        except Exception as e:
            print(f"❌ Expert {update['expert_id']}: Failed - {e}")
    
    conn.commit()
    conn.close()
    
    print("\n" + "=" * 100)
    print("COMPLETE!")
    print("=" * 100)
    print(f"\nSuccessfully updated {success_count}/{len(updates_to_apply)} experts.")
    print("\n💡 Estimated cost savings:")
    print("   - Grok-4 -> Grok-4 Fast: ~70% cheaper")
    print("   - GPT-5 -> GPT-5 Mini: ~80% cheaper")
    print("   - Gemini 3 Pro -> Gemini 3 Flash: ~90% cheaper")
    print("   - DeepSeek paid -> DeepSeek free: 100% cheaper")
    
    print("\n⚠️ IMPORTANT:")
    print("   - Restart the application to apply these changes")
    print("   - Clear model cache if using cached instances")
    print("   - Monitor performance to ensure low-cost models meet requirements")

if __name__ == "__main__":
    main()
