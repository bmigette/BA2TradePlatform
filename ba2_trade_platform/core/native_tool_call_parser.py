"""
Native Tool Call Parser for Kimi/DeepSeek Models

This module provides utility functions to parse native tool call formats from models
like Kimi-K2 and DeepSeek when the API provider (e.g., NagaAI) does not properly
parse them into OpenAI-compatible tool_calls format.

This is a WORKAROUND that should be removed once the API provider fixes their
tool call parsing.

References:
- Kimi K2 Tool Calling: https://huggingface.co/moonshotai/Kimi-K2-Instruct/blob/main/docs/tool_call_guidance.md

Native Format (Kimi K2):
    <|tool_calls_section_begin|>
    <|tool_call_begin|>functions.get_weather:0<|tool_call_argument_begin|>{"city": "Beijing"}<|tool_call_end|>
    <|tool_calls_section_end|>

Native Format (DeepSeek):
    <|tool▁calls▁begin|>
    <|tool▁call▁begin|>function<|tool▁sep|>get_weather
    {"city": "Beijing"}
    <|tool▁call▁end|>
    <|tool▁calls▁end|>
"""

import re
import json
import uuid
from typing import List, Dict, Any, Optional
from langchain_core.messages import AIMessage

from ..logger import logger


# Models that require native tool call parsing
NATIVE_TOOL_CALL_MODELS = ["kimi", "deepseek"]


def requires_native_tool_parsing(model_name: str) -> bool:
    """
    Check if a model requires native tool call parsing.
    
    Args:
        model_name: The model name string
        
    Returns:
        True if the model requires native tool call parsing
    """
    if not model_name:
        return False
    model_lower = model_name.lower()
    return any(m in model_lower for m in NATIVE_TOOL_CALL_MODELS)


def extract_kimi_tool_calls(content: str) -> List[Dict[str, Any]]:
    """
    Extract tool calls from Kimi K2 native format.
    
    Format:
        <|tool_calls_section_begin|>
        <|tool_call_begin|>functions.get_weather:0<|tool_call_argument_begin|>{"city": "Beijing"}<|tool_call_end|>
        <|tool_calls_section_end|>
    
    Args:
        content: The raw content string from the model
        
    Returns:
        List of tool call dicts in OpenAI format
    """
    if '<|tool_calls_section_begin|>' not in content:
        return []
    
    tool_calls = []
    
    # Extract the tool calls section
    pattern = r"<\|tool_calls_section_begin\|>(.*?)<\|tool_calls_section_end\|>"
    tool_calls_sections = re.findall(pattern, content, re.DOTALL)
    
    if not tool_calls_sections:
        return []
    
    # Extract individual tool calls
    # Format: <|tool_call_begin|>functions.func_name:idx<|tool_call_argument_begin|>{...}<|tool_call_end|>
    func_call_pattern = r"<\|tool_call_begin\|>\s*(?P<tool_call_id>[\w\.]+:\d+)\s*<\|tool_call_argument_begin\|>\s*(?P<function_arguments>.*?)\s*<\|tool_call_end\|>"
    
    for match in re.findall(func_call_pattern, tool_calls_sections[0], re.DOTALL):
        function_id, function_args = match
        # function_id format: functions.get_weather:0
        # Extract function name from functions.{name}:{idx}
        parts = function_id.split('.')
        if len(parts) >= 2:
            name_and_idx = parts[1]
            function_name = name_and_idx.split(':')[0]
        else:
            function_name = function_id
        
        # Try to parse arguments as JSON to validate
        try:
            # Validate JSON
            json.loads(function_args.strip())
            args_str = function_args.strip()
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse Kimi tool call arguments as JSON: {function_args[:100]}")
            args_str = function_args.strip()
        
        tool_calls.append({
            "id": f"kimi_{function_id.replace('.', '_').replace(':', '_')}",
            "type": "function",
            "name": function_name,
            "args": json.loads(args_str) if args_str.startswith('{') else {}
        })
    
    return tool_calls


