def extract_text_from_llm_response(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "reasoning":
                    continue
                if "text" in item:
                    text_parts.append(item["text"])
        return "\n".join(text_parts)
    return str(content)
