"""测试 find_message_by_created_at 的匹配逻辑。

运行方式：
    pip install pytest pytest-asyncio
    pytest test_find_message.py -v
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
from datetime import datetime, timezone, timedelta

from session_ops import SessionOperator, find_turn_end, find_turn_user

LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def make_msg(created_at: str, role: str = "user", idx: int = 0) -> dict:
    """构造一条新格式消息 dict。"""
    return {
        "id": f"msg_{idx:03d}",
        "role": role,
        "name": "user" if role == "user" else "assistant",
        "content": [{"type": "text", "text": f"test message {idx}"}],
        "created_at": created_at,
        "metadata": {},
    }


def make_old_msg(created_at: str, role: str = "user", idx: int = 0) -> list:
    """构造一条旧格式消息 [msg_dict, extra] 对。"""
    return [make_msg(created_at, role, idx), {}]


@pytest.fixture
def operator():
    """创建一个 SessionOperator 实例，绕过 __init__，直接 mock get_messages。"""
    op = SessionOperator.__new__(SessionOperator)
    op.workspace_dir = Path("/tmp/test_ws")
    op.get_messages = AsyncMock()
    return op


# ======================================================================
# 严格匹配（同一秒）
# ======================================================================


class TestStrictMatch:
    """严格匹配：int(ts) == int(target_ts)"""

    @pytest.mark.asyncio
    async def test_iso_string(self, operator):
        """前端传 ISO 带时区字符串，会话文件同秒 → 匹配成功"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
            make_msg("2026-07-20T20:00:00.000000", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",  # UTC → 上海 19:59:28
        )
        assert idx == 0
        assert msg["id"] == "msg_000"

    @pytest.mark.asyncio
    async def test_unix_timestamp(self, operator):
        """前端传 Unix 秒数，会话文件同秒 → 匹配成功"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
        ]
        # 1784548768 = 2026-07-20T11:59:28 UTC = 上海 19:59:28
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="1784548768",
        )
        assert idx == 0
        assert msg["id"] == "msg_000"

    @pytest.mark.asyncio
    async def test_old_format_list(self, operator):
        """旧格式 [msg_dict, extra] 对 → 匹配成功"""
        operator.get_messages.return_value = [
            make_old_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
            make_old_msg("2026-07-20T20:00:00.000000", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0
        assert msg[0]["id"] == "msg_000"

    @pytest.mark.asyncio
    async def test_match_last_message(self, operator):
        """同秒内有 user+assistant 多条消息（不传 role）→ 歧义报错"""
        # v2.5.0：不传 role 时窗口内 2 条候选 → 报错（role 筛选可消歧，见 TestRoleFilter）
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
            make_msg("2026-07-20T19:59:28.654321", role="assistant", idx=1),
        ]
        with pytest.raises(ValueError, match="同角色匹配消息"):
            await operator.find_message_by_created_at(
                "sid", "uid", "ch",
                created_at="2026-07-20T11:59:28+00:00",
            )


# ======================================================================
# 宽松匹配（±1 秒容差）
# ======================================================================


class TestFuzzyMatch:
    """宽松匹配：abs(ts - target_ts) <= 1"""

    @pytest.mark.asyncio
    async def test_1s_behind(self, operator):
        """会话文件比目标晚1秒 → 宽松匹配成功"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:29.000000", role="user", idx=0),
        ]
        # 目标 19:59:28，会话文件 19:59:29，差1秒
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0

    @pytest.mark.asyncio
    async def test_1s_ahead(self, operator):
        """会话文件比目标早1秒 → 宽松匹配成功"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:27.123456", role="user", idx=0),
        ]
        # 目标 19:59:28，会话文件 19:59:27，差1秒
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0

    @pytest.mark.asyncio
    async def test_strict_preferred_over_fuzzy(self, operator):
        """严格匹配优先于宽松匹配：同秒有匹配就不走宽松"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),  # 严格匹配
            make_msg("2026-07-20T19:59:29.654321", role="assistant", idx=1),  # 宽松匹配
        ]
        # 严格匹配找到 idx=0，直接返回，不走宽松
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0
        assert msg["id"] == "msg_000"


