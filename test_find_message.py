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


# ======================================================================
# scroll 簿记继承修复（v2.5.6）
# ======================================================================

import sqlite3

from session_ops import (
    SCROLL_PERSIST_KEYS,
    RECALL_TOOL_NAMES,
    _copy_history_rows,
    _remap_scroll_index,
)

_DB_SCHEMA = """
CREATE TABLE conversation_history (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    agent_id     TEXT,
    kind         TEXT NOT NULL,
    role         TEXT,
    name         TEXT,
    content      TEXT,
    tool_call_id TEXT,
    tool_input   TEXT,
    tool_state   TEXT,
    headline     TEXT,
    blocks       TEXT,
    metadata     TEXT,
    created_at   TEXT,
    dedup_key    TEXT
);
CREATE UNIQUE INDEX ux_dedup ON conversation_history(session_id, dedup_key);
CREATE VIRTUAL TABLE conversation_history_fts USING fts5(
    content, content='conversation_history', content_rowid='seq'
);
"""


def _make_db(path, parent_sid, n_normal=3, n_recall=1):
    """建临时 history.db：父会话 n_normal 条普通行 + n_recall 条 recall 工具行。"""
    con = sqlite3.connect(str(path))
    con.executescript(_DB_SCHEMA)
    for i in range(n_normal):
        con.execute(
            "INSERT INTO conversation_history "
            "(session_id, kind, role, name, content, dedup_key, created_at) "
            "VALUES (?, 'model_turn', 'user', 'user', ?, ?, ?)",
            (parent_sid, f"content-{i}", f"dk-{i}", f"2026-11-11T00:00:0{i}"),
        )
    for i in range(n_recall):
        con.execute(
            "INSERT INTO conversation_history "
            "(session_id, kind, role, name, content, dedup_key, created_at) "
            "VALUES (?, 'context_msg', 'tool', 'recall_history', ?, ?, ?)",
            (parent_sid, f"recall-{i}", f"recall-dk-{i}", f"2026-11-11T00:01:0{i}"),
        )
    con.commit()
    con.close()


def _make_scroll_state(parent_sid: str) -> dict:
    """构造带 scroll checkpoint 的会话 state（含已被压缩驱逐的 index）。"""
    return {
        "session_id": parent_sid,
        "agent": {
            "state": {"context": [make_msg("2026-11-11T00:00:00", idx=99)]},
            "scroll": {
                "token_estimate": {"margin": 0, "model_key": "test", "context_size": 1000},
                "persisted_ids": ["a", "b"],
                "persisted_tcids": ["call_1"],
                "seen_tool_result_ids": ["call_1"],
                "seen_thinking_block_ids": ["t1"],
                "folded_thinking_block_ids": [],
                "seq_by_tcid": {"call_1": 2},
                "synthetic_ids": [],
                "seq_by_id": {"a": [1, 1], "b": [2, 2]},
                "model_turn_seq": {"a": 1, "b": 2},
                "model_turn_nblk": {"a": 1, "b": 2},
                "leaf_by_id": {"a": [1, "headline-a"]},
                # index 指向父会话库行的 seq（1~2 在父行范围内）
                "index": {
                    "session_id": parent_sid,
                    "agent_id": "default",
                    "tiers": [
                        [
                            {
                                "seq_lo": 1,
                                "seq_hi": 2,
                                "lines": [[1, 1, "head-a", "head-a"]],
                            }
                        ]
                    ],
                },
                "continuation_summary": None,
                "summary_update_failed": False,
            },
        },
    }


