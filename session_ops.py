"""会话文件读写与操作核心。"""

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '--', name)


class SessionOperator:
    """会话操作核心类 — 读写会话文件、解析消息结构。"""

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        self.sessions_root = workspace_dir / "sessions"
        self.backup_dir = workspace_dir / "sessions_backup"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    # ── 文件路径与 I/O ──────────────────────────────────

    def _get_session_file_path(
        self,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> Path:
        """生成会话文件路径：{sessions_root}/{channel}/{user_id}_{sanitized_id}.json"""
        safe_id = sanitize_filename(session_id)
        return self.sessions_root / channel / f"{user_id}_{safe_id}.json"

    async def _load_state(self, session_id: str, user_id: str, channel: str) -> Dict:
        file_path = self._get_session_file_path(session_id, user_id, channel)
        if not file_path.exists():
            raise FileNotFoundError(f"会话文件不存在: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def _save_state(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        data: Dict,
    ):
        """保存状态，旧文件自动备份。"""
        file_path = self._get_session_file_path(session_id, user_id, channel)
        if file_path.exists():
            backup_name = f"{session_id}_{datetime.now():%Y%m%d_%H%M%S}.json"
            shutil.copy2(file_path, self.backup_dir / backup_name)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # ── 消息操作 ────────────────────────────────────────

    async def get_messages(self, session_id: str, user_id: str, channel: str) -> List[Any]:
        """获取会话消息列表。

        实际消息存储在 state.agent.memory.content 中，
        每条消息格式为 [msg_dict, extra_list]。
        """
        state = await self._load_state(session_id, user_id, channel)
        messages = state.get("agent", {}).get("memory", {}).get("content", [])
        logger.debug("获取到 %d 条消息", len(messages))
        return messages

    def _get_role(self, msg: Any) -> str:
        """从消息中提取 role 字段。消息格式为 [msg_dict, ...]"""
        if isinstance(msg, list) and len(msg) > 0 and isinstance(msg[0], dict):
            return msg[0].get("role", "")
        if isinstance(msg, dict):
            return msg.get("role", "")
        return ""

    # ── 操作命令 ────────────────────────────────────────

    async def rollback(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        n_rounds: int = 1,
    ) -> int:
        """撤销 N 轮对话（删除最后 N 组 user+assistant 消息）。"""
        messages = await self._load_and_check_messages(session_id, user_id, channel)

        user_indices = [i for i, m in enumerate(messages) if self._get_role(m) == "user"]
        if len(user_indices) < n_rounds:
            raise ValueError(
                f"只有 {len(user_indices)} 轮对话，无法撤销 {n_rounds} 轮。"
            )

        cutoff = user_indices[-n_rounds]
        new_messages = messages[:cutoff]
        deleted = len(messages) - len(new_messages)
        logger.info("rollback: 删除 %d 条，保留 %d 条", deleted, len(new_messages))

        await self._update_content(session_id, user_id, channel, new_messages)
        return deleted

    async def fork(
        self,
        source_id: str,
        user_id: str,
        channel: str,
        n_rounds: int = 0,
        new_id: str | None = None,
    ) -> str:
        """从源会话分叉出新会话。"""
        messages = await self._load_and_check_messages(source_id, user_id, channel)

        if n_rounds > 0:
            user_indices = [i for i, m in enumerate(messages) if self._get_role(m) == "user"]
            if len(user_indices) < n_rounds:
                raise ValueError(
                    f"只有 {len(user_indices)} 轮对话，无法回退 {n_rounds} 轮后分叉。"
                )
            cutoff = user_indices[-n_rounds]
            messages = messages[:cutoff]
            logger.debug("fork: 保留 %d 条消息", len(messages))

        new_id = new_id or f"{source_id}_fork_{datetime.now():%Y%m%d_%H%M%S}"

        state = await self._load_state(source_id, user_id, channel)
        state["session_id"] = new_id
        state["agent"]["memory"]["content"] = messages

        new_file = self._get_session_file_path(new_id, user_id, channel)
        new_file.parent.mkdir(parents=True, exist_ok=True)
        with open(new_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

        logger.info("fork: 已创建新会话 %s", new_id)
        return new_id

    async def trim_last_round(
        self,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> str:
        """移除最后一轮（用户消息 + 之后所有消息），返回最后用户消息文本。

        用于 /regen：保留之前的对话上下文，让最后一条用户消息重新进入 AI 流程。
        """
        messages = await self._load_and_check_messages(session_id, user_id, channel)

        user_indices = [i for i, m in enumerate(messages) if self._get_role(m) == "user"]
        if not user_indices:
            raise ValueError("当前没有用户消息，无法重新生成。")

        last_idx = user_indices[-1]

        # 提取最后用户消息的文本
        last_msg = messages[last_idx]
        user_text = ""
        if isinstance(last_msg, list) and len(last_msg) > 0:
            content = last_msg[0].get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        user_text = block.get("text", "")
                        break
            elif isinstance(content, str):
                user_text = content

        if not user_text:
            raise ValueError("最后一条用户消息没有文本内容，无法重新生成。")

        trimmed = messages[:last_idx]
        logger.info("trim_last_round: 保留 %d 条，删除 %d 条", len(trimmed), len(messages) - len(trimmed))
        await self._update_content(session_id, user_id, channel, trimmed)
        return user_text

    # ── 内部辅助 ────────────────────────────────────────

    async def _load_and_check_messages(
        self,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> List[Any]:
        """加载消息列表，空列表时抛出 ValueError。"""
        try:
            messages = await self.get_messages(session_id, user_id, channel)
        except FileNotFoundError as e:
            raise ValueError(
                "会话不存在，请先在当前会话发送几条消息。"
            ) from e
        if not messages:
            raise ValueError("当前会话没有任何消息。")
        return messages

    async def _update_content(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        messages: List[Any],
    ):
        """更新会话文件中的 memory.content。"""
        state = await self._load_state(session_id, user_id, channel)
        state["agent"]["memory"]["content"] = messages
        await self._save_state(session_id, user_id, channel, state)
