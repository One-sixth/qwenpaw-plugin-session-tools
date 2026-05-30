"""会话增强工具插件 — 提供 /fork、/rewind、/regen 命令 + HTTP API"""

import json
import logging
from pathlib import Path

from agentscope.message import Msg, TextBlock
from fastapi import APIRouter, HTTPException, Query
from qwenpaw.plugins.api import PluginApi

from .session_ops import SessionOperator

logger = logging.getLogger(__name__)

# 按 agent_id 缓存 SessionOperator，每个 agent 各自独立
_operators: dict = {}

# HTTP 路由器
router = APIRouter()


def _get_workspace_dir() -> Path:
    """从 QwenPaw 配置获取默认 agent 的工作区路径。"""
    try:
        from qwenpaw.config.utils import load_config
        config = load_config()
        for agent_ref in config.agents:
            if agent_ref.agent_id == "default":
                return Path(agent_ref.workspace_dir).expanduser().resolve()
        if config.agents:
            return Path(config.agents[0].workspace_dir).expanduser().resolve()
    except Exception:
        pass
    return Path.home() / ".qwenpaw" / "workspaces" / "default"


def _ensure_operator(agent_id: str = "default") -> SessionOperator:
    """获取或创建对应智能体的 SessionOperator。"""
    global _operators
    if agent_id not in _operators:
        _operators[agent_id] = SessionOperator(_get_workspace_dir())
    return _operators[agent_id]


def _get_msg_role(msg) -> str:
    """提取消息的角色字段。"""
    if isinstance(msg, list) and len(msg) > 0 and isinstance(msg[0], dict):
        return msg[0].get("role", "")
    if isinstance(msg, dict):
        return msg.get("role", "")
    return ""


def _dom_to_file_index(messages: list, dom_index: int) -> int:
    """将 DOM 气泡索引（倒序）映射到文件消息索引（正序）。

    DOM 按 order-desc 渲染（新在上，index 0 = 最新），
    文件按时间正序存储（index 0 = 最早），所以反向映射。
    """
    visible_indices = []
    for i, msg in enumerate(messages):
        role = _get_msg_role(msg)
        if role in ("user", "assistant"):
            visible_indices.append(i)
    if dom_index < 0 or dom_index >= len(visible_indices):
        return -1
    return visible_indices[-(dom_index + 1)]


def _find_round_user_idx(messages: list, file_idx: int) -> int:
    """从文件索引往回找，找到所属轮次的用户消息索引（轮次起始位置）。"""
    for i in range(file_idx, -1, -1):
        if _get_msg_role(messages[i]) == "user":
            return i
    return -1


def _get_round_end(messages: list, round_user_idx: int) -> int:
    """找到某一轮对话的结束位置（不含），即下一个用户消息或文件末尾。"""
    for i in range(round_user_idx + 1, len(messages)):
        if _get_msg_role(messages[i]) == "user":
            return i
    return len(messages)


def _try_get_chat_manager():
    """尝试从运行中的 QwenPaw app 获取 chat_manager。"""
    try:
        from qwenpaw.app.app import app
        from qwenpaw.config.context import get_current_workspace_dir
        ws_dir = get_current_workspace_dir()
        if ws_dir and ws_dir in app.state.workspaces:
            ws = app.state.workspaces[ws_dir]
            return ws.chat_manager
    except Exception:
        pass
    return None


# ── HTTP 路由 ────────────────────────────────────────────────


