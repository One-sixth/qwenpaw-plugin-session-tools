"""会话增强工具插件 — 提供 /fork、/rewind、/regen 命令 + HTTP API

QwenPaw 2.x 版本。

架构说明：
- 所有命令（/rewind, /fork, /regen）用 register_slash_command() 注册
- /regen 修改 ctx.input_msgs 后返回 None 让 agent 继续
- HTTP API 使用 message_id 定位消息（解决虚拟滚动问题）
- 通过 SafeJSONSession API 读写会话文件
"""

import logging
from pathlib import Path

from agentscope.message import Msg, TextBlock
from fastapi import APIRouter, HTTPException, Query
from qwenpaw.plugins.api import PluginApi

from .session_ops import SessionOperator, sanitize_filename

logger = logging.getLogger(__name__)

# 按 workspace_dir 缓存 SessionOperator
_operators: dict = {}

# HTTP 路由器
router = APIRouter()


def _get_default_workspace_dir() -> Path:
    """获取默认工作区路径。"""
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


def _ensure_operator(workspace_dir: Path) -> SessionOperator:
    """获取或创建对应工作区的 SessionOperator。"""
    key = str(workspace_dir.resolve())
    if key not in _operators:
        _operators[key] = SessionOperator(workspace_dir)
    return _operators[key]


def _get_user_text(msg: object) -> str:
    """从消息对象中提取用户文本内容，兼容新旧两种格式。"""
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


# ── Slash 命令处理器 ────────────────────────────────────────


async def _handle_noreply_slash(ctx, args: str) -> Msg | None:
    """处理 /noreply 消息 — 发送消息但不触发 AI 回复。"""
    workspace_dir = ctx.workspace_dir
    if workspace_dir is None:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 无法获取工作区路径")])

    text = args.strip()
    if not text:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 请输入消息内容，格式：/noreply 消息内容")])

    op = _ensure_operator(workspace_dir)
    request = getattr(ctx, "request", None)
    user_id = getattr(request, "user_id", "") or ctx.session_id
    channel = getattr(request, "channel", "") or "console"

    try:
        # 读取当前消息列表
        messages = await op.get_messages(ctx.session_id, user_id, channel)

        # 构建一条用户消息，格式与现有消息一致
        from datetime import datetime
        import uuid
        new_msg = {
            "id": uuid.uuid4().hex,
            "role": "user",
            "name": "user",
            "content": [{"type": "text", "text": text}],
            "created_at": datetime.now().isoformat(),
            "metadata": {},
        }

        # 根据现有格式决定新消息的格式
        if messages and isinstance(messages[0], list):
            messages.append([new_msg, {}])
        else:
            messages.append(new_msg)

        await op._update_content(ctx.session_id, user_id, channel, messages)

        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text",
                                      text=f"✅ 已记录消息（不回复）")])
    except Exception as e:
        logger.exception("/noreply 失败")
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ 失败: {e}")])


async def _handle_rewind_slash(ctx, args: str) -> Msg | None:
    """处理 /rewind [N] — 回退 N 轮对话。"""
    workspace_dir = ctx.workspace_dir
    if workspace_dir is None:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 无法获取工作区路径")])

    op = _ensure_operator(workspace_dir)
    request = getattr(ctx, "request", None)
    user_id = getattr(request, "user_id", "") or ctx.session_id
    channel = getattr(request, "channel", "") or "console"

    n = 1
    if args.strip():
        try:
            n = int(args.strip())
        except ValueError:
            return Msg(name="system", role="assistant",
                       content=[TextBlock(type="text", text=f"❌ 参数错误: '{args}' 不是有效的数字")])

    try:
        deleted = await op.rollback(ctx.session_id, user_id, channel, n)
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"⏪ 已回退 {n} 轮（删除了 {deleted} 条消息）")])
    except Exception as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])