# ======================================================================
# 多匹配报错
# ======================================================================


class TestMultipleMatchError:
    """多个匹配时抛出 ValueError"""

    @pytest.mark.asyncio
    async def test_strict_multiple(self, operator):
        """同一秒内有多条消息（不传 role）→ 歧义报错"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
            make_msg("2026-07-20T19:59:28.654321", role="assistant", idx=1),
        ]
        with pytest.raises(ValueError, match="同角色匹配消息"):
            await operator.find_message_by_created_at(
                "sid", "uid", "ch",
                created_at="2026-07-20T11:59:28+00:00",
            )

    @pytest.mark.asyncio
    async def test_fuzzy_multiple(self, operator):
        """±1s 窗口内有多条消息（不传 role）→ 首层窗口即报错"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:29.000000", role="user", idx=0),  # target+1
            make_msg("2026-07-20T19:59:31.000000", role="assistant", idx=1),  # target+1
        ]
        # 目标 19:59:30（上海时间），两条都差 1 秒 → 层1（±1s）即 2 条 → 报错
        with pytest.raises(ValueError, match=r"±1s 窗口"):
            await operator.find_message_by_created_at(
                "sid", "uid", "ch",
                created_at="2026-07-20T11:59:30+00:00",  # UTC → 上海 19:59:30
            )


# ======================================================================
# 未找到消息
# ======================================================================


class TestNoMatch:
    """没有匹配的消息时返回 (-1, None)"""

    @pytest.mark.asyncio
    async def test_no_match(self, operator):
        """时间差超过容差 → 未找到"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T20:00:00.000000", role="user", idx=0),
        ]
        # 目标 19:59:28，会话文件 20:00:00，差 32 秒，超出容差
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == -1
        assert msg is None

    @pytest.mark.asyncio
    async def test_empty_messages(self, operator):
        """空消息列表 → 未找到"""
        operator.get_messages.return_value = []
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == -1
        assert msg is None

    @pytest.mark.asyncio
    async def test_invalid_created_at(self, operator):
        """无法解析的 created_at 值 → 未找到"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="not-a-timestamp",
        )
        assert idx == -1
        assert msg is None


# ======================================================================
# 边界情况
# ======================================================================


class TestEdgeCases:
    """边界情况"""

    @pytest.mark.asyncio
    async def test_no_created_at_field(self, operator):
        """消息缺少 created_at 字段 → 跳过"""
        operator.get_messages.return_value = [
            {"id": "msg_000", "role": "user", "content": "hello"},
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == -1
        assert msg is None

    @pytest.mark.asyncio
    async def test_iso_without_timezone(self, operator):
        """前端传无时区 ISO 字符串 → 补 UTC，正确转本地后匹配"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
        ]
        # "2026-07-20T11:59:28" 无时区 → 补 UTC → 11:59:28 UTC → 上海 19:59:28
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28",
        )
        assert idx == 0

    @pytest.mark.asyncio
    async def test_iso_with_space(self, operator):
        """前端传空格分隔的 ISO 字符串（无时区）→ 补 UTC，正确转本地后匹配"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
        ]
        # "2026-07-20 11:59:28" 无时区 → 补 UTC → 11:59:28 UTC → 上海 19:59:28
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20 11:59:28",
        )
        assert idx == 0

    @pytest.mark.asyncio
    async def test_local_timezone_iso_with_plus8(self, operator):
        """前端传 +08:00 时区的 ISO 字符串 → 正确转本地"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
        ]
        # "2026-07-20T19:59:28+08:00" 直接就是上海时间
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T19:59:28+08:00",
        )
        assert idx == 0

# ======================================================================
# 渐进窗口匹配（v2.5.0：±1s → ±2s → ±3s）
# ======================================================================


class TestTieredWindow:
    """渐进窗口：窄窗优先命中，0 条才扩窗"""

    @pytest.mark.asyncio
    async def test_tier1_hit(self, operator):
        """Δ0.5s → 层1（±1s）直接命中"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.500000", role="user", idx=0),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0

    @pytest.mark.asyncio
    async def test_tier2_hit_after_tier1_miss(self, operator):
        """Δ1.5s → 层1 无候选，层2（±2s）命中"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:29.500000", role="user", idx=0),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0

    @pytest.mark.asyncio
    async def test_tier3_hit_after_tier1_tier2_miss(self, operator):
        """Δ2.5s → 层1/层2 无候选，层3（±3s）命中（覆盖 3 秒漂移场景）"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:30.500000", role="user", idx=0),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0

    @pytest.mark.asyncio
    async def test_narrow_window_preferred(self, operator):
        """窄窗优先：Δ0.5s 与 Δ2.5s 两条同 role → 层1 命中 Δ0.5s 那条，不扩窗"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.500000", role="user", idx=0),   # Δ0.5s
            make_msg("2026-07-20T19:59:30.500000", role="user", idx=1),   # Δ2.5s
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0
        assert msg["id"] == "msg_000"

    @pytest.mark.asyncio
    async def test_beyond_3s_no_match(self, operator):
        """Δ3.5s → 三层窗口全 0 条 → 未找到"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:31.500000", role="user", idx=0),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == -1
        assert msg is None


