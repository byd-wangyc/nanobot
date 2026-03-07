"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.utils.helpers import ensure_dir
from nanobot.utils.tokens import count_messages_tokens

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the history consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                },
                "required": ["history_entry"],
            },
        },
    }
]


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    def search_history(
        self,
        query: str,
        *,
        top_k: int = 3,
        max_entry_chars: int = 1200,
    ) -> list[str]:
        """Return the most relevant HISTORY.md entries for the current query."""
        if top_k <= 0 or not query or not self.history_file.exists():
            return []

        raw = self.history_file.read_text(encoding="utf-8").strip()
        if not raw:
            return []

        entries = [chunk.strip() for chunk in re.split(r"\n\s*\n", raw) if chunk.strip()]
        if not entries:
            return []

        query_terms = _extract_similarity_terms(query)
        scored: list[tuple[float, int, str]] = []
        for idx, entry in enumerate(entries):
            score = _history_similarity_score(query, query_terms, entry)
            scored.append((score, idx, entry[:max_entry_chars]))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        top_entries = [entry for _, _, entry in scored[:top_k] if entry]
        return top_entries

    @staticmethod
    def user_explicitly_requested_memory_write(text: str) -> bool:
        """Heuristic gate for MEMORY.md writes in the current turn."""
        if not text:
            return False

        lower = text.lower()
        explicit_phrases = (
            "memory.md",
            "save to memory",
            "write to memory",
            "store in memory",
            "remember this",
            "记到长期记忆",
            "写入长期记忆",
            "写到长期记忆",
            "写入memory",
            "更新memory",
            "记住这件事",
            "记住这个",
            "记到memory",
            "存到记忆",
        )
        return any(phrase in lower or phrase in text for phrase in explicit_phrases)

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        keep_tokens: int = 12000,
    ) -> bool:
        """Consolidate old messages into HISTORY.md via LLM tool call.

        Returns True on success (including no-op), False on failure.
        """
        if archive_all:
            old_messages = session.messages
            keep_start = len(session.messages)
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_start = _find_keep_start_index(session.messages, session.last_consolidated, keep_tokens, model)
            if keep_start <= session.last_consolidated:
                return True
            old_messages = session.messages[session.last_consolidated:keep_start]
            if not old_messages:
                return True
            logger.info(
                "Memory consolidation: {} to consolidate, keeping tail from index {}",
                len(old_messages), keep_start,
            )

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")

        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

Only write a new HISTORY.md entry. Do not update long-term memory.

## Conversation to Archive
{chr(10).join(lines)}"""

        try:
            response = await provider.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = response.tool_calls[0].arguments
            # Some providers return arguments as a JSON string instead of dict
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                logger.warning("Memory consolidation: unexpected arguments type {}", type(args).__name__)
                return False

            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                self.append_history(entry)

            session.last_consolidated = keep_start
            logger.info(
                "Memory consolidation done: {} messages, last_consolidated={}",
                len(session.messages), session.last_consolidated,
            )
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False


def _find_keep_start_index(
    messages: list[dict],
    last_consolidated: int,
    keep_tokens: int,
    model: str,
) -> int:
    """Find the earliest message index that keeps the recent tail within token budget."""
    if keep_tokens <= 0:
        return len(messages)

    selected_rev: list[dict] = []
    total = 0
    for message in reversed(messages[last_consolidated:]):
        cost = count_messages_tokens([_to_chat_message(message)], model=model)
        if selected_rev and total + cost > keep_tokens:
            break
        selected_rev.append(message)
        total += cost

    return len(messages) - len(selected_rev)


def _to_chat_message(message: dict) -> dict:
    """Convert a stored session message back into chat-message shape for token counting."""
    entry: dict = {"role": message.get("role"), "content": message.get("content", "")}
    for key in ("tool_calls", "tool_call_id", "name", "reasoning_content", "thinking_blocks"):
        if key in message:
            entry[key] = message[key]
    return entry


def _extract_similarity_terms(text: str) -> set[str]:
    """Extract lightweight similarity terms for mixed Chinese/English text."""
    terms: set[str] = set()
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", normalized):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            terms.add(token)
            if len(token) > 1:
                terms.update(token[i:i + 2] for i in range(len(token) - 1))
        else:
            terms.add(token)
    return {term for term in terms if term}


def _history_similarity_score(query: str, query_terms: set[str], entry: str) -> float:
    """Score a HISTORY.md entry against the current query."""
    entry_normalized = entry.lower()
    if not query_terms:
        return 0.0

    entry_terms = _extract_similarity_terms(entry)
    overlap = len(query_terms & entry_terms) / len(query_terms)
    substring_hits = sum(1 for term in query_terms if len(term) > 1 and term in entry_normalized)
    exact_query_bonus = 1.0 if query.strip() and query.strip().lower() in entry_normalized else 0.0
    return overlap + (substring_hits / max(len(query_terms), 1)) + exact_query_bonus