def extract_deepseek_tool_calls(content: str) -> List[Dict[str, Any]]:
    """
    Extract tool calls from DeepSeek native format.
    
    Format:
        <|tool▁calls▁begin|>
        <|tool▁call▁begin|>function<|tool▁sep|>get_weather
        {"city": "Beijing"}
        <|tool▁call▁end|>
        <|tool▁calls▁end|>
    
    Args:
        content: The raw content string from the model
        
    Returns:
        List of tool call dicts in OpenAI format
    """
    # DeepSeek uses Unicode characters that look like pipes
    if '<|tool▁calls▁begin|>' not in content and '<|tool▁calls▁begin|>' not in content:
        return []
    
    tool_calls = []
    
    # Normalize potential unicode variations
    normalized = content.replace('|', '|').replace('▁', '_')
    
    # Extract the tool calls section
    pattern = r"<\|tool_calls_begin\|>(.*?)<\|tool_calls_end\|>"
    tool_calls_sections = re.findall(pattern, normalized, re.DOTALL)
    
    if not tool_calls_sections:
        return []
    
    # Extract individual tool calls
    # Format: <|tool_call_begin|>function<|tool_sep|>func_name\n{...}<|tool_call_end|>
    func_call_pattern = r"<\|tool_call_begin\|>\s*function\s*<\|tool_sep\|>\s*(\w+)\s*\n?\s*(\{.*?\})\s*<\|tool_call_end\|>"
    
    for match in re.findall(func_call_pattern, tool_calls_sections[0], re.DOTALL):
        function_name, function_args = match
        
        # Try to parse arguments as JSON to validate
        try:
            args_dict = json.loads(function_args.strip())
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse DeepSeek tool call arguments as JSON: {function_args[:100]}")
            args_dict = {}
        
        tool_calls.append({
            "id": f"deepseek_{function_name}_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "name": function_name,
            "args": args_dict
        })
    
    return tool_calls


def extract_native_tool_calls(content: str, model_name: str = "") -> List[Dict[str, Any]]:
    """
    Extract tool calls from native format based on model type.
    
    Tries both Kimi and DeepSeek formats.
    
    Args:
        content: The raw content string from the model
        model_name: Optional model name to prioritize parsing (not strictly required)
        
    Returns:
        List of tool call dicts in OpenAI format
    """
    if not content:
        return []
    
    # Try Kimi format first
    tool_calls = extract_kimi_tool_calls(content)
    if tool_calls:
        logger.debug(f"Extracted {len(tool_calls)} tool calls using Kimi parser")
        return tool_calls
    
    # Try DeepSeek format
    tool_calls = extract_deepseek_tool_calls(content)
    if tool_calls:
        logger.debug(f"Extracted {len(tool_calls)} tool calls using DeepSeek parser")
        return tool_calls
    
    return []


def process_response_for_native_tool_calls(response: AIMessage, model_name: str) -> AIMessage:
    """
    Process an AIMessage response to extract native tool calls if needed.
    
    If the model is a Kimi/DeepSeek model AND the response has content with native
    tool call markers BUT no parsed tool_calls, this function will parse the native
    format and update the response.
    
    This is the main entry point for tool call parsing workaround.
    
    Args:
        response: The AIMessage response from the LLM
        model_name: The model name string
        
    Returns:
        The original response if no parsing needed, or a new AIMessage with
        extracted tool_calls if native format was detected
    """
    # Skip if model doesn't need native parsing
    if not requires_native_tool_parsing(model_name):
        return response
    
    # Skip if response already has tool_calls
    if response.tool_calls and len(response.tool_calls) > 0:
        return response
    
    # Skip if no content to parse
    if not response.content:
        return response
    
    # Check if content contains native tool call markers
    content = response.content
    has_kimi_markers = '<|tool_calls_section_begin|>' in content
    has_deepseek_markers = '<|tool▁calls▁begin|>' in content or '<|tool▁calls▁begin|>' in content
    
    if not has_kimi_markers and not has_deepseek_markers:
        return response
    
    # Extract native tool calls
    extracted_tool_calls = extract_native_tool_calls(content, model_name)
    
    if not extracted_tool_calls:
        logger.warning(f"Found native tool call markers but failed to extract tool calls from: {content[:200]}...")
        return response
    
    logger.info(f"Native tool call parser: Extracted {len(extracted_tool_calls)} tool calls from {model_name} response")
    
    # Clean the content by removing tool call markers
    clean_content = content
    if has_kimi_markers:
        clean_content = re.sub(r"<\|tool_calls_section_begin\|>.*?<\|tool_calls_section_end\|>", "", clean_content, flags=re.DOTALL)
    if has_deepseek_markers:
        # Normalize and remove
        normalized = clean_content.replace('|', '|').replace('▁', '_')
        clean_content = re.sub(r"<\|tool_calls_begin\|>.*?<\|tool_calls_end\|>", "", normalized, flags=re.DOTALL)
    clean_content = clean_content.strip()
    
    # Create new AIMessage with extracted tool calls
    new_response = AIMessage(
        content=clean_content,
        tool_calls=extracted_tool_calls,
        additional_kwargs=response.additional_kwargs.copy() if response.additional_kwargs else {},
        response_metadata=response.response_metadata.copy() if response.response_metadata else {}
    )
    
    return new_response


def wrap_llm_response_with_native_parsing(response: AIMessage, model_name: str) -> AIMessage:
    """
    Convenience wrapper for process_response_for_native_tool_calls.
    
    Use this as a post-processing step after invoking the LLM:
    
        response = llm_with_tools.invoke(messages)
        response = wrap_llm_response_with_native_parsing(response, model_name)
    
    Args:
        response: The AIMessage response from the LLM
        model_name: The model name string
        
    Returns:
        Processed AIMessage with tool_calls extracted if applicable
    """
    return process_response_for_native_tool_calls(response, model_name)