# ======================================================================
# role 筛选（v2.5.0：每层窗口先按 role 过滤再判定）
# ======================================================================


class TestRoleFilter:
    """role 过滤：user/assistant 时间相近不再撞车"""

    @pytest.mark.asyncio
    async def test_role_resolves_same_second_collision(self, operator):
        """同秒 user+assistant 两条，传 role='user' → 命中 user 那条（旧版会 409）"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
            make_msg("2026-07-20T19:59:28.654321", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
            role="user",
        )
        assert idx == 0
        assert msg["role"] == "user"

    @pytest.mark.asyncio
    async def test_role_multiple_same_role_still_409(self, operator):
        """窗口内多条同 role → 传 role 依然报错（真歧义）"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
            make_msg("2026-07-20T19:59:28.654321", role="user", idx=1),
        ]
        with pytest.raises(ValueError, match="同角色匹配消息"):
            await operator.find_message_by_created_at(
                "sid", "uid", "ch",
                created_at="2026-07-20T11:59:28+00:00",
                role="user",
            )

    @pytest.mark.asyncio
    async def test_role_mismatch_no_match(self, operator):
        """窗口内只有 user 消息，传 role='assistant' → 未找到"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
            role="assistant",
            completed_at="2026-07-20T11:59:30+00:00",
        )
        assert idx == -1
        assert msg is None

    @pytest.mark.asyncio
    async def test_role_tier_crossing(self, operator):
        """role 筛选跨层：Δ0.5s 有 assistant，Δ1.5s 有 user；传 role='user' → 层2 命中 user"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.500000", role="assistant", idx=0),  # Δ0.5s
            make_msg("2026-07-20T19:59:29.500000", role="user", idx=1),       # Δ1.5s
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
            role="user",
        )
        assert idx == 1
        assert msg["role"] == "user"


# ======================================================================
# 窗口闭区间边界（v2.5.0 补充：Δ 恰好等于 2.0/3.0）
# ======================================================================


