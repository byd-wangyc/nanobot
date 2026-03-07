"""Token counting helpers with LiteLLM fallback support."""

from __future__ import annotations

import json
import re
from typing import Any

try:
    from litellm import token_counter as litellm_token_counter
except Exception:  # pragma: no cover - optional import guard
    litellm_token_counter = None


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[^\s]")


def count_text_tokens(text: str, model: str | None = None) -> int:
    """Count tokens in plain text with LiteLLM, falling back to a heuristic."""
    if not text:
        return 0
    if litellm_token_counter:
        try:
            return int(litellm_token_counter(model=model or "", text=text))
        except Exception:
            pass
    return _fallback_text_tokens(text)


def count_messages_tokens(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Count tokens for chat messages and optional tool schemas."""
    if not messages:
        return 0
    if litellm_token_counter:
        try:
            return int(litellm_token_counter(model=model or "", messages=messages, tools=tools))
        except Exception:
            pass

    total = 0
    for message in messages:
        total += 4  # Rough per-message wrapper overhead.
        total += _fallback_text_tokens(_serialize_message(message))
    if tools:
        total += _fallback_text_tokens(json.dumps(tools, ensure_ascii=False, sort_keys=True))
    return total


def _serialize_message(message: dict[str, Any]) -> str:
    """Serialize a chat message for heuristic token counting."""
    content = message.get("content")
    if isinstance(content, str):
        base = content
    else:
        base = json.dumps(content, ensure_ascii=False, sort_keys=True)
    extras = []
    for key in ("tool_calls", "tool_call_id", "name", "reasoning_content", "thinking_blocks"):
        if key in message:
            extras.append(json.dumps(message[key], ensure_ascii=False, sort_keys=True))
    return "\n".join([message.get("role", ""), base, *extras])


def _fallback_text_tokens(text: str) -> int:
    """Approximate tokens when provider-specific counting is unavailable."""
    cjk_chars = len(_CJK_RE.findall(text))
    non_cjk = _CJK_RE.sub(" ", text)
    other_units = len(_WORD_RE.findall(non_cjk))
    return max(1, cjk_chars + other_units)
