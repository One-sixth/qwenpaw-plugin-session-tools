"""测试 find_message_by_created_at 的匹配逻辑。

运行方式：
    pip install pytest pytest-asyncio
    pytest test_find_message.py -v
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
from datetime import datetime, timezone, timedelta

from session_ops import SessionOperator

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
        """同秒内有多个消息，从后往前匹配到最新的那条"""
        # 从后往前遍历，msg_001 先被加入 msg_entries，然后 msg_000
        # 严格匹配筛选出两条，报错，而不是返回最后一条
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
            make_msg("2026-07-20T19:59:28.654321", role="assistant", idx=1),
        ]
        with pytest.raises(ValueError, match="多个同一时间的匹配"):
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
        """同一秒内有多条消息 → 严格匹配报错"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:28.123456", role="user", idx=0),
            make_msg("2026-07-20T19:59:28.654321", role="assistant", idx=1),
        ]
        with pytest.raises(ValueError, match="多个同一时间的匹配"):
            await operator.find_message_by_created_at(
                "sid", "uid", "ch",
                created_at="2026-07-20T11:59:28+00:00",
            )

    @pytest.mark.asyncio
    async def test_fuzzy_multiple(self, operator):
        """±1秒内有多条消息且无严格匹配 → 宽松匹配报错"""
        operator.get_messages.return_value = [
            make_msg("2026-07-20T19:59:29.000000", role="user", idx=0),  # target+1
            make_msg("2026-07-20T19:59:31.000000", role="assistant", idx=1),  # target+3
        ]
        # 目标 19:59:30（上海时间）
        # 严格匹配 19:59:30 → 0 条
        # 宽松匹配 ±1秒 → 19:59:29（差1秒）和 19:59:31（差1秒）→ 2 条
        with pytest.raises(ValueError, match="多个相近时间的匹配"):
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