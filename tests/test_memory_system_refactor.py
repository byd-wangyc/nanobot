from pathlib import Path

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.agent.tools.filesystem import EditFileTool, WriteFileTool
from nanobot.session.manager import Session


@pytest.mark.asyncio
async def test_write_file_blocks_memory_md_without_explicit_permission(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    tool = WriteFileTool(workspace=workspace)

    result = await tool.execute("memory/MEMORY.md", "# Memory\n- fact")

    assert "explicit user instruction" in result


@pytest.mark.asyncio
async def test_edit_file_allows_memory_md_with_explicit_permission(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    memory_file = workspace / "memory" / "MEMORY.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Memory\n- old", encoding="utf-8")
    tool = EditFileTool(workspace=workspace)
    tool.set_memory_write_permission(True)

    result = await tool.execute("memory/MEMORY.md", "- old", "- new")

    assert "Successfully edited" in result
    assert "- new" in memory_file.read_text(encoding="utf-8")


def test_search_history_returns_top_3_relevant_entries(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.history_file.write_text(
        "\n\n".join(
            [
                "[2026-03-01 10:00] 用户要求每天22:30生成热点新闻并上传到 GitHub daily 仓库。",
                "[2026-03-02 11:00] 用户喜欢小熊维尼。",
                "[2026-03-03 12:00] 用户要求上传统一使用 gh 命令，不要用 github skill。",
                "[2026-03-04 13:00] 用户想详细分析 VectifyAI PageIndex 的索引构建。",
            ]
        ),
        encoding="utf-8",
    )

    matches = store.search_history("帮我把热点新闻上传到 GitHub 仓库", top_k=3)

    assert len(matches) == 3
    assert "热点新闻" in matches[0]
    assert any("GitHub" in entry or "gh 命令" in entry for entry in matches[:2])


def test_session_history_uses_token_budget(tmp_path: Path) -> None:
    session = Session(key="cli:direct")
    for i in range(6):
        session.messages.append({"role": "user", "content": f"user message {i}"})
        session.messages.append({"role": "assistant", "content": ("assistant reply " + str(i) + " ") * 30})

    history = session.get_history(max_tokens=80, model="gpt-4o-mini")

    assert history
    assert len(history) < len(session.messages)
    assert history[0]["role"] == "user"