async def _handle_fork_slash(ctx, args: str) -> Msg | None:
    """处理 /fork [N] — 分叉新会话。"""
    workspace_dir = ctx.workspace_dir
    if workspace_dir is None:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 无法获取工作区路径")])

    op = _ensure_operator(workspace_dir)
    request = getattr(ctx, "request", None)
    user_id = getattr(request, "user_id", "") or ctx.session_id
    channel = getattr(request, "channel", "") or "console"

    n = 0
    if args.strip():
        try:
            n = int(args.strip())
        except ValueError:
            return Msg(name="system", role="assistant",
                       content=[TextBlock(type="text", text=f"❌ 参数错误: '{args}' 不是有效的数字")])

    try:
        new_id = await op.fork(ctx.session_id, user_id, channel, n)

        # 获取源会话名称
        src_name = "Chat"
        workspace = getattr(ctx, "workspace", None)
        if workspace:
            cm = workspace.chat_manager
            if cm:
                try:
                    from datetime import datetime, timezone
                    from qwenpaw.app.chats.models import ChatSpec
                    try:
                        src_spec = await cm.get_or_create_chat(
                            ctx.session_id, user_id, channel,
                        )
                        src_name = src_spec.name or "Chat"
                    except Exception:
                        pass
                    await cm.create_chat(ChatSpec(
                        name=f"Fork: {src_name}",
                        session_id=new_id, user_id=user_id, channel=channel,
                        created_at=datetime.now(timezone.utc),
                        meta={"forked_from": ctx.session_id, "n_rounds": n},
                    ))
                except Exception as e:
                    logger.warning("chat_manager 注册失败: %s", e)

        # 构建会话名和路径
        session_name = f"Fork: {src_name}"
        session_path = str(workspace_dir / "sessions" / channel / sanitize_filename(f"{user_id}_{new_id}.json"))

        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"✅ 已分叉新会话"),
                            TextBlock(type="text", text=f"📛 名称: {session_name}"),
                            TextBlock(type="text", text=f"📂 路径: {session_path}")])
    except Exception as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])


async def _handle_regen_slash(ctx, args: str) -> Msg | None:
    """处理 /regen — 重新生成上一条回复。

    删除最后一轮对话，然后修改 ctx.input_msgs[-1] 替换 /regen 为原文本，
    返回 None 让 agent 继续处理。
    """
    workspace_dir = ctx.workspace_dir
    if workspace_dir is None:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text="❌ 无法获取工作区路径")])

    op = _ensure_operator(workspace_dir)
    request = getattr(ctx, "request", None)
    user_id = getattr(request, "user_id", "") or ctx.session_id
    channel = getattr(request, "channel", "") or "console"

    try:
        original_text = await op.trim_last_round(ctx.session_id, user_id, channel)
    except ValueError as e:
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ {e}")])
    except Exception as e:
        logger.exception("/regen 失败")
        return Msg(name="system", role="assistant",
                   content=[TextBlock(type="text", text=f"❌ 重新生成失败: {e}")])

    # 修改 ctx.input_msgs[-1]，把 /regen 替换为原用户消息
    if ctx.input_msgs:
        last_msg = ctx.input_msgs[-1]
        if hasattr(last_msg, "content"):
            content = last_msg.content
            if isinstance(content, list):
                found = False
                for block in content:
                    if hasattr(block, "type") and block.type == "text":
                        block.text = original_text
                        found = True
                        break
                if not found:
                    content.append(TextBlock(type="text", text=original_text))
            else:
                last_msg.content = [TextBlock(type="text", text=original_text)]

    return None  # 让 agent 继续处理


# ── HTTP API ─────────────────────────────────────────────

@router.post("/session/{session_id}/rewind")
async def http_rewind(session_id: str,
                       user_id: str = Query(...), channel: str = Query(...),
                       created_at: str = Query(...)):
    """回退：按 created_at 定位消息后删除其及之后所有消息。

    使用 ctx.data.created_at 定位消息，不依赖 message_id。
    """
    try:
        ws_dir = _get_default_workspace_dir()
        op = _ensure_operator(ws_dir)

        file_idx, target_msg = await op.find_message_by_created_at(
            session_id, user_id, channel,
            created_at=created_at,
        )
        if file_idx < 0:
            raise HTTPException(status_code=404, detail=f"未找到消息: created_at={created_at}")

        rewound_message = _get_user_text(target_msg)
        messages = await op.get_messages(session_id, user_id, channel)
        new_messages = messages[:file_idx]
        deleted = len(messages) - len(new_messages)
        await op._update_content(session_id, user_id, channel, new_messages)

        return {"success": True, "deleted": deleted, "remaining": len(new_messages),
                "rewound_message": rewound_message}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except Exception as e:
        logger.exception("回退失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/{session_id}/regen")
