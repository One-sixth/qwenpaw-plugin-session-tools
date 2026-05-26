"""会话增强工具插件 — 提供 /fork、/rewind、/regen 命令"""

import logging

from agentscope.message import Msg, TextBlock
from qwenpaw.plugins.api import PluginApi

from .session_ops import SessionOperator

logger = logging.getLogger(__name__)

# 按 agent_id 缓存 SessionOperator，每个 agent 各自独立
_operators: dict = {}


def _parse_args(text: str):
    parts = text.strip().split()
    return parts[1:] if len(parts) > 1 else []


async def _handle_fork(op, session_id, user_id, channel, chat_manager, args):
    n = int(args[0]) if args else 0
    new_id = await op.fork(
        source_id=session_id,
        user_id=user_id,
        channel=channel,
        n_rounds=n,
    )

    fork_name = None

    # 注册新会话到 chat_manager，让 WebUI 能看见
    if chat_manager:
        from datetime import datetime, timezone
        from qwenpaw.app.runner.models import ChatSpec

        # 获取原会话名称
        original_name = "Chat"
        try:
            original_spec = await chat_manager.get_or_create_chat(
                session_id, user_id, channel,
            )
            original_name = original_spec.name or "Chat"
        except Exception:
            pass

        # 计算 Fork 编号：查找已有 Fork #{N} 原名称
        prefix = "Fork #"
        suffix = f" {original_name}"
        max_n = 0
        for c in await chat_manager.list_chats(user_id=user_id, channel=channel):
            name = c.name or ""
            if name.startswith(prefix) and name.endswith(suffix):
                middle = name[len(prefix):-len(suffix)]
                try:
                    max_n = max(max_n, int(middle))
                except ValueError:
                    pass

        fork_name = f"{prefix}{max_n + 1}{suffix}"

        spec = ChatSpec(
            name=fork_name,
            session_id=new_id,
            user_id=user_id,
            channel=channel,
            created_at=datetime.now(timezone.utc),
            meta={"forked_from": session_id, "n_rounds": n},
        )
        await chat_manager.create_chat(spec)
        logger.debug("已注册新会话: %s (%s)", fork_name, new_id)

    if fork_name:
        return f"✅ 已分叉新会话: `{fork_name}`"
    return f"✅ 已分叉新会话: `{new_id}`"


async def _handle_rewind(op, session_id, user_id, channel, args):
    n = int(args[0]) if args else 1
    deleted = await op.rollback(
        session_id=session_id,
        user_id=user_id,
        channel=channel,
        n_rounds=n,
    )
    return (
        f"⏪ 已撤销 {deleted} 条消息（{n} 轮对话）\n\n"
        f"↪ 会话已回退，请**刷新页面**查看最新状态。"
    )


async def _handle_regen(op, session_id, user_id, channel):
    """删除最后一轮并返回最后用户消息文本。"""
    return await op.trim_last_round(session_id, user_id, channel)


def _patch_handler():
    from qwenpaw.app.runner.runner import AgentRunner

    original = AgentRunner.query_handler

    async def patched(self, msgs, request=None, **kwargs):
        global _operators

        # 按 agent_id 获取/创建对应工作区的 SessionOperator
        agent_id = self.agent_id
        if agent_id not in _operators:
            _operators[agent_id] = SessionOperator(self.workspace_dir)
        operator = _operators[agent_id]

        # 检查最后一条消息是否为命令
        if msgs and len(msgs) > 0:
            last = msgs[-1]
            text = None
            if hasattr(last, "get_text_content"):
                text = last.get_text_content()
            elif isinstance(last.content, str):
                text = last.content
            elif isinstance(last.content, list):
                for block in last.content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        break

            if text and text.startswith("/"):
                cmd = text.split()[0].lower()
                args = _parse_args(text)
                logger.info("收到命令: %s (session=%s)", text.strip(), request.session_id)

                try:
                    if cmd == "/fork":
                        resp = await _handle_fork(
                            operator,
                            request.session_id,
                            request.user_id,
                            request.channel,
                            self._chat_manager,
                            args,
                        )
                        msg = Msg(
                            name=self.agent_name,
                            role="assistant",
                            content=[TextBlock(type="text", text=resp)],
                        )
                        yield msg, True
                        return

                    if cmd == "/rewind":
                        resp = await _handle_rewind(
                            operator,
                            request.session_id,
                            request.user_id,
                            request.channel,
                            args,
                        )
                        msg = Msg(
                            name=self.agent_name,
                            role="assistant",
                            content=[TextBlock(type="text", text=resp)],
                        )
                        yield msg, True
                        return

                    if cmd == "/regen":
                        last_user_text = await _handle_regen(
                            operator,
                            request.session_id,
                            request.user_id,
                            request.channel,
                        )
                        # 替换命令文本为原用户消息，走正常 AI 流程
                        modified_msgs = list(msgs)
                        if modified_msgs:
                            last_msg = modified_msgs[-1]
                            if isinstance(last_msg.content, str):
                                last_msg.content = last_user_text
                            elif isinstance(last_msg.content, list):
                                found = False
                                for block in last_msg.content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        block["text"] = last_user_text
                                        found = True
                                        break
                                if not found:
                                    last_msg.content.append(
                                        {"type": "text", "text": last_user_text}
                                    )

                        async for msg, last in original(self, modified_msgs, request, **kwargs):
                            yield msg, last
                        return

                except Exception as e:
                    logger.exception("命令执行失败: %s", cmd)
                    msg = Msg(
                        name=self.agent_name,
                        role="assistant",
                        content=[TextBlock(type="text", text=f"❌ 错误: {e}")],
                    )
                    yield msg, True
                    return

        # 非命令消息 → 交给原始 handler
        async for r in original(self, msgs, request, **kwargs):
            yield r

    AgentRunner.query_handler = patched
    logger.info("插件已注册: /fork, /rewind, /regen")


class Plugin:
    def register(self, api: PluginApi):
        api.register_startup_hook(
            "session_tools_patch",
            _patch_handler,
            priority=50,
        )


plugin = Plugin()