class TestWindowBoundaries:
    """Δ 恰好等于窗口阈值时的闭区间归属（<= 判定）"""

    @pytest.mark.asyncio
    async def test_exact_2s_hits_tier2(self, operator):
        """Δ=2.0s → 层1 无候选，层2（±2s）闭区间命中"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:30.000000", role="user", idx=0),
        ]
        idx, _msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0

    @pytest.mark.asyncio
    async def test_exact_3s_hits_tier3(self, operator):
        """Δ=3.0s → 层1/层2 无候选，层3（±3s）闭区间命中"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:31.000000", role="user", idx=0),
        ]
        idx, _msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
        )
        assert idx == 0

    @pytest.mark.asyncio
    async def test_millisecond_input_rejected(self, operator):
        """Unix 毫秒串（>4e9）→ 视为非法，未找到而非荒谬远期匹配"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="1784548768000",  # 毫秒时间戳
        )
        assert idx == -1
        assert msg is None


class TestRoleFilterOldFormat:
    """role 过滤对旧格式 [msg_dict, extra] 的兼容"""

    @pytest.mark.asyncio
    async def test_old_format_role_filter(self, operator):
        """旧格式消息同秒 user+assistant，传 role='assistant' → 命中 assistant"""
        operator.get_messages.return_value = [
            make_old_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
            make_old_msg("2026-07-20T19:59:28.654321", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-07-20T11:59:28+00:00",
            role="assistant",
            completed_at="2026-07-20T11:59:30+00:00",
        )
        assert idx == 1
        assert msg[0]["role"] == "assistant"


# ======================================================================
# 轮次感知删除辅助函数（v2.5.0：find_turn_user / find_turn_end）
# ======================================================================


class TestFindTurnHelpers:
    """find_turn_user / find_turn_end 的纯函数单测"""

    def test_turn_user_hit_immediately(self):
        """idx 即 user 消息 → 返回自身"""
        messages = [
            {"role": "user", "id": "u1"},
            {"role": "assistant", "id": "a1"},
        ]
        assert find_turn_user(messages, 1) == 0
        assert find_turn_user(messages, 0) == 0

    def test_turn_user_scan_backwards(self):
        """idx 为 assistant，往前跳过 system 找到本轮 user"""
        messages = [
            {"role": "user", "id": "u1"},
            {"role": "assistant", "id": "a1"},
            {"role": "system", "id": "t1"},  # 工具调用链
            {"role": "assistant", "id": "a2"},
        ]
        assert find_turn_user(messages, 3) == 0
        assert find_turn_user(messages, 2) == 0

    def test_turn_user_not_found(self):
        """idx 之前没有任何 user 消息 → -1"""
        messages = [
            {"role": "system", "id": "s1"},
            {"role": "assistant", "id": "a1"},
        ]
        assert find_turn_user(messages, 1) == -1

    def test_turn_user_empty_messages(self):
        """空列表防御（idx 无效时不抛 IndexError）"""
        assert find_turn_user([], -1) == -1

    def test_turn_user_old_format(self):
        """旧格式 [msg_dict, extra] 对 → 正确提取 role"""
        messages = [
            [{"role": "user", "id": "u1"}, {}],
            [{"role": "assistant", "id": "a1"}, {}],
        ]
        assert find_turn_user(messages, 1) == 0

    def test_turn_end_until_next_user(self):
        """一轮 = user 起点到下一条 user 前（含工具链 system）"""
        messages = [
            {"role": "user", "id": "u1"},
            {"role": "assistant", "id": "a1"},
            {"role": "system", "id": "t1"},
            {"role": "assistant", "id": "a2"},
            {"role": "user", "id": "u2"},
        ]
        assert find_turn_end(messages, 0) == 4

    def test_turn_end_last_round(self):
        """最后一轮（后面没有 user）→ 开区间右界 = len(messages)"""
        messages = [
            {"role": "user", "id": "u1"},
            {"role": "assistant", "id": "a1"},
        ]
        assert find_turn_end(messages, 0) == 2

    def test_turn_end_single_user(self):
        """user 后没有其他消息 → end = 1"""
        messages = [{"role": "user", "id": "u1"}]
        assert find_turn_end(messages, 0) == 1

    def test_turn_end_old_format(self):
        """旧格式 [msg_dict, extra] 对 → 正确提取 role"""
        messages = [
            [{"role": "user", "id": "u1"}, {}],
            [{"role": "assistant", "id": "a1"}, {}],
            [{"role": "user", "id": "u2"}, {}],
        ]
        assert find_turn_end(messages, 0) == 2


# ======================================================================
# assistant 区间匹配（v2.5.3）：created_at ~ completed_at 自适应区间
# ======================================================================


class TestAssistantRangeMatch:
    """role=assistant 走区间匹配：msg.created_at ∈ [created_at, completed_at+容差]。

    数据取自 2026-11-11 作者实测：发送 00:16:37、首段保存 00:16:46.19
    （漂移 8.8s，渐进窗口 ±3s 搞不定）、完成 00:17:08.34。
    """

    @pytest.mark.asyncio
    async def test_range_match_long_drift(self, operator):
        """漂移 8.8s 的实测场景：区间匹配命中，渐进窗口根本够不到"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:37.353803", role="user", idx=0),
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-11-10T16:16:37+00:00",      # 前端 ISO UTC 形态
            role="assistant",
            completed_at="2026-11-10T16:17:08+00:00",
        )
        assert idx == 1
        assert msg["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_range_match_unix_created_at(self, operator):
        """created_at 用 Unix 秒形态（user 实测形态）同样可做区间起点"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:37.353803", role="user", idx=0),
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=1),
        ]
        # 1794327397 = UTC 2026-11-10T16:16:37Z = 本地 00:16:37
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="1794327397",
            role="assistant",
            completed_at="2026-11-10T16:17:08+00:00",
        )
        assert idx == 1

    @pytest.mark.asyncio
    async def test_range_end_tolerance_covers_truncation_gap(self, operator):
        """右边界 +1s 容差：快回复（完成<1s）时首段保存落进整秒截断缝隙

        发送 09:00:00.35、首段保存 09:00:01.10、真实完成 09:00:01.34 →
        completed_at 整秒截断为 2026-11-11T01:00:01+00:00（UTC）。无容差时
        右边界 01:00:01.000 < 01:00:01.10 会漏；+1s 容差命中。
        """
        operator.get_messages.return_value = [
            make_msg("2026-11-11T09:00:00.350000", role="user", idx=0),
            make_msg("2026-11-11T09:00:01.100000", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-11-11T01:00:00+00:00",
            role="assistant",
            completed_at="2026-11-11T01:00:01+00:00",
        )
        assert idx == 1

    @pytest.mark.asyncio
    async def test_range_excludes_user_message(self, operator):
        """user 消息（created_at=发送时刻）落在区间左端，role 过滤排除"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:37.353803", role="user", idx=0),
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-11-10T16:16:37+00:00",
            role="assistant",
            completed_at="2026-11-10T16:17:08+00:00",
        )
        # user 的 37.354 ≥ 区间起点 37.000，但 role != assistant 被排除
        assert idx == 1
        assert msg["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_range_prev_round_not_matched(self, operator):
        """左边界不放宽：上一轮助手消息（早于区间起点）不误命中"""
        operator.get_messages.return_value = [
            make_msg("2026-11-10T23:59:30.000000", role="assistant", idx=0),
            make_msg("2026-11-11T00:16:37.353803", role="user", idx=1),
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=2),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-11-10T16:16:37+00:00",
            role="assistant",
            completed_at="2026-11-10T16:17:08+00:00",
        )
        assert idx == 2

    @pytest.mark.asyncio
    async def test_range_ambiguous_raises(self, operator):
        """区间内两条 assistant 消息 → ValueError（真歧义）"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:45.000000", role="assistant", idx=0),
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=1),
        ]
        with pytest.raises(ValueError, match="歧义"):
            await operator.find_message_by_created_at(
                "sid", "uid", "ch",
                created_at="2026-11-10T16:16:37+00:00",
                role="assistant",
                completed_at="2026-11-10T16:17:08+00:00",
            )

    @pytest.mark.asyncio
    async def test_range_no_match(self, operator):
        """区间内无 assistant 消息 → (-1, None)"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:37.353803", role="user", idx=0),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-11-10T16:16:37+00:00",
            role="assistant",
            completed_at="2026-11-10T16:17:08+00:00",
        )
        assert idx == -1
        assert msg is None

    @pytest.mark.asyncio
    async def test_missing_completed_at_raises_dev_hint(self, operator):
        """completed_at 缺失 → ValueError 提示开发者（官方结构变更可能）"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=0),
        ]
        with pytest.raises(ValueError, match="开发者"):
            await operator.find_message_by_created_at(
                "sid", "uid", "ch",
                created_at="2026-11-10T16:16:37+00:00",
                role="assistant",
                completed_at="",
            )

    @pytest.mark.asyncio
    async def test_whitespace_completed_at_raises(self, operator):
        """completed_at 为纯空白 → 同样报错"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=0),
        ]
        with pytest.raises(ValueError, match="开发者"):
            await operator.find_message_by_created_at(
                "sid", "uid", "ch",
                created_at="2026-11-10T16:16:37+00:00",
                role="assistant",
                completed_at="   ",
            )

    @pytest.mark.asyncio
    async def test_unparseable_completed_at_raises(self, operator):
        """completed_at 无法解析 → ValueError 提示检查官方结构"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=0),
        ]
        with pytest.raises(ValueError, match="无法解析"):
            await operator.find_message_by_created_at(
                "sid", "uid", "ch",
                created_at="2026-11-10T16:16:37+00:00",
                role="assistant",
                completed_at="not-a-time",
            )

    @pytest.mark.asyncio
    async def test_user_role_still_progressive_window(self, operator):
        """回归：role=user 不受区间逻辑影响，仍走渐进窗口 ±1s 命中"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:37.353803", role="user", idx=0),
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="1794327397",
            role="user",
        )
        assert idx == 0
        assert msg["role"] == "user"


