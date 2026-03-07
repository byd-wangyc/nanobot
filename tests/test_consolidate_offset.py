from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import MemoryStore
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import Session, SessionManager


def test_last_consolidated_persists(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    session = Session(key="cli:test")
    session.messages = [{"role": "user", "content": "hello"}]
    session.last_consolidated = 1

    manager.save(session)
    loaded = manager.get_or_create("cli:test")

    assert loaded.last_consolidated == 1


def test_get_history_respects_token_budget_and_turn_boundary() -> None:
    session = Session(key="cli:test")
    for i in range(5):
        session.messages.append({"role": "user", "content": f"user message {i}"})
        session.messages.append({"role": "assistant", "content": ("assistant reply " + str(i) + " ") * 25})

    history = session.get_history(max_tokens=80, model="gpt-4o-mini")

    assert history
    assert history[0]["role"] == "user"
    assert len(history) < len(session.messages)


@pytest.mark.asyncio
async def test_consolidate_archives_history_only(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    provider = AsyncMock()
    provider.chat = AsyncMock(
        return_value=LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="call_1",
                    name="save_memory",
                    arguments={"history_entry": "[2026-03-07 12:00] archived summary"},
                )
            ],
        )
    )
    session = Session(key="cli:test")
    session.messages = [
        {"role": "user", "content": "older message", "timestamp": "2026-03-07T12:00:00"},
        {"role": "assistant", "content": "older reply", "timestamp": "2026-03-07T12:00:01"},
        {"role": "user", "content": "recent message", "timestamp": "2026-03-07T12:00:02"},
    ]

    ok = await store.consolidate(session, provider, "gpt-4o-mini", keep_tokens=20)

    assert ok is True
    assert "archived summary" in store.history_file.read_text(encoding="utf-8")
    assert not store.memory_file.exists()
    assert session.last_consolidated > 0


@pytest.mark.asyncio
async def test_process_message_consolidates_when_prompt_exceeds_token_budget(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=32,
        max_tokens=8,
        recent_history_tokens=16,
    )

    session = loop.sessions.get_or_create("cli:test")
    for i in range(8):
        session.add_message("user", f"message {i}")
        session.add_message("assistant", ("reply " + str(i) + " ") * 20)
    loop.sessions.save(session)

    consolidate_calls = 0

    async def _fake_consolidate(sess, archive_all: bool = False) -> bool:
        nonlocal consolidate_calls
        consolidate_calls += 1
        sess.last_consolidated = max(sess.last_consolidated, len(sess.messages) - 2)
        return True

    loop._consolidate_memory = _fake_consolidate  # type: ignore[method-assign]

    msg = InboundMessage(channel="cli", sender_id="user", chat_id="test", content="hello")
    await loop._process_message(msg)

    assert consolidate_calls >= 1
    assert session.last_consolidated > 0


@pytest.mark.asyncio
async def test_new_does_not_clear_session_when_archive_fails(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    session = loop.sessions.get_or_create("cli:test")
    for i in range(3):
        session.add_message("user", f"msg{i}")
        session.add_message("assistant", f"resp{i}")
    loop.sessions.save(session)

    async def _failing_consolidate(sess, archive_all: bool = False) -> bool:
        return not archive_all

    loop._consolidate_memory = _failing_consolidate  # type: ignore[method-assign]

    response = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="test", content="/new")
    )

    assert response is not None
    assert "failed" in response.content.lower()
    assert loop.sessions.get_or_create("cli:test").messages