class TestCopyHistoryRows:
    """_copy_history_rows：库行复制 / FTS 排除 / 幂等 / 映射表"""

    def test_copy_and_seq_map(self, tmp_path):
        db = tmp_path / "history.db"
        _make_db(db, "parent-sid", n_normal=3, n_recall=1)
        seq_map = _copy_history_rows(db, "parent-sid", "fork-fid")
        con = sqlite3.connect(str(db))
        # fork 名下 4 行（3 普通 + 1 recall，recall 行也复制——只是不进 FTS）
        assert con.execute(
            "SELECT COUNT(*) FROM conversation_history WHERE session_id='fork-fid'"
        ).fetchone()[0] == 4
        # 映射表覆盖全部有 dedup_key 的行
        assert len(seq_map) == 4
        # 新旧 seq 不重叠（AUTOINCREMENT 续接）
        assert set(seq_map.values()).isdisjoint(set(seq_map.keys()))
        # FTS：recall 工具行排除，普通行在。
        # ⚠️ 不能用 SELECT rowid FROM fts 判断——external content 全扫描会
        # 回读主表（父行未入索引也会出现）；用 fts_docsize（与宿主
        # history.py:504 判索引方式一致）
        fts_rows = {
            r[0] for r in con.execute("SELECT id FROM conversation_history_fts_docsize")
        }
        fork_recall = con.execute(
            "SELECT seq FROM conversation_history WHERE session_id='fork-fid' AND dedup_key='recall-dk-0'"
        ).fetchone()[0]
        fork_normal = con.execute(
            "SELECT seq FROM conversation_history WHERE session_id='fork-fid' AND dedup_key='dk-0'"
        ).fetchone()[0]
        assert fork_recall not in fts_rows
        assert fork_normal in fts_rows
        con.close()

    def test_idempotent_second_call(self, tmp_path):
        db = tmp_path / "history.db"
        _make_db(db, "parent-sid", n_normal=3, n_recall=1)
        _copy_history_rows(db, "parent-sid", "fork-fid")
        seq_map2 = _copy_history_rows(db, "parent-sid", "fork-fid")
        con = sqlite3.connect(str(db))
        # 二次调用跳过 INSERT，行数不翻倍；映射表仍完整
        assert con.execute(
            "SELECT COUNT(*) FROM conversation_history WHERE session_id='fork-fid'"
        ).fetchone()[0] == 4
        assert len(seq_map2) == 4
        con.close()

    def test_parent_missing_rows_empty_map(self, tmp_path):
        db = tmp_path / "history.db"
        _make_db(db, "other-sid")
        seq_map = _copy_history_rows(db, "parent-sid", "fork-fid")
        assert seq_map == {}
        con = sqlite3.connect(str(db))
        assert con.execute(
            "SELECT COUNT(*) FROM conversation_history WHERE session_id='fork-fid'"
        ).fetchone()[0] == 0
        con.close()


class TestRemapScrollIndex:
    """_remap_scroll_index：index seq 重映射 / 保留键不动 / 空映射安全"""

    def _scroll(self):
        return _make_scroll_state("parent-sid")["agent"]["scroll"]

    def test_remap_all_points(self, tmp_path):
        db = tmp_path / "history.db"
        _make_db(db, "parent-sid", n_normal=3, n_recall=1)
        seq_map = _copy_history_rows(db, "parent-sid", "fork-fid")
        scroll = self._scroll()
        touched = _remap_scroll_index(scroll, seq_map, "fork-fid")
        idx = scroll["index"]
        assert idx["session_id"] == "fork-fid"
        block = idx["tiers"][0][0]
        # seq_lo=1, seq_hi=2 都有映射 → 换成 fork 新 seq
        assert block["seq_lo"] == seq_map[1]
        assert block["seq_hi"] == seq_map[2]
        # lines 的 [lo, hi, head, tail] 前两位重映射，head/tail 不动
        line = block["lines"][0]
        assert line[0] == seq_map[1]
        assert line[2] == "head-a"
        assert touched >= 3  # seq_lo + seq_hi + line lo + line hi 中可映射项

    def test_unmapped_seq_kept(self):
        scroll = self._scroll()
        # 空映射 → 原样保留
        touched = _remap_scroll_index(scroll, {}, "fork-fid")
        assert touched == 0
        assert scroll["index"]["tiers"][0][0]["seq_lo"] == 1

    def test_no_index_safe(self):
        scroll = {"persisted_ids": []}
        assert _remap_scroll_index(scroll, {1: 9}, "fork-fid") == 0