async def http_regen(session_id: str,
                      user_id: str = Query(...), channel: str = Query(...),
                      created_at: str = Query(...)):
    """重新生成：按 created_at 定位到助手消息，找到对应的用户消息，
    回退到用户消息位置（删除该用户消息及之后所有），返回用户消息文本。

    前端将返回的文本填入输入框并发送，触发 agent 重新生成回复。
    """
    try:
        ws_dir = _get_default_workspace_dir()
        op = _ensure_operator(ws_dir)

        # 1. 定位到助手消息
        file_idx, target_msg = await op.find_message_by_created_at(
            session_id, user_id, channel,
            created_at=created_at,
        )
        if file_idx < 0:
            raise HTTPException(status_code=404, detail=f"未找到消息: created_at={created_at}")

        messages = await op.get_messages(session_id, user_id, channel)

        # 2. 从助手消息往前找，找到对应的用户消息
        user_idx = -1
        user_text = ""
        for i in range(file_idx, -1, -1):
            msg = messages[i]
            d = msg[0] if isinstance(msg, list) else msg
            role = d.get("role", "")
            if role == "user":
                user_idx = i
                user_text = _get_user_text(d)
                break

        if user_idx < 0:
            raise HTTPException(status_code=404, detail="未找到对应的用户消息")

        # 3. rewind 到用户消息位置（删除该用户消息及之后所有）
        new_messages = messages[:user_idx]
        deleted = len(messages) - len(new_messages)
        await op._update_content(session_id, user_id, channel, new_messages)

        return {"success": True, "deleted": deleted, "remaining": len(new_messages),
                "rewound_message": user_text}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("重新生成失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/{session_id}/fork")
async def http_fork(session_id: str,
                     user_id: str = Query(...), channel: str = Query(...),
                     created_at: str = Query(...)):
    """分叉：按 created_at 定位消息后分叉新会话。

    使用 ctx.data.created_at 定位消息，不依赖 message_id。
    """
    try:
        ws_dir = _get_default_workspace_dir()
        op = _ensure_operator(ws_dir)

        file_idx, target_msg = await op.find_message_by_created_at(
            session_id, user_id, channel,
            created_at=created_at,
        )
        if file_idx < 0:
            raise HTTPException(status_code=404, detail=f"未找到消息: created_at={created_at}")

        messages = await op.get_messages(session_id, user_id, channel)
        truncated = messages[:file_idx + 1]

        from datetime import datetime
        new_id = await op.fork(session_id, user_id, channel, 0, messages=truncated)

        # 注册到 chat_manager — 直接操作 chats.json 文件
        try:
            import json
            from datetime import datetime, timezone
            import uuid

            chats_path = ws_dir / "chats.json"
            if chats_path.exists():
                with open(chats_path, "r", encoding="utf-8") as f:
                    chats_data = json.load(f)

                # 获取源会话名称
                src_name = session_id
                for c in chats_data.get("chats", []):
                    if c.get("session_id") == session_id:
                        src_name = c.get("name", session_id)
                        break

                new_chat = {
                    "channel": channel,
                    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "id": str(uuid.uuid4()),
                    "meta": {"forked_from": session_id},
                    "name": f"Fork: {src_name}",
                    "pinned": False,
                    "session_id": new_id,
                    "source": "chat",
                    "status": "idle",
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "user_id": user_id,
                }

                chats_data.setdefault("chats", []).append(new_chat)
                with open(chats_path, "w", encoding="utf-8") as f:
                    json.dump(chats_data, f, ensure_ascii=False, indent=2)

                logger.info("已通过 chats.json 注册 fork 新会话: %s", new_id)
        except Exception as e:
            logger.warning("chats.json 注册失败（非致命）: %s", e)

        session_name = f"Fork: {src_name}"
        session_path = str(ws_dir / "sessions" / channel / sanitize_filename(f"{user_id}_{new_id}.json"))

        return {"success": True, "new_session_id": new_id, "messages": len(truncated),
                "session_name": session_name, "session_path": session_path}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except Exception as e:
        logger.exception("分叉失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/session/{session_id}/message")
