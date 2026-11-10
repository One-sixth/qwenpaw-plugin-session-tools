"""会话文件读写与操作核心。

通过 QwenPaw 官方 SafeJSONSession API 读写会话文件，
不再直接操作文件系统。
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """将文件名中的非法字符替换为 '--'，确保跨平台兼容。"""
    return re.sub(r'[\\/:*?"<>|]', '--', name)


# created_at 渐进窗口匹配的时间层（秒），±1s 优先命中、逐层放宽到 ±3s。
# user 端：前端气泡时间戳与后端写盘时间常有 1~3 秒漂移（2026-11-10 v2.5.0 确立）。
TIME_MATCH_TIERS = (1.0, 2.0, 3.0)

# 助手消息区间匹配的右边界容差（秒）。
# completed_at 为整秒截断（向下取整），快回复（首段保存距完成 <1s）时
# 目标可能落进截断缝隙，右边界 +1s 保证覆盖（2026-11-11 v2.5.3 确立）。
RANGE_END_TOLERANCE = 1.0


# ── scroll 簿记继承修复（v2.5.6）─────────────────────────────────────
#
# scroll checkpoint（会话文件 agent.scroll）里的「持久化判定位」记录着
# 父会话已写入 history.db 的行。fork 整文件复制后，宿主 _persist_new
# 判定这些消息「已持久化」（实际挂的是父 session_id）而全部跳过——
# fork 会话在 scroll 数据库里看不到任何父历史（2026-11-11 实锤：
# 11 月 fork 库中 0 行；老 fork 的行是 session-sync 迁移期批量导入的孤例）。
#
# 修复组合（fork 时执行）：
# 1. 清持久化判定位 → 宿主以 fork session_id 重新 write-through context
# 2. 库行复制 → 父会话全部行（含已驱逐历史）挂 fork session_id，fork 后立即可查
# 3. index 重映射 → 驱逐索引/目录卡片的 seq 换成 fork 新编号，全程自洽

# scroll checkpoint 中需要清空的「持久化判定位」（宿主 manager.to_dict 的键）。
SCROLL_PERSIST_KEYS = (
    "persisted_ids",    # list：已入库的 turn 消息 id
    "persisted_tcids",  # list：已入库的 tool result id
    "seq_by_tcid",      # dict：tcid → seq
    "seq_by_id",        # dict：mid → [seq_lo, seq_hi]
    "model_turn_seq",   # dict：mid → seq
    "model_turn_nblk",  # dict：mid → 块数
    "leaf_by_id",       # dict：mid → [seq, headline]
)

# 与宿主 scroll/history.py 的 _RECALL_TOOL_NAMES 对齐：recall 工具自身的
# 调用/结果行按 seq 直接寻址、不进 FTS 全文索引。复制时同样排除。
RECALL_TOOL_NAMES = ("recall_history", "recall_history_python")



def _copy_history_rows(db_path: Path, parent_sid: str, fork_sid: str) -> Dict[int, int]:
    """把父会话在 history.db 的全部行复制到 fork 会话名下（单事务原子）。

    包括已被 scroll 压缩驱逐的历史——它们只存在于库行、不在会话文件里，
    是「fork 后还能查到压缩前信息」的关键一步。同步补写 FTS 全文索引，
    行为对齐宿主 HistoryStore.append（recall 工具行排除在索引外）。

    Returns:
        {旧seq: 新seq} 映射表（按 dedup_key 配对），供驱逐索引重映射。

    Raises:
        sqlite3.Error / OSError：由调用方降级处理（fork 不因此失败）。
    """
    con = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    try:
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            # 幂等防御：fork 名下已有行（此前复制过）则跳过 INSERT，只构建映射
            cur.execute(
                "SELECT 1 FROM conversation_history WHERE session_id=? LIMIT 1",
                (fork_sid,),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO conversation_history "
                    "(session_id, agent_id, kind, role, name, content, tool_call_id, "
                    " tool_input, tool_state, headline, blocks, metadata, created_at, dedup_key) "
                    "SELECT ?, agent_id, kind, role, name, content, tool_call_id, "
                    "       tool_input, tool_state, headline, blocks, metadata, created_at, dedup_key "
                    "FROM conversation_history WHERE session_id=?",
                    (fork_sid, parent_sid),
                )
                excl = ", ".join("?" for _ in RECALL_TOOL_NAMES)
                cur.execute(
                    "INSERT INTO conversation_history_fts(rowid, content) "
                    "SELECT seq, COALESCE(content, '') FROM conversation_history "
                    f"WHERE session_id=? AND (name IS NULL OR name NOT IN ({excl}))",
                    (fork_sid, *RECALL_TOOL_NAMES),
                )
            # 旧→新 seq 映射：按 dedup_key 配对（会话内唯一，宿主 ux_dedup
            # UNIQUE）。NULL dedup_key 行不参与——宿主写路径从不产生 NULL
            # dedup_key（turn 的 mid、tool result 的 tcid/{mid}#anon{n} 恒非空）
            old = dict(cur.execute(
                "SELECT dedup_key, seq FROM conversation_history "
                "WHERE session_id=? AND dedup_key IS NOT NULL", (parent_sid,)))
            new = dict(cur.execute(
                "SELECT dedup_key, seq FROM conversation_history "
                "WHERE session_id=? AND dedup_key IS NOT NULL", (fork_sid,)))
            seq_map = {old[ok]: new[ok] for ok in old.keys() & new.keys()}
            con.commit()
            return seq_map
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass  # rollback 失败不掩盖原始异常
            raise
    finally:
        con.close()


def _remap_scroll_index(scroll: Dict, seq_map: Dict[int, int], fork_sid: str) -> int:
    """把 scroll.index 里的父会话 seq 重映射为 fork 会话的新 seq。

    index 结构（宿主 eviction_index.to_dict）：
    tiers → blocks → {seq_lo, seq_hi, lines: [[lo, hi, head, tail], ...]}。
    宿主 restore 后以此重建内存索引，首次压缩重渲染目录卡片时全部使用
    fork 自己的编号。无映射的 seq（如被 retention purge 的行）保留原值。
    返回重映射的点数（诊断用）。
    """
    if not seq_map:
        return 0
    index = scroll.get("index")
    if not isinstance(index, dict):
        return 0
    touched = 0
    tiers = index.get("tiers") or index.get("levels") or []
    for tier in tiers:
        if not isinstance(tier, list):
            continue
        for block in tier:
            if not isinstance(block, dict):
                continue
            for key in ("seq_lo", "seq_hi"):
                if block.get(key) in seq_map:
                    block[key] = seq_map[block[key]]
                    touched += 1
            for line in block.get("lines") or []:
                # 病态数据防御（跨版本持久化结构）：短行/非序列跳过，
                # 避免单个坏行让整个重映射中途放弃（留下混合态索引）
                if not (isinstance(line, (list, tuple)) and len(line) >= 2):
                    continue
                for i in (0, 1):
                    if line[i] in seq_map:
                        line[i] = seq_map[line[i]]
                        touched += 1
    index["session_id"] = fork_sid
    return touched



def find_turn_user(messages: list, idx: int) -> int:
    """从 idx 往前找本轮的 user 消息下标；找不到返回 -1。"""
    for i in range(idx, -1, -1):
        msg = messages[i]
        role = msg[0].get("role", "") if isinstance(msg, list) and msg else (
            msg.get("role", "") if isinstance(msg, dict) else ""
        )
        if role == "user":
            return i
    return -1


def find_turn_end(messages: list, start: int) -> int:
    """从 start（user 消息）往后找到下一条 user 消息为止，返回开区间右界。

    一轮 = user + 该 user 之后的全部 assistant/system（工具调用链）消息。
    """
    for j in range(start + 1, len(messages)):
        msg = messages[j]
        role = msg[0].get("role", "") if isinstance(msg, list) and msg else (
            msg.get("role", "") if isinstance(msg, dict) else ""
        )
        if role == "user":
            return j
    return len(messages)


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

    def _parse_timestamp(self, text: str):
        """解析前端传来的时间戳：ISO 字符串（可带时区）或 Unix 秒数。

        无时区输入按 UTC 解释（前端约定传 UTC 或 Unix 秒）；无法解析返回 None。
        """
        text = (text or "").strip()
        if not text:
            return None
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
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        # 尝试 Unix 秒数（合理范围防毫秒串误判：1e8≈1973 年，4e9≈2109 年）
        try:
            ts = float(text)
            if 1e8 < ts < 4e9:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OverflowError):
            pass
        return None

    def _find_assistant_by_range(
        self,
        msg_entries: list,
        start_ts: float,
        completed_at: str,
    ) -> tuple:
        """助手消息区间匹配（v2.5.3）：created_at ~ completed_at 自适应区间。

        数学依据：助手消息顶层 created_at（首段保存时刻）必然落在
        [发送, 完成] 区间内，区间随生成时长自适应，不受固定窗口限制。
        右边界 +RANGE_END_TOLERANCE 容差：completed_at 为整秒截断，快回复
        （首段保存距完成 <1s）时目标可能落进截断缝隙；左边界不放宽：
        created_at 同为向下截断（≤ 真实发送时刻），放宽反而可能框进上一轮
        助手消息造成歧义。
        """
        completed_text = (completed_at or "").strip()
        if not completed_text:
            raise ValueError(
                "assistant 消息定位缺少 completed_at 参数。正常情况下前端"
                "按钮上下文始终携带该字段，若缺失说明 QwenPaw 官方可能修改"
                "了消息数据结构，请开发者检查并更新插件。"
            )
        completed_dt = self._parse_timestamp(completed_text)
        if completed_dt is None:
            raise ValueError(
                f"无法解析 completed_at: {completed_text}（请开发者检查"
                "QwenPaw 官方消息数据结构是否变更）"
            )
        if completed_dt.tzinfo is None:
            completed_dt = completed_dt.replace(tzinfo=timezone.utc)
        end_ts = completed_dt.timestamp() + RANGE_END_TOLERANCE

        matches = [
            (i, m) for i, m, ts in msg_entries
            if start_ts <= ts <= end_ts and self._get_role(m) == "assistant"
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(
                f"定位消息失败，区间 [created_at, completed_at+{RANGE_END_TOLERANCE:g}s] "
                f"内找到 {len(matches)} 条 assistant 消息，存在歧义，请刷新网页后重试"
            )
        return -1, None

    async def find_message_by_created_at(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        created_at: str,
        role: str = "",
        completed_at: str = "",
    ) -> tuple:
        """按 created_at 定位消息。不同角色用不同策略（v2.5.3）。

        - role="assistant"：区间匹配——msg.created_at ∈
          [created_at, completed_at + 容差] 且 role=assistant。
          completed_at 必填，缺失抛 ValueError 提示开发者（正常情况下前端
          按钮上下文始终携带该字段）。刷新后场景有 original_id，走精确
          匹配，不进入本方法。
        - 其他（user / 未指定 role）：渐进窗口 ±1s → ±2s → ±3s
          （TIME_MATCH_TIERS），每层先按 role 过滤再判定：0 条候选 → 扩窗；
          1 条 → 命中；≥2 条 → 抛 ValueError（真歧义）。

        支持两种格式的时间戳：ISO 字符串（如 "2026-07-12T17:46:53+00:00"）
        与 Unix 秒数（如 "1783851825"）。
        注意：无时区的 created_at 输入按 UTC 解释（前端约定传 UTC 或 Unix 秒）；
        会话文件内无时区时间按本地 Asia/Shanghai（UTC+8）解释。

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            channel: 通道名称
            created_at: 前端传来的 created_at 值（字符串形式）
            role: 角色过滤（"user"/"assistant"），可选
            completed_at: assistant 区间匹配的终点（前端 completed_at 字段），
                仅 role="assistant" 时使用，必填

        Returns:
            tuple: (文件索引, 消息对象)，未找到则返回 (-1, None)

        Raises:
            ValueError: assistant 缺少/无法解析 completed_at；或窗口/区间内
                存在多条同角色候选（歧义）
        """
        messages = await self.get_messages(session_id, user_id, channel)
        if not messages:
            return -1, None

        # 解析目标时间
        target_dt = self._parse_timestamp(created_at)
        if target_dt is None:
            logger.warning("无法解析 created_at: %s", created_at)
            return -1, None

        # 确保 target_dt 有时区信息
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=timezone.utc)

        # 把目标时间转成 Unix 秒数
        target_ts = target_dt.timestamp()

        import datetime as _dt_mod
        LOCAL_TZ = _dt_mod.timezone(_dt_mod.timedelta(hours=8), "Asia/Shanghai")

        # 统一计算所有消息的 Unix 秒数
        msg_entries = []  # (index, msg, msg_ts)
        for i, msg in enumerate(messages):
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

            # 会话文件中的 created_at 可能是 datetime 或字符串，统一转成 Unix 秒数
            if isinstance(msg_ct, datetime):
                if msg_ct.tzinfo is None:
                    msg_ct = msg_ct.replace(tzinfo=LOCAL_TZ)  # 本地时间！
                msg_ts = msg_ct.timestamp()
            elif isinstance(msg_ct, str):
                try:
                    msg_dt = datetime.fromisoformat(msg_ct)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=LOCAL_TZ)  # 本地时间！
                    msg_ts = msg_dt.timestamp()
                except (ValueError, TypeError):
                    continue
            else:
                continue

            msg_entries.append((i, msg, msg_ts))

        # ── assistant：区间匹配（v2.5.3）──
        if role == "assistant":
            return self._find_assistant_by_range(msg_entries, target_ts, completed_at)

        # ── user / 未指定 role：渐进窗口匹配 ±1s → ±2s → ±3s，每层先筛 role ──
        for window in TIME_MATCH_TIERS:
            tier = [
                (i, m) for i, m, ts in msg_entries
                if abs(ts - target_ts) <= window
                and (not role or self._get_role(m) == role)
            ]
            if len(tier) == 1:
                return tier[0]
            if len(tier) > 1:
                raise ValueError(
                    f"定位消息失败，基于 created_at（±{window:g}s 窗口）找到 "
                    f"{len(tier)} 条同角色匹配消息，存在歧义，请刷新网页后重试"
                )
            # 该层 0 条候选 → 继续下一层窗口

        return -1, None

    async def find_message_by_id(
        self,
        session_id: str,
        user_id: str,
        channel: str,
        msg_id: str = "",
    ) -> tuple[int, Any]:
        """按消息 ID 查找消息。

        按 session 文件中的 msg.id 精确匹配。
        original_id（前端 ctx.data.metadata.original_id）就是 msg.id，
        因此该函数同时服务于 original_id 定位。

        Args:
            session_id: 会话 ID
            user_id: 用户 ID
            channel: 通道名称
            msg_id: 消息 ID（对应 msg.id，可选）

        Returns:
            tuple[int, Any]: (文件索引, 消息对象)，未找到则返回 (-1, None)
        """
        messages = await self.get_messages(session_id, user_id, channel)

        if not msg_id:
            return -1, None

        for i, msg in enumerate(messages):
            if isinstance(msg, list) and len(msg) > 0 and isinstance(msg[0], dict):
                mid = msg[0].get("id", "")
            elif isinstance(msg, dict):
                mid = msg.get("id", "")
            else:
                continue
            if mid == msg_id:
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
                # scroll 簿记继承修复（v2.5.6）：清持久化判定位，防止宿主
                # _persist_new 误判「已持久化」（实际挂父 session_id）而
                # 跳过写入。old fmt 无 scroll 键，天然跳过。
                scroll = proxy.data.get("scroll")
                if isinstance(scroll, dict):
                    for persist_key in SCROLL_PERSIST_KEYS:
                        scroll.pop(persist_key, None)
            modules[module_name] = proxy

        # 库行复制 + index 重映射（v2.5.6）：把父会话在 history.db 的全部行
        # （含已被压缩驱逐、不在 context 里的历史）复制到 fork 名下，让
        # 压缩前信息 fork 后依然可查；驱逐索引 seq 同步换成 fork 新编号。
        # 失败降级为纯文件 fork（仅 context 部分由 write-through 修复），
        # 不阻塞 fork 本身。
        scroll = None
        agent_mod = modules.get("agent")
        if agent_mod is not None and isinstance(agent_mod.data, dict):
            scroll = agent_mod.data.get("scroll")
        if isinstance(scroll, dict):
            db_path = self.workspace_dir / "history.db"
            if db_path.exists():
                try:
                    seq_map = await asyncio.to_thread(
                        _copy_history_rows, db_path, source_id, new_id,
                    )
                except Exception as e:
                    logger.warning(
                        "fork: history.db 行复制失败，降级为纯文件 fork"
                        "（被驱逐历史将不可查）: %s", e,
                    )
                else:
                    try:
                        remapped = _remap_scroll_index(scroll, seq_map, new_id)
                        logger.info(
                            "fork: history.db 行已复制 %s → %s"
                            "（%d 行，index 重映射 %d 点）",
                            source_id, new_id, len(seq_map), remapped,
                        )
                    except Exception as e:
                        # 行已复制提交，仅重映射失败——索引功能良性
                        #（父 seq 与 fork seq 指向内容相同的真实行），日志区分
                        logger.warning(
                            "fork: index 重映射失败（行已复制、历史仍可查）: %s", e,
                        )

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
        expect_count: int | None = None,
    ):
        """更新会话文件中的消息列表。

        通过 SafeJSONSession.update_session_state() API 写入，
        自动检测格式，原子写入 + 写入锁保护。

        expect_count 不为 None 时做乐观并发校验：写入前重新读一次磁盘，
        若消息总数 != expect_count，说明「读取 → 写回」期间 agent 追加了
        消息，放弃写入抛 SessionChangedError，避免静默覆盖导致丢消息。
        """
        state = await self.session.get_session_state_dict(
            session_id,
            user_id=user_id,
            channel=channel,
            allow_not_exist=False,
        )
        fmt = self._detect_format(state)
        if expect_count is not None:
            current = (
                state.get("agent", {}).get("memory", {}).get("content", [])
                if fmt == "old"
                else state.get("agent", {}).get("state", {}).get("context", [])
            )
            if len(current) != expect_count:
                raise SessionChangedError(
                    f"会话在操作期间被修改（{expect_count} → {len(current)} 条），"
                    "为避免覆盖新消息已中止写入，请重试"
                )
        key = self._get_msg_key(fmt)
        await self.session.update_session_state(
            session_id,
            key,
            messages,
            user_id=user_id,
            channel=channel,
        )


class SessionChangedError(RuntimeError):
    """会话在「读取 → 写回」之间被追加/修改（并发写入），写入已中止。"""
