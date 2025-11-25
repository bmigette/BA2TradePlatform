"""
Gemini compatibility patch for LangChain ToolMessage name field and thought signatures.

This module patches LangChain's _convert_message_to_dict function to ensure:
1. ToolMessage.name is properly included in the API payload
2. Thought signatures from Gemini responses are preserved in conversation history

Issue 1: Gemini requires the 'name' field in function_response messages, but
LangChain's converter doesn't include it even when set on ToolMessage objects.

Issue 2: Gemini 3 Pro requires thought_signature preservation during function calling.
When using reasoning_effort parameter, Gemini returns thought signatures in tool_calls
that must be passed back in subsequent requests or you get a 400 error.

Solution: Monkey-patch the converter to:
- Add the name field for tool messages
- Preserve thought_signature from AIMessage tool_calls back to the API

See: https://ai.google.dev/gemini-api/docs/thought-signatures
"""

from langchain_core.messages import ToolMessage, AIMessage
from ba2_trade_platform.logger import logger


def apply_gemini_toolmessage_patch():
    """
    Apply monkey-patch to LangChain's message converter to fix Gemini compatibility.
    
    This patches the _convert_message_to_dict function in langchain_openai to ensure:
    1. ToolMessage.name is included in the API payload
    2. Thought signatures from tool_calls are preserved
    
    Returns:
        bool: True if patch was applied successfully, False otherwise
    """
    try:
        from langchain_openai.chat_models.base import _convert_message_to_dict
        import langchain_openai.chat_models.base
        
        # Store original function
        original_convert = _convert_message_to_dict
        
        def patched_convert_message_to_dict(message, max_tokens=None):
            """
            Patched version that ensures:
            1. ToolMessage includes name field
            2. AIMessage with tool_calls has thought_signature (real or dummy)
            
            This is required for Gemini 3 Pro compatibility via OpenAI client.
            """
            # Call original converter
            result = original_convert(message, max_tokens) if max_tokens is not None else original_convert(message)
            
            # PATCH 1: Add name field for ToolMessages
            if isinstance(message, ToolMessage) and hasattr(message, 'name') and message.name:
                if result.get('role') == 'tool':
                    result['name'] = message.name
                    logger.debug(f"[GEMINI_PATCH] Added name '{message.name}' to ToolMessage dict")
            
            # PATCH 2: Ensure thought_signature in AIMessage tool_calls
            # Gemini 3 Pro REQUIRES thought signatures on all function calls in conversation history
            # Per docs: https://ai.google.dev/gemini-api/docs/thought-signatures
            if isinstance(message, AIMessage) and 'tool_calls' in result and isinstance(result['tool_calls'], list) and len(result['tool_calls']) > 0:
                # Check if we already have thought signatures from Gemini response
                has_signatures = False
                
                # Try multiple places where signatures might be stored
                # 1. Check additional_kwargs (OpenAI format)
                if hasattr(message, 'additional_kwargs') and message.additional_kwargs:
                    tool_calls_raw = message.additional_kwargs.get('tool_calls', [])
                    
                    for idx, raw_tool_call in enumerate(tool_calls_raw):
                        if idx < len(result['tool_calls']):
                            # Check for Google thought signature
                            extra_content = raw_tool_call.get('extra_content', {})
                            google_data = extra_content.get('google', {})
                            thought_sig = google_data.get('thought_signature')
                            
                            if thought_sig:
                                has_signatures = True
                                # Preserve the thought signature in the converted dict
                                if 'extra_content' not in result['tool_calls'][idx]:
                                    result['tool_calls'][idx]['extra_content'] = {}
                                if 'google' not in result['tool_calls'][idx]['extra_content']:
                                    result['tool_calls'][idx]['extra_content']['google'] = {}
                                result['tool_calls'][idx]['extra_content']['google']['thought_signature'] = thought_sig
                                logger.debug(f"[GEMINI_PATCH] Preserved real thought_signature for tool_call {idx} from additional_kwargs")
                
                # 2. Check if tool_calls attribute has extra_content directly
                if not has_signatures and hasattr(message, 'tool_calls') and message.tool_calls:
                    for idx, tool_call in enumerate(message.tool_calls):
                        if idx < len(result['tool_calls']) and isinstance(tool_call, dict):
                            extra = tool_call.get('extra_content', {})
                            if extra:
                                google_data = extra.get('google', {})
                                thought_sig = google_data.get('thought_signature')
                                if thought_sig:
                                    has_signatures = True
                                    if 'extra_content' not in result['tool_calls'][idx]:
                                        result['tool_calls'][idx]['extra_content'] = {}
                                    if 'google' not in result['tool_calls'][idx]['extra_content']:
                                        result['tool_calls'][idx]['extra_content']['google'] = {}
                                    result['tool_calls'][idx]['extra_content']['google']['thought_signature'] = thought_sig
                                    logger.debug(f"[GEMINI_PATCH] Preserved real thought_signature for tool_call {idx} from tool_calls attribute")
                
                # If no signatures found, add dummy signature to skip validation
                # Per FAQ: use "skip_thought_signature_validator" or "context_engineering_is_the_way_to_go"
                # for transferred/injected function calls
                # IMPORTANT: Only add to FIRST tool_call (per Gemini parallel function calling rules)
                if not has_signatures:
                    # Add dummy signature to first tool_call only (required by Gemini 3)
                    if 'extra_content' not in result['tool_calls'][0]:
                        result['tool_calls'][0]['extra_content'] = {}
                    if 'google' not in result['tool_calls'][0]['extra_content']:
                        result['tool_calls'][0]['extra_content']['google'] = {}
                    # Use the alternative dummy signature
                    result['tool_calls'][0]['extra_content']['google']['thought_signature'] = "context_engineering_is_the_way_to_go"
                    
                    # Log with message role for debugging
                    msg_role = result.get('role', 'unknown')
                    logger.debug(f"[GEMINI_PATCH] Added dummy thought_signature to {msg_role} message with {len(result['tool_calls'])} tool_call(s) (signature on first only)")
            
            return result
        
        # Apply the patch
        langchain_openai.chat_models.base._convert_message_to_dict = patched_convert_message_to_dict
        
        logger.info("Successfully applied Gemini ToolMessage and thought signature compatibility patch")
        return True
        
    except ImportError as e:
        logger.warning(f"Could not apply Gemini patch - langchain_openai not available: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to apply Gemini ToolMessage patch: {e}", exc_info=True)
        return False


def is_patch_applied() -> bool:
    """
    Check if the Gemini ToolMessage patch has been applied.
    
    Returns:
        bool: True if patch is active, False otherwise
    """
    try:
        from langchain_openai.chat_models.base import _convert_message_to_dict
        return _convert_message_to_dict.__name__ == 'patched_convert_message_to_dict'
    except:
        return False
