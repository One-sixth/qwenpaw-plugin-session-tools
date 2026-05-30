"""会话文件读写与操作核心。

通过 QwenPaw 官方 SafeJSONSession API 读写会话文件，
不再直接操作文件系统。
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """将文件名中的非法字符替换为 '--'，确保跨平台兼容。"""
    return re.sub(r'[\\/:*?"<>|]', '--', name)


class _StateProxy:
    """最小模块代理，满足 SafeJSONSession 的 state_module 协议。"""

    def __init__(self):
        self.data: dict = {}

    def state_dict(self) -> dict:
        return self.data

    def load_state_dict(self, d: dict) -> None:
        self.data = d


class SessionOperator:
    """会话操作核心类 — 通过 SafeJSONSession API 读写会话文件。"""

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir
        from qwenpaw.app.chats.session import SafeJSONSession

        self.session = SafeJSONSession(str(workspace_dir / "sessions"))

    # ── 格式检测 ────────────────────────────────────────

    def _detect_format(self, state: Dict) -> str:
        """检测会话文件格式。

        Returns:
            "old": state["agent"]["memory"]["content"] 旧格式 [msg_dict, extra_list] 对列表
            "new": state["agent"]["state"]["context"] 新格式 Msg dict 列表
            "unknown": 无法识别
        """
        agent = state.get("agent", {})
        if "memory" in agent:
            return "old"
        if "state" in agent:
            return "new"
        return "unknown"

    def _get_msg_key(self, fmt: str) -> str:
        """根据格式返回消息列表在 state 中的点号路径 key。"""
        if fmt == "old":
            return "agent.memory.content"
        return "agent.state.context"

    # ── 消息操作 ────────────────────────────────────────

    async def get_messages(self, session_id: str, user_id: str, channel: str) -> List[Any]:
        """获取会话消息列表。

        通过 SafeJSONSession API 读取，自动检测新旧两种格式。

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            channel: 通道名称

        Returns:
            List[Any]: 消息列表（旧格式为 [msg_dict, extra_list] 对，新格式为 Msg dict）
        """
        state = await self.session.get_session_state_dict(
            session_id,
            user_id=user_id,
            channel=channel,
            allow_not_exist=False,
        )
        fmt = self._detect_format(state)

        if fmt == "old":
            messages = state.get("agent", {}).get("memory", {}).get("content", [])
        elif fmt == "new":
            messages = state.get("agent", {}).get("state", {}).get("context", [])
        else:
            messages = []

        logger.debug("获取到 %d 条消息（格式: %s）", len(messages), fmt)
        return messages

    async def find_message_by_created_at(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        created_at: str,
    ) -> tuple[int, Any]:
        """按 created_at 时间戳查找消息。

        支持两种格式的时间戳：
        - ISO 字符串：如 "2026-07-12T17:46:53+00:00"
        - Unix 秒数：如 "1783851825"（数字字符串）

        会话文件中的 created_at 是 datetime 对象，匹配时做模糊比较（同秒）。

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            channel: 通道名称
            created_at: 前端传来的 created_at 值（字符串形式）

        Returns:
            tuple[int, Any]: (文件索引, 消息对象)，未找到则返回 (-1, None)
        """
        messages = await self.get_messages(session_id, user_id, channel)
        if not messages:
            return -1, None

        # 解析目标时间
        target_dt = None
        created_at = created_at.strip()

        # 尝试 ISO 格式
        for fmt in [
            "%Y-%m-%dT%H:%M:%S%z",     # 2026-07-12T17:46:53+00:00
            "%Y-%m-%dT%H:%M:%S.%f%z",  # 带毫秒+时区
            "%Y-%m-%dT%H:%M:%S.%f",    # 带毫秒无时区
            "%Y-%m-%d %H:%M:%S%z",     # 空格分隔
            "%Y-%m-%dT%H:%M:%S",       # 无时区
            "%Y-%m-%d %H:%M:%S",       # 空格无时区
        ]:
            try:
                target_dt = datetime.strptime(created_at, fmt)
                break
            except ValueError:
                continue

        # 尝试 Unix 秒数
        if target_dt is None:
            try:
                ts = float(created_at)
                if ts > 1e8:  # 合理时间戳范围
                    target_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OverflowError):
                pass

        if target_dt is None:
            logger.warning("无法解析 created_at: %s", created_at)
            return -1, None

        # 确保 target_dt 有时区信息
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=timezone.utc)

        # 把目标时间转成本地时间（会话文件中的 created_at 是本地时间）
        import datetime as _dt_mod
        LOCAL_TZ = _dt_mod.timezone(_dt_mod.timedelta(hours=8), "Asia/Shanghai")
        target_local = target_dt.astimezone(LOCAL_TZ)

        # 从后往前找，找时间接近的消息（同秒匹配）
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            # 提取 msg dict
            if isinstance(msg, list) and len(msg) > 0:
                d = msg[0]
            elif isinstance(msg, dict):
                d = msg
            else:
                continue

            msg_ct = d.get("created_at", None)
            if msg_ct is None:
                continue

            # 会话文件中的 created_at 可能是 datetime 或字符串
            if isinstance(msg_ct, datetime):
                if msg_ct.tzinfo is None:
                    msg_ct = msg_ct.replace(tzinfo=LOCAL_TZ)  # 标记为本地时区！
                # 比较到秒级
                if msg_ct.strftime("%Y-%m-%d %H:%M:%S") == target_local.strftime("%Y-%m-%d %H:%M:%S"):
                    return i, msg
            elif isinstance(msg_ct, str):
                try:
                    msg_dt = datetime.fromisoformat(msg_ct)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                    if msg_dt.strftime("%Y-%m-%d %H:%M:%S") == target_local.strftime("%Y-%m-%d %H:%M:%S"):
                        return i, msg
                except (ValueError, TypeError):
                    # 字符串比较直接相等
                    if msg_ct.strip() == created_at:
                        return i, msg

        return -1, None

    async def find_message_by_id_or_text(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        message_id: str = "",
        message_text: str = "",
        role: str = "",
    ) -> tuple[int, Any]:
        """按消息 ID 或文本内容查找消息。

        优先按 ID 查找，如果找不到且提供了文本，则按文本匹配。
        文本匹配时支持指定 role 来缩小范围。

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            channel: 通道名称
            message_id: 消息 ID（可选）
            message_text: 消息文本内容（可选，按文本前缀匹配）
            role: 角色过滤（可选，如 "user"/"assistant"）

        Returns:
            tuple[int, Any]: (文件索引, 消息对象)，未找到则返回 (-1, None)
        """
        messages = await self.get_messages(session_id, user_id, channel)

        # 先按 ID 查找
        if message_id:
            for i, msg in enumerate(messages):
                if isinstance(msg, list) and len(msg) > 0 and isinstance(msg[0], dict):
                    mid = msg[0].get("id", "")
                    meta = msg[0].get("metadata", {}) or {}
                elif isinstance(msg, dict):
                    mid = msg.get("id", "")
                    meta = msg.get("metadata", {}) or {}
                else:
                    continue
                if mid == message_id:
                    return i, msg
                # SSE 流新消息的 _client_id 匹配
                if isinstance(meta, dict) and meta.get("_client_id") == message_id:
                    return i, msg

        # 按文本匹配（从后往前找，找最新的匹配消息）
        if message_text:
            norm_text = message_text.strip().lower()
            # 从最新消息开始找
            for i in range(len(messages) - 1, -1, -1):
                msg = messages[i]
                # 检查 role
                msg_role = ""
                if isinstance(msg, list) and len(msg) > 0:
                    msg_role = msg[0].get("role", "")
                elif isinstance(msg, dict):
                    msg_role = msg.get("role", "")
                if role and msg_role != role:
                    continue

                # 提取文本
                msg_text = ""
                if isinstance(msg, list) and len(msg) > 0:
                    content = msg[0].get("content", "")
                elif isinstance(msg, dict):
                    content = msg.get("content", "")
                else:
                    continue

                if isinstance(content, str):
                    msg_text = content.strip().lower()
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            msg_text = block.get("text", "").strip().lower()
                            break

                if msg_text and (msg_text.startswith(norm_text[:50]) or norm_text.startswith(msg_text[:50])):
                    return i, msg

        return -1, None

    # ── 操作命令 ────────────────────────────────────────

    async def rollback(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        n_rounds: int = 1,
    ) -> int:
        """撤销 N 轮对话（删除最后 N 组 user+assistant 消息）。"""
        state = await self.session.get_session_state_dict(
            session_id,
            user_id=user_id,
            channel=channel,
            allow_not_exist=False,
        )
        fmt = self._detect_format(state)
        messages = await self.get_messages(session_id, user_id, channel)

        user_indices = [i for i, m in enumerate(messages) if self._get_role(m) == "user"]
        if len(user_indices) < n_rounds:
            raise ValueError(
                f"只有 {len(user_indices)} 轮对话，无法撤销 {n_rounds} 轮。"
            )

        cutoff = user_indices[-n_rounds]
        new_messages = messages[:cutoff]
        deleted = len(messages) - len(new_messages)

        key = self._get_msg_key(fmt)
        await self.session.update_session_state(
            session_id,
            key,
            new_messages,
            user_id=user_id,
            channel=channel,
        )

        logger.info("rollback: 删除 %d 条，保留 %d 条", deleted, len(new_messages))
        return deleted

    async def fork(
        self,
        source_id: str,
        user_id: str,
        channel: str,
        n_rounds: int = 0,
        new_id: str | None = None,
        messages: List[Any] | None = None,
    ) -> str:
        """从源会话分叉出新会话。

        通过 SafeJSONSession API 读取源会话，修改后保存到新 ID。
        如果传入了 messages 参数，则直接使用该消息列表，跳过 n_rounds 处理。

        Args:
            source_id: 源会话 ID
            user_id: 用户 ID
            channel: 通道名称
            n_rounds: 要回退的轮次数，默认 0 表示全部保留（仅在 messages 为 None 时生效）
            new_id: 可选的新会话 ID
            messages: 可选的消息列表，若提供则直接使用此列表（跳过 n_rounds 处理）

        Returns:
            str: 新创建的会话 ID
        """
        state = await self.session.get_session_state_dict(
            source_id,
            user_id=user_id,
            channel=channel,
            allow_not_exist=False,
        )
        fmt = self._detect_format(state)

        if messages is None:
            # 从源会话读取消息列表
            messages = await self.get_messages(source_id, user_id, channel)
            if n_rounds > 0:
                user_indices = [i for i, m in enumerate(messages) if self._get_role(m) == "user"]
                if len(user_indices) < n_rounds:
                    raise ValueError(
                        f"只有 {len(user_indices)} 轮对话，无法回退 {n_rounds} 轮后分叉。"
                    )
                cutoff = user_indices[-n_rounds]
                messages = messages[:cutoff]
        # else: 直接使用传入的消息列表，跳过 n_rounds 处理

        # 修改 state dict
        now = datetime.now()
        new_id = new_id or f"fork-{now.strftime('%Y-%m-%d-%H-%M-%S')}.{now.microsecond // 1000:03d}"
        key = self._get_msg_key(fmt)
        state["session_id"] = new_id

        # 写入新文件：一次性保存所有模块（排除 session_id，避免与 save_session_state 参数冲突）
        modules = {}
        for module_name, module_data in state.items():
            if module_name == "session_id":
                continue
            proxy = _StateProxy()
            proxy.data = module_data
            if module_name == "agent":
                if fmt == "old":
                    proxy.data["memory"]["content"] = messages
                else:
                    proxy.data["state"]["context"] = messages
            modules[module_name] = proxy

        await self.session.save_session_state(
            session_id=new_id,
            user_id=user_id,
            channel=channel,
            **modules,
        )

        logger.info("fork: 已创建新会话 %s（格式: %s）", new_id, fmt)
        return new_id

    async def trim_last_round(
        self,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> str:
        """移除最后一轮，返回最后用户消息文本（用于 /regen）。"""
        state = await self.session.get_session_state_dict(
            session_id,
            user_id=user_id,
            channel=channel,
            allow_not_exist=False,
        )
        fmt = self._detect_format(state)
        messages = await self.get_messages(session_id, user_id, channel)

        if not messages:
            raise ValueError("当前会话没有任何消息。")

        user_indices = [i for i, m in enumerate(messages) if self._get_role(m) == "user"]
        if not user_indices:
            raise ValueError("当前没有用户消息，无法重新生成。")

        last_idx = user_indices[-1]

        # 提取最后用户消息的文本（支持新旧两种格式）
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
        elif isinstance(last_msg, dict):
            content = last_msg.get("content", "")
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
        key = self._get_msg_key(fmt)
        await self.session.update_session_state(
            session_id,
            key,
            trimmed,
            user_id=user_id,
            channel=channel,
        )

        logger.info("trim_last_round: 保留 %d 条，删除 %d 条", len(trimmed), len(messages) - len(trimmed))
        return user_text

    # ── 文本+时间+邻居 三层匹配 ────────────────────────

    async def find_message_by_text_time_neighbors(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        message_text: str = "",
        message_time: str = "",
        role: str = "",
        prev_texts: str = "",
        prev_times: str = "",
        prev_roles: str = "",
        next_texts: str = "",
        next_times: str = "",
        next_roles: str = "",
    ) -> tuple[int, Any]:
        """按文本+时间+邻居三层匹配查找消息。

        第一层：按文本前缀匹配 → 得到候选列表
        第二层：按时间（时分秒）匹配 → 缩小范围
        第三层：按前后邻居消息的文本+时间匹配 → 唯一确定
        仍有多条 → 抛出 ValueError

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            channel: 通道名称
            message_text: 当前消息文本（必填）
            message_time: 当前消息时间（时分秒，如 "19:52:59"）
            role: 角色过滤（如 "user"/"assistant"）
            prev_texts: 前 N 条邻居文本，按 ||| 分隔
            prev_times: 前 N 条邻居时间，按 ||| 分隔
            prev_roles: 前 N 条邻居角色，按 ||| 分隔
            next_texts: 后 N 条邻居文本，按 ||| 分隔
            next_times: 后 N 条邻居时间，按 ||| 分隔
            next_roles: 后 N 条邻居角色，按 ||| 分隔

        Returns:
            tuple[int, Any]: (文件索引, 消息对象)

        Raises:
            ValueError: 存在多个匹配时
        """
        messages = await self.get_messages(session_id, user_id, channel)

        # ── 第一层：文本匹配 ──
        if not message_text:
            raise ValueError("message_text 不能为空")

        norm_text = message_text.strip().lower()
        candidates = []

        for i, msg in enumerate(messages):
            msg_role = self._get_role(msg)
            if role and msg_role != role:
                continue

            msg_text = self._get_msg_text(msg)
            if not msg_text:
                continue

            msg_text_norm = msg_text.strip().lower()
            if msg_text_norm.startswith(norm_text[:50]) or norm_text.startswith(msg_text_norm[:50]):
                candidates.append((i, msg))

        if not candidates:
            return -1, None

        # ── 第二层：时间匹配 ──
        if message_time:
            time_filtered = []
            for i, msg in candidates:
                msg_time = self._get_msg_time(msg)
                if msg_time == message_time:
                    time_filtered.append((i, msg))
            if time_filtered:
                candidates = time_filtered

        if len(candidates) == 1:
            return candidates[0]

        # ── 第三层：邻居匹配 ──
        prev_list = prev_texts.split('|||') if prev_texts else []
        prev_t_list = prev_times.split('|||') if prev_times else []
        prev_r_list = prev_roles.split('|||') if prev_roles else []
        next_list = next_texts.split('|||') if next_texts else []
        next_t_list = next_times.split('|||') if next_times else []
        next_r_list = next_roles.split('|||') if next_roles else []

        if prev_list or next_list:
            neighbor_filtered = []
            for i, msg in candidates:
                if self._match_neighbors(
                    messages, i,
                    prev_list, prev_t_list, prev_r_list,
                    next_list, next_t_list, next_r_list,
                ):
                    neighbor_filtered.append((i, msg))
            if neighbor_filtered:
                candidates = neighbor_filtered

        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            raise ValueError(
                f"存在 {len(candidates)} 个匹配的消息，无法自动定位。"
                "请使用控制台命令 /rewind 或 /fork 操作。"
            )
        else:
            return -1, None

    def _get_msg_text(self, msg: Any) -> str:
        """从消息中提取纯文本内容。兼容新旧两种格式。"""
        if isinstance(msg, list) and len(msg) > 0:
            content = msg[0].get("content", "")
        elif isinstance(msg, dict):
            content = msg.get("content", "")
        else:
            return ""

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
        return ""

    def _get_msg_time(self, msg: Any) -> str:
        """从消息的 created_at 字段提取时分秒字符串（如 '19:52:59'）。"""
        created_at = ""
        if isinstance(msg, list) and len(msg) > 0:
            created_at = msg[0].get("created_at", "")
        elif isinstance(msg, dict):
            created_at = msg.get("created_at", "")

        if not created_at:
            return ""

        # created_at 格式: "2026-07-11T19:52:59.177492" 或类似
        try:
            # 提取 T 后面的时间部分
            time_part = created_at.split("T")[1] if "T" in created_at else created_at
            # 取前 8 个字符 (HH:MM:SS)
            return time_part[:8]
        except Exception:
            return ""

    def _match_neighbors(
        self,
        messages: list,
        current_idx: int,
        prev_texts: list,
        prev_times: list,
        prev_roles: list,
        next_texts: list,
        next_times: list,
        next_roles: list,
    ) -> bool:
        """检查当前消息的邻居是否匹配前端传过来的邻居信息。

        DOM 是倒序排列（最新在上）而 session 文件是正序（最早在前），
        因此边界消息的 prev/next 方向可能相反。越界的邻居直接跳过，
        不要求全匹配。
        """
        # 检查前 N 条邻居
        for offset in range(len(prev_texts)):
            idx = current_idx - (offset + 1)
            if idx < 0:
                continue  # 越界跳过，不直接失败（DOM 倒序时最早消息无前邻居）

            expected_text = prev_texts[offset]
            expected_time = prev_times[offset] if offset < len(prev_times) else ""
            expected_role = prev_roles[offset] if offset < len(prev_roles) else ""

            msg = messages[idx]
            msg_role = self._get_role(msg)
            if expected_role and msg_role != expected_role:
                return False

            if expected_time:
                msg_time = self._get_msg_time(msg)
                if msg_time != expected_time:
                    return False

            if expected_text:
                msg_text = self._get_msg_text(msg)
                if not msg_text:
                    return False
                norm_expected = expected_text.strip().lower()
                norm_msg = msg_text.strip().lower()
                if not (norm_msg.startswith(norm_expected[:50]) or norm_expected.startswith(norm_msg[:50])):
                    return False

        # 检查后 N 条邻居
        for offset in range(len(next_texts)):
            idx = current_idx + (offset + 1)
            if idx >= len(messages):
                continue  # 越界跳过，不直接失败

            expected_text = next_texts[offset]
            expected_time = next_times[offset] if offset < len(next_times) else ""
            expected_role = next_roles[offset] if offset < len(next_roles) else ""

            msg = messages[idx]
            msg_role = self._get_role(msg)
            if expected_role and msg_role != expected_role:
                return False

            if expected_time:
                msg_time = self._get_msg_time(msg)
                if msg_time != expected_time:
                    return False

            if expected_text:
                msg_text = self._get_msg_text(msg)
                if not msg_text:
                    return False
                norm_expected = expected_text.strip().lower()
                norm_msg = msg_text.strip().lower()
                if not (norm_msg.startswith(norm_expected[:50]) or norm_expected.startswith(norm_msg[:50])):
                    return False

        return True

    # ── 内部辅助 ────────────────────────────────────────

    def _get_role(self, msg: Any) -> str:
        """从消息中提取 role 字段。兼容新旧两种格式。"""
        if isinstance(msg, list) and len(msg) > 0 and isinstance(msg[0], dict):
            return msg[0].get("role", "")
        if isinstance(msg, dict):
            return msg.get("role", "")
        return ""

    async def _load_and_check_messages(
        self,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> List[Any]:
        """加载消息列表，空列表时抛出 ValueError。"""
        try:
            messages = await self.get_messages(session_id, user_id, channel)
        except Exception as e:
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
        """更新会话文件中的消息列表。

        通过 SafeJSONSession.update_session_state() API 写入，
        自动检测格式，原子写入 + 写入锁保护。

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            channel: 通道名称
            messages: 新的消息列表
        """
        state = await self.session.get_session_state_dict(
            session_id,
            user_id=user_id,
            channel=channel,
            allow_not_exist=False,
        )
        fmt = self._detect_format(state)
        key = self._get_msg_key(fmt)
        await self.session.update_session_state(
            session_id,
            key,
            messages,
            user_id=user_id,
            channel=channel,
        )