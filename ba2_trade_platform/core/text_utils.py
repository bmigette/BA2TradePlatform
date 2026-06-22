"""Extract text from LLM responses, handling various content block types.

This module provides functionality to extract text from LLM response content,
handling various content block types including reasoning blocks and structured data.
"""


def extract_text_from_llm_response(content):
    """Extract text from LLM response content blocks.
    
    Handles GPT-5.x reasoning models which prepend reasoning blocks to content
    lists. Reasoning blocks must not leak into the extracted text content.
    Non-text blocks without a 'text' key are not stringified into output.
    
    Args:
        content: Plain string, list of strings, or list of content block dicts.
            - Strings are returned as-is or joined with newlines if in a list
            - Dicts with type='reasoning' are skipped entirely
            - Dicts with a 'text' key have that text extracted
            - Dicts without 'text' key are skipped
        
    Returns:
        str: Extracted text content joined with newlines.
             Empty string if only reasoning blocks exist.
    """
    if isinstance(content, str):
        return content
    
    if isinstance(content, list):
        text_parts = []
        
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                if item.get('type') == 'reasoning':
                    continue
                if 'text' in item:
                    text_parts.append(item['text'])
        
        return '\n'.join(text_parts)
    
    return str(content)