class TestForkScrollInheritance:
    """fork 端到端：簿记清空 + 保留键原样 + 库行复制 + index 重映射"""

    @pytest.mark.asyncio
    async def test_fork_full_chain(self, tmp_path):
        db = tmp_path / "history.db"
        _make_db(db, "parent-sid", n_normal=3, n_recall=1)

        op = SessionOperator.__new__(SessionOperator)
        op.workspace_dir = tmp_path
        op.session = MagicMock()
        op.session.get_session_state_dict = AsyncMock(
            return_value=_make_scroll_state("parent-sid")
        )
        saved = {}

        async def fake_save(session_id, user_id="", channel="", **modules):
            saved["sid"] = session_id
            saved["modules"] = modules

        op.session.save_session_state = fake_save

        new_id = await op.fork(
            "parent-sid", "uid", "console", 0,
            messages=[make_msg("2026-11-11T00:00:00", idx=99)],
            new_id="fork-fid",
        )
        assert new_id == "fork-fid"
        assert saved["sid"] == "fork-fid"

        scroll = saved["modules"]["agent"].data["scroll"]
        # 持久化判定位全部清除
        for key in SCROLL_PERSIST_KEYS:
            assert key not in scroll, f"{key} 应被清除"
        # 折叠状态 / 合成占位符 / token 估算 / summary 标记保留
        assert scroll["seen_tool_result_ids"] == ["call_1"]
        assert scroll["seen_thinking_block_ids"] == ["t1"]
        assert scroll["folded_thinking_block_ids"] == []
        assert scroll["synthetic_ids"] == []
        assert scroll["token_estimate"]["context_size"] == 1000
        assert scroll["summary_update_failed"] is False
        # index 重映射为 fork 新 seq
        assert scroll["index"]["session_id"] == "fork-fid"

        # 库行复制生效
        con = sqlite3.connect(str(db))
        assert con.execute(
            "SELECT COUNT(*) FROM conversation_history WHERE session_id='fork-fid'"
        ).fetchone()[0] == 4
        con.close()

    @pytest.mark.asyncio
    async def test_fork_db_failure_degrades(self, tmp_path):
        """history.db 复制失败（库不存在/坏表）→ fork 照常完成，簿记仍清空"""
        op = SessionOperator.__new__(SessionOperator)
        op.workspace_dir = tmp_path  # 无 history.db 文件 → db_path.exists() False
        op.session = MagicMock()
        op.session.get_session_state_dict = AsyncMock(
            return_value=_make_scroll_state("parent-sid")
        )
        saved = {}

        async def fake_save(session_id, user_id="", channel="", **modules):
            saved["modules"] = modules

        op.session.save_session_state = fake_save

        new_id = await op.fork(
            "parent-sid", "uid", "console", 0,
            messages=[make_msg("2026-11-11T00:00:00", idx=99)],
            new_id="fork-fid",
        )
        assert new_id == "fork-fid"
        scroll = saved["modules"]["agent"].data["scroll"]
        for key in SCROLL_PERSIST_KEYS:
            assert key not in scroll
        # index 未重映射（保留父值），但不阻塞 fork
        assert scroll["index"]["session_id"] == "parent-sid"

    @pytest.mark.asyncio
    async def test_fork_old_format_no_scroll(self, tmp_path):
        """old fmt（无 scroll 键）→ 不炸、照常 fork"""
        state = {
            "session_id": "parent-sid",
            "agent": {"memory": {"content": [make_old_msg("2026-11-11T00:00:00")]}},
        }
        op = SessionOperator.__new__(SessionOperator)
        op.workspace_dir = tmp_path
        op.session = MagicMock()
        op.session.get_session_state_dict = AsyncMock(return_value=state)
        saved = {}

        async def fake_save(session_id, user_id="", channel="", **modules):
            saved["modules"] = modules

        op.session.save_session_state = fake_save

        new_id = await op.fork(
            "parent-sid", "uid", "console", 0,
            messages=[make_old_msg("2026-11-11T00:00:00")],
            new_id="fork-fid",
        )
        assert new_id == "fork-fid"
        assert "scroll" not in saved["modules"]["agent"].data