@router.post("/session/{session_id}/rewind")
async def http_rewind(
    session_id: str,
    to_message_index: int = Query(...),
    user_id: str = Query(...),
    channel: str = Query(...),
):
    """回退到指定气泡所在轮次的开头，删除该轮及之后的所有消息。

    例如点击倒数第二条用户消息的回退按钮：
    - 保留该用户消息及之前的所有历史
    - 删除该用户消息触发的所有 AI 响应（工具调用、思考、最终回答）
    """
    try:
        op = _ensure_operator()
        messages = await op.get_messages(session_id, user_id, channel)
        file_idx = _dom_to_file_index(messages, to_message_index)
        if file_idx < 0:
            raise HTTPException(status_code=404,
                                detail=f"未找到索引 {to_message_index} 对应的消息")

        # 找到该消息所属轮次的用户消息（轮次起点）
        round_user_idx = _find_round_user_idx(messages, file_idx)
        if round_user_idx < 0:
            raise HTTPException(status_code=400,
                                detail="无法定位所属对话轮次")

        # 保留到轮次起点（含），删除该轮之后的所有内容
        new_messages = messages[:round_user_idx + 1]
        deleted = len(messages) - len(new_messages)
        await op._update_content(session_id, user_id, channel, new_messages)
        return {"success": True, "deleted": deleted, "remaining": len(new_messages)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except Exception as e:
        logger.exception("回退失败: %s", session_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/{session_id}/fork")
async def http_fork(
    session_id: str,
    to_message_index: int = Query(...),
    user_id: str = Query(...),
    channel: str = Query(...),
):
    """从指定气泡所在轮次的起点分叉，复制之前所有历史到新会话。"""
    try:
        op = _ensure_operator()
        messages = await op.get_messages(session_id, user_id, channel)
        file_idx = _dom_to_file_index(messages, to_message_index)
        if file_idx < 0:
            raise HTTPException(status_code=404,
                                detail=f"未找到索引 {to_message_index} 对应的消息")

        # 找到所属轮次的用户消息，以此截止
        round_user_idx = _find_round_user_idx(messages, file_idx)
        if round_user_idx < 0:
            raise HTTPException(status_code=400,
                                detail="无法定位所属对话轮次")
        truncated = messages[:round_user_idx + 1]

        from datetime import datetime
        new_id = f"{session_id}_fork_{datetime.now():%Y%m%d_%H%M%S}"
        state = await op._load_state(session_id, user_id, channel)
        state["session_id"] = new_id
        state["agent"]["memory"]["content"] = truncated
        new_file = op._get_session_file_path(new_id, user_id, channel)
        new_file.parent.mkdir(parents=True, exist_ok=True)
        with open(new_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

        # 注册到 chat_manager，写入 chats.json 让 WebUI 左侧菜单能看见
        chat_manager = _try_get_chat_manager()
        if chat_manager:
            try:
                await chat_manager.get_or_create_chat(
                    session_id=new_id,
                    user_id=user_id,
                    channel=channel,
                    name=f"Fork #{to_message_index + 1}",
                    source="chat",
                )
            except Exception as e:
                logger.warning("注册分叉会话到 chat_manager 失败: %s", e)

        return {"success": True, "new_session_id": new_id, "messages": len(truncated)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except Exception as e:
        logger.exception("分叉失败: %s", session_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/session/{session_id}/message/{msg_index}")
async def http_delete_message(
    session_id: str,
    msg_index: int,
    user_id: str = Query(...),
    channel: str = Query(...),
):
    """删除指定气泡所在的整轮对话（用户消息 + 全部 AI 响应）。"""
    try:
        op = _ensure_operator()
        messages = await op.get_messages(session_id, user_id, channel)
        file_idx = _dom_to_file_index(messages, msg_index)
        if file_idx < 0:
            raise HTTPException(status_code=404,
                                detail=f"未找到索引 {msg_index} 对应的消息")

        # 找到该消息所属轮次的用户消息（轮次起点）
        round_user_idx = _find_round_user_idx(messages, file_idx)
        if round_user_idx < 0:
            raise HTTPException(status_code=400,
                                detail="无法定位所属对话轮次")

        # 找到轮次终点（下一个用户消息或文件末尾）
        round_end = _get_round_end(messages, round_user_idx)

        # 删除整轮
        deleted_count = round_end - round_user_idx
        new_messages = messages[:round_user_idx] + messages[round_end:]
        await op._update_content(session_id, user_id, channel, new_messages)
        return {"success": True, "deleted": deleted_count, "remaining": len(new_messages)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except Exception as e:
        logger.exception("删除消息失败: %s", session_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}/info")
async def http_session_info(
    session_id: str,
    user_id: str = Query(default="default"),
    channel: str = Query(default="console"),
):
    """获取会话信息 — 消息数量、角色分布等。"""
    try:
        op = _ensure_operator()
        messages = await op.get_messages(session_id, user_id, channel)
        roles = [_get_msg_role(m) for m in messages if _get_msg_role(m) in ("user", "assistant")]
        return {
            "session_id": session_id,
            "user_id": user_id,
            "channel": channel,
            "total_messages": len(messages),
            "visible_messages": len(roles),
            "roles": roles,
        }
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except Exception as e:
        logger.exception("获取会话信息失败: %s", session_id)
        raise HTTPException(status_code=500, detail=str(e))


# ── 命令处理 ────────────────────────────────────────────────


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
        # 注册 HTTP 路由，前端按钮调用的 API
        api.register_http_router(
            router,
            prefix="/session-tools",
            tags=["session-tools"],
        )


plugin = Plugin()
