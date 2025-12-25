"""
Text utility functions for the BA2 Trade Platform.

This module contains low-level text processing utilities that don't depend
on other platform modules to avoid circular imports.
"""


def extract_text_from_llm_response(content) -> str:
    """
    Extract plain text from LLM response content, handling various formats.
    
    Some LLM providers (especially Gemini) return content as a list of dictionaries
    with 'type' and 'text' keys: [{'type': 'text', 'text': 'actual content'}, ...]
    
    This function normalizes the content to a plain string for database storage.
    
    Args:
        content: The response.content from an LLM invoke call. Can be:
            - str: Plain text (returned as-is)
            - list of dicts: [{'type': 'text', 'text': '...'}, ...]
            - list of other: Converted to string with newlines
            
    Returns:
        str: Plain text content suitable for database storage
        
    Examples:
        >>> extract_text_from_llm_response("plain text")
        "plain text"
        
        >>> extract_text_from_llm_response([{'type': 'text', 'text': 'Summary'}])
        "Summary"
        
        >>> extract_text_from_llm_response([{'type': 'text', 'text': 'Part 1'}, {'type': 'text', 'text': 'Part 2'}])
        "Part 1\\nPart 2"
    """
    if isinstance(content, str):
        return content
    
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                # Handle Gemini format: {'type': 'text', 'text': 'content'}
                if 'text' in item:
                    text_parts.append(str(item['text']))
                else:
                    # Fallback: convert dict to string
                    text_parts.append(str(item))
            else:
                # Non-dict list items
                text_parts.append(str(item))
        
        return "\n".join(text_parts)
    
    # Fallback for any other type
    return str(content)