class TestForkDegradePaths:
    """复审补充（v2.5.6）：真实降级路径覆盖——库坏/数据病态时 fork 不阻塞"""

    @pytest.mark.asyncio
    async def test_fork_broken_db_degrades(self, tmp_path):
        """表存在但 FTS 虚表缺失（宿主 schema 变更模拟）→ sqlite3.Error →
        fork 照常完成、index 未重映射（与 A 复审员指出的零覆盖路径对应）"""
        db = tmp_path / "history.db"
        con = sqlite3.connect(str(db))
        # 只建主表，不建 FTS 表
        con.executescript(_DB_SCHEMA.split("CREATE VIRTUAL TABLE")[0])
        con.execute(
            "INSERT INTO conversation_history "
            "(session_id, kind, role, name, content, dedup_key, created_at) "
            "VALUES ('parent-sid', 'model_turn', 'user', 'user', 'c', 'dk-0', 't')"
        )
        con.commit()
        con.close()

        op = SessionOperator.__new__(SessionOperator)
        op.workspace_dir = tmp_path
        op.session = MagicMock()
        op.session.get_session_state_dict = AsyncMock(
            return_value=_make_scroll_state("parent-sid")
        )
        saved = {}

        async def fake_save(session_id, user_id="", channel="", **modules):
            saved["modules"] = modules

        op.session.save_session_state = fake_save

        new_id = await op.fork(
            "parent-sid", "uid", "console", 0,
            messages=[make_msg("2026-11-11T00:00:00", idx=99)],
            new_id="fork-fid",
        )
        assert new_id == "fork-fid"  # fork 本身不失败
        scroll = saved["modules"]["agent"].data["scroll"]
        # 复制失败 → index 未重映射（保留父值），但簿记清空已生效
        assert scroll["index"]["session_id"] == "parent-sid"

    @pytest.mark.asyncio
    async def test_fork_duplicate_dedup_key_degrades(self, tmp_path):
        """父库存在重复 dedup_key（绕过唯一约束写入的病态数据）→
        INSERT SELECT 撞 ux_dedup → sqlite3.IntegrityError → fork 降级不阻塞"""
        db = tmp_path / "history.db"
        con = sqlite3.connect(str(db))
        con.executescript(_DB_SCHEMA.split("CREATE VIRTUAL TABLE")[0])
        # 手动写两行同 (session_id, dedup_key) —— 先关索引模拟脏数据
        con.execute("DROP INDEX ux_dedup")
        for i in range(2):
            con.execute(
                "INSERT INTO conversation_history "
                "(session_id, kind, role, name, content, dedup_key, created_at) "
                "VALUES ('parent-sid', 'model_turn', 'user', 'user', ?, 'dup-0', ?)",
                (f"c{i}", f"t{i}"),
            )
        con.commit()
        con.close()

        op = SessionOperator.__new__(SessionOperator)
        op.workspace_dir = tmp_path
        op.session = MagicMock()
        op.session.get_session_state_dict = AsyncMock(
            return_value=_make_scroll_state("parent-sid")
        )
        saved = {}

        async def fake_save(session_id, user_id="", channel="", **modules):
            saved["modules"] = modules

        op.session.save_session_state = fake_save

        new_id = await op.fork(
            "parent-sid", "uid", "console", 0,
            messages=[make_msg("2026-11-11T00:00:00", idx=99)],
            new_id="fork-fid",
        )
        assert new_id == "fork-fid"  # 降级：fork 照常完成
        scroll = saved["modules"]["agent"].data["scroll"]
        assert scroll["index"]["session_id"] == "parent-sid"  # 未重映射


class TestRemapMalformedIndex:
    """复审补充（v2.5.6）：index 病态结构防御（A 复审员 P2-1）"""

    def test_short_line_skipped(self):
        """lines 元素短于 2 位 → 跳过不崩，其余行照常重映射"""
        scroll = {
            "index": {
                "session_id": "p",
                "tiers": [[
                    {"seq_lo": 1, "seq_hi": 2,
                     "lines": [[10], 11, None, ["x"], [1, 2, "head", "tail"]]},
                ]],
            }
        }
        touched = _remap_scroll_index(scroll, {1: 100, 2: 200}, "f")
        block = scroll["index"]["tiers"][0][0]
        assert block["seq_lo"] == 100 and block["seq_hi"] == 200
        # [10] 太短跳过；11 非 list 跳过；None 跳过；["x"] 太短跳过
        assert block["lines"][4] == [100, 200, "head", "tail"]
        assert touched == 4  # seq_lo + seq_hi + 好行的 lo/hi

    def test_tiers_none_and_bad_tier(self):
        """tiers 为 None / tier 非 list → 不崩返回 0"""
        scroll = {"index": {"session_id": "p", "tiers": None}}
        assert _remap_scroll_index(scroll, {1: 2}, "f") == 0
        scroll = {"index": {"session_id": "p", "tiers": [None, "x"]}}
        assert _remap_scroll_index(scroll, {1: 2}, "f") == 0