async def http_delete_message(session_id: str,
                               user_id: str = Query(...), channel: str = Query(...),
                               created_at: str = Query(...)):
    """删除消息：按 created_at 定位消息后删除整轮。

    使用 ctx.data.created_at 定位消息，不依赖 message_id。
    """
    try:
        ws_dir = _get_default_workspace_dir()
        op = _ensure_operator(ws_dir)

        file_idx, target_msg = await op.find_message_by_created_at(
            session_id, user_id, channel,
            created_at=created_at,
        )
        if file_idx < 0:
            raise HTTPException(status_code=404, detail=f"未找到消息: created_at={created_at}")

        messages = await op.get_messages(session_id, user_id, channel)

        # 删除整轮：找到匹配消息所在的轮次（用户消息+助手回复）
        # 从当前消息往前找最近的 user 消息，往后找最近的 assistant 消息
        target_role = ""
        if isinstance(target_msg, list) and len(target_msg) > 0:
            target_role = target_msg[0].get("role", "")
        elif isinstance(target_msg, dict):
            target_role = target_msg.get("role", "")

        if target_role == "user":
            # 点击的是用户消息：删该用户消息 + 紧跟其后的助手消息（如果存在）
            start_idx = file_idx
            # 检查下一条是否是助手消息
            if file_idx + 1 < len(messages):
                next_msg = messages[file_idx + 1]
                next_role = ""
                if isinstance(next_msg, list) and len(next_msg) > 0:
                    next_role = next_msg[0].get("role", "")
                elif isinstance(next_msg, dict):
                    next_role = next_msg.get("role", "")
                if next_role == "assistant":
                    end_idx = file_idx + 2  # 用户+助手
                else:
                    end_idx = file_idx + 1  # 只删用户
            else:
                end_idx = file_idx + 1  # 没有下一条，只删用户
        else:
            # 点击的是助手消息：删该助手消息 + 前面的用户消息
            start_idx = file_idx - 1  # 前面的用户消息
            if start_idx < 0:
                start_idx = 0
            end_idx = file_idx + 1

        new_messages = messages[:start_idx] + messages[end_idx:]
        deleted = end_idx - start_idx

        await op._update_content(session_id, user_id, channel, new_messages)
        return {"success": True, "deleted": deleted, "remaining": len(new_messages)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except Exception as e:
        logger.exception("删除失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}/message")
async def http_get_message(session_id: str,
                            user_id: str = Query(...), channel: str = Query(...),
                            message_id: str = Query(default=""), created_at: str = Query(default="")):
    """获取消息详情：按 message_id 或 created_at 查找消息，返回完整 JSON。

    至少提供 message_id 或 created_at 中的一个参数。
    优先使用 message_id 查找。
    """
    if not message_id and not created_at:
        raise HTTPException(status_code=400, detail="至少提供 message_id 或 created_at 参数")

    try:
        ws_dir = _get_default_workspace_dir()
        op = _ensure_operator(ws_dir)

        if message_id:
            file_idx, target_msg = await op.find_message_by_id_or_text(
                session_id, user_id, channel,
                message_id=message_id,
            )
        else:
            file_idx, target_msg = await op.find_message_by_created_at(
                session_id, user_id, channel,
                created_at=created_at,
            )

        if file_idx < 0 or target_msg is None:
            detail = f"未找到消息: message_id={message_id}, created_at={created_at}"
            raise HTTPException(status_code=404, detail=detail)

        # 提取 msg dict（兼容旧格式 [msg_dict, extra] 对）
        if isinstance(target_msg, list) and len(target_msg) > 0:
            msg_dict = target_msg[0]
        elif isinstance(target_msg, dict):
            msg_dict = target_msg
        else:
            msg_dict = target_msg

        return {"success": True, "index": file_idx, "message": msg_dict}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("获取消息失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}/info")
async def http_session_info(session_id: str, user_id: str = Query(default="default"),
                             channel: str = Query(default="console")):
    """获取会话信息。"""
    try:
        ws_dir = _get_default_workspace_dir()
        op = _ensure_operator(ws_dir)
        messages = await op.get_messages(session_id, user_id, channel)

        role_count = {}
        for m in messages:
            role = ""
            if isinstance(m, list) and len(m) > 0:
                role = m[0].get("role", "")
            elif isinstance(m, dict):
                role = m.get("role", "")
            if role:
                role_count[role] = role_count.get(role, 0) + 1

        return {"session_id": session_id, "total_messages": len(messages), "role_counts": role_count}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话文件未找到")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 插件入口 ──────────────────────────────────────────────


class Plugin:
    """会话增强工具插件主类。"""

    def register(self, api: PluginApi):
        """注册插件到 QwenPaw 系统。"""
        logger.info("注册 Session Tools 插件...")

        api.register_slash_command(
            name="rewind", handler=_handle_rewind_slash,
            category="plugin", help_text="回退 N 轮对话，例如 /rewind 3",
        )
        api.register_slash_command(
            name="fork", handler=_handle_fork_slash,
            category="plugin", help_text="分叉新会话，例如 /fork 2",
        )
        api.register_slash_command(
            name="regen", handler=_handle_regen_slash,
            category="plugin", help_text="重新生成上一条回复",
        )
        api.register_slash_command(
            name="noreply", handler=_handle_noreply_slash,
            category="plugin", help_text="发送消息但不触发 AI 回复，例如 /noreply 好的",
        )

        api.register_http_router(router, prefix="/session-tools", tags=["session-tools"])

        logger.info("✓ Session Tools 插件注册完成")


plugin = Plugin()