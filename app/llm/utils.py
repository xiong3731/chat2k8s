import logging
import tiktoken
from typing import List
from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)

def count_tokens(messages: List[BaseMessage]) -> int:
    """
    使用 tiktoken 进行 Token 计数，支持多模态消息。
    """
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        encoding = tiktoken.get_encoding("gpt2") 
        
    num_tokens = 0
    for msg in messages:
        if isinstance(msg.content, str):
            num_tokens += 4 + len(encoding.encode(msg.content))
        elif isinstance(msg.content, list):
            for part in msg.content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        num_tokens += len(encoding.encode(part.get("text", "")))
                    elif part.get("type") == "image_url":
                        num_tokens += 1000
                else:
                    num_tokens += len(encoding.encode(str(part)))
            num_tokens += 4
        else:
            num_tokens += 4 + len(encoding.encode(str(msg.content)))
    return num_tokens