# ======================================================================
# 覆盖洞补充（v2.5.5，对抗复审 P3-3）
# ======================================================================


class TestCoverageHoles:
    """复审发现的测试覆盖洞：role="" 显式路径 / naive completed_at / 倒挂区间"""

    @pytest.mark.asyncio
    async def test_empty_role_uses_progressive_window(self, operator):
        """显式传 role=""：不做角色过滤，走渐进窗口命中时间匹配的消息"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:37.353803", role="user", idx=0),
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="1794327397",  # 本地 00:16:37，±1s 内只有 user
            role="",
        )
        assert idx == 0
        assert msg["role"] == "user"

    @pytest.mark.asyncio
    async def test_naive_completed_at_treated_as_utc(self, operator):
        """completed_at 传 naive 无时区 ISO：按 UTC 解释（前端约定）

        completed_at = "2026-11-10T16:20:00"（naive）：
        - 按 UTC 解释 → 区间 [16:16:37, 16:21:00] 包含目标 16:16:46.19 → 命中
        - 若误按本地解释 → UTC 08:20:00 → 区间起点>终点 → 不命中
        断言命中即锁定「naive 按 UTC 解释」行为。
        """
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:37.353803", role="user", idx=0),
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-11-10T16:16:37+00:00",
            role="assistant",
            completed_at="2026-11-10T16:20:00",
        )
        assert idx == 1
        assert msg["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_inverted_range_returns_not_found(self, operator):
        """病态输入：completed_at < created_at（区间倒挂）→ 静默 (-1, None) → 404"""
        operator.get_messages.return_value = [
            make_msg("2026-11-11T00:16:37.353803", role="user", idx=0),
            make_msg("2026-11-11T00:16:46.191835", role="assistant", idx=1),
        ]
        idx, msg = await operator.find_message_by_created_at(
            "sid", "uid", "ch",
            created_at="2026-11-10T16:17:00+00:00",
            role="assistant",
            completed_at="2026-11-10T16:16:00+00:00",  # 早于起点
        )
        assert idx == -1
        assert msg is None
