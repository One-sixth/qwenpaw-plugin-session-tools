"""会话增强工具插件 — 提供 /fork、/rewind、/regen、/noreply 命令 + HTTP API

QwenPaw 2.x 版本。

架构说明：
- 所有命令（/rewind, /fork, /regen）用 register_slash_command() 注册
- /regen 修改 ctx.input_msgs 后返回 None 让 agent 继续
- HTTP API 优先使用 original_id（UUID）精确匹配，不可用时回退到 created_at 按角色定位
  （assistant 区间匹配 / 其他渐进窗口，v2.5.3）：
  user 端维持渐进窗口；assistant 端用 created_at ~ completed_at 自适应区间
  （首段保存时刻必然落在 [发送, 完成] 区间内，随生成时长自适应）；
  completed_at 缺失或为空直接报错提示开发者（正常情况下前端按钮上下文
  始终携带该字段）
- 通过 SafeJSONSession API 读写会话文件

v2.5.0 增强（自 qwenpaw-session-tools-pro 反向吸收 + 自研）：
- expect_count 乐观并发校验：写入前重读磁盘，消息数变化即中止（SessionChangedError → 409），
  杜绝"agent 追加消息被静默覆盖导致上下文丢失"
- 主动停止流式输出（方案 B）：WebUI 按钮触发的破坏性 HTTP 操作入口处，
  先经 task_tracker.request_stop 停止目标会话运行中的生成（幂等，未运行返回 False；
  带超时防长任务阻塞），从时序上保证操作期间会话不再变化；
  slash 命令不做 stop（执行时正处于 agent run 内，自杀风险）
- 点气泡删除 = 删整轮（user + assistant + system 工具链），不再只删相邻一对
- created_at 定位渐进窗口 ±1s/±2s/±3s + role 筛选（每层先筛角色再判定）
- fork 的 HTTP 路径统一走 ChatManager.create_chat（移除手写 chats.json 分支）
- 前端破坏性操作刷新前 purgeMessageCache（清宿主 React 消息 store 与 pending_user_msg 残留）
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from agentscope.message import Msg, TextBlock
from fastapi import APIRouter, HTTPException, Query, Request
from qwenpaw.plugins.api import PluginApi

from .session_ops import (
    SessionChangedError,
    SessionOperator,
    find_turn_end,
    find_turn_user,
    sanitize_filename,
)

logger = logging.getLogger(__name__)

# 按 workspace_dir 缓存 SessionOperator
_operators: dict = {}

# HTTP 路由器
router = APIRouter()


async def _get_workspace(request: Request):
    """获取当前请求对应的 agent Workspace 对象（多 agent 感知）。

    通过 QwenPaw 官方 get_agent_for_request 解析当前 agent：
    优先级依次为 agent_id 参数 → request.state.agent_id → X-Agent-Id header → active agent。
    """
    from qwenpaw.app.agent_context import get_agent_for_request

    return await get_agent_for_request(request)


async def _get_workspace_dir(request: Request) -> Path:
    """获取当前请求对应的 agent 工作区路径（多 agent 感知）。"""
    ws = await _get_workspace(request)
    return Path(ws.workspace_dir).expanduser().resolve()


async def _stop_session_stream(request: Request, session_id: str, channel: str) -> bool:
    """主动停止目标会话运行中的流式输出（方案 B：破坏性操作前无条件调用）。

    复用宿主停止机制：chat_manager.get_chat_id_by_session 反查 chat_id，
    再 task_tracker.request_stop 取消运行中的 run（task.cancel + await 真正停止）。
    await 带 15 秒超时：run 的取消若因 uncancelable 区域长时间挂起，放弃等待
    继续操作（cancel 信号已发出），由 expect_count 校验兜底。
    幂等无害：会话未在运行时返回 False；任何异常均吞掉并返回 False，
    不阻塞后续操作（stop 失败仍有 expect_count 校验兜底）。

    Returns:
        True = 有运行中的任务被停止；False = 未在运行或停止失败/超时。
    """
    try:
        ws = await _get_workspace(request)
        cm = getattr(ws, "chat_manager", None)
        if cm is None:
            return False
        try:
            chat_id = await cm.get_chat_id_by_session(
                session_id=session_id, channel=channel or "console",
            )
        except Exception:
            logger.debug("反查 chat_id 失败，跳过停止（expect_count 兜底）", exc_info=True)
            return False
        if not chat_id:
            return False
        tracker = getattr(ws, "task_tracker", None)
        if tracker is None:
            return False
        try:
            stopped = await asyncio.wait_for(
                tracker.request_stop(chat_id, reason="user"),
                timeout=15,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "停止会话 %s 的 run 超过 15s 未完成，放弃等待继续操作（cancel 信号已发出）",
                session_id,
            )
            return False
        if stopped:
            logger.info("已停止会话 %s 的流式输出（chat_id=%s）", session_id, chat_id)
        return stopped
    except Exception:
        logger.exception("停止会话流式输出失败（忽略，继续操作）")
        return False



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


async def _locate_message(
    op: SessionOperator,
    session_id: str, user_id: str, channel: str,
    original_id: str = "", created_at: str = "",
    role: str = "", completed_at: str = "",
) -> tuple[int, Any]:
    """定位消息：优先 original_id 精确匹配，回退 created_at 按角色定位
    （assistant 区间匹配 / 其他渐进窗口，v2.5.3）。

    Args:
        op: SessionOperator 实例
        session_id: 会话 ID
        user_id: 用户 ID
        channel: 通道名称
        original_id: 消息 UUID（对应 session 文件 msg.id），可选
        created_at: 时间戳，可选
        role: 角色过滤（"user"/"assistant"），仅作用于 created_at 匹配，可选
        completed_at: assistant 区间匹配终点（role="assistant" 时必填），
            仅作用于 created_at 匹配，可选

    Returns:
        tuple[int, Any]: (文件索引, 消息对象)

    Raises:
        HTTPException: 400 参数全空，404 未找到，
                       409 created_at 匹配到多条同角色候选
    """
    if not original_id and not created_at:
        raise HTTPException(
            status_code=400,
            detail="至少提供 original_id 或 created_at 参数",
        )

    if original_id:
        file_idx, target_msg = await op.find_message_by_id(
            session_id, user_id, channel,
            msg_id=original_id,
        )
    else:
        file_idx, target_msg = await op.find_message_by_created_at(
            session_id, user_id, channel,
            created_at=created_at, role=role, completed_at=completed_at,
        )

    if file_idx < 0:
        detail = "定位消息失败，基于 "
        if original_id:
            detail += "original_id 寻找匹配消息"
        else:
            detail += "created_at 寻找匹配消息"
        detail += "，未找到匹配的消息，请尝试刷新网页"
        raise HTTPException(status_code=404, detail=detail)

    return file_idx, target_msg


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
async def http_rewind(request: Request, session_id: str,
                       user_id: str = Query(...), channel: str = Query(...),
                       original_id: str = Query(default=""),
                       created_at: str = Query(default=""),
                       role: str = Query(default=""),
                       completed_at: str = Query(default="")):
    """回退：按 original_id 或 created_at 定位消息后删除其及之后所有消息。

    优先使用 original_id 精确匹配，不可用时回退到 created_at 按角色定位（assistant 区间匹配 / 其他渐进窗口，v2.5.3）。
    入口先停止该会话运行中的流式输出（方案 B），写回前做 expect_count 并发校验。
    """
    try:
        # 方案 B：破坏性操作前先停止目标会话的流式输出（幂等）
        await _stop_session_stream(request, session_id, channel)

        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)

        file_idx, target_msg = await _locate_message(
            op, session_id, user_id, channel,
            original_id=original_id, created_at=created_at, role=role,
            completed_at=completed_at,
        )

        rewound_message = _get_user_text(target_msg)
        messages = await op.get_messages(session_id, user_id, channel)
        new_messages = messages[:file_idx]
        deleted = len(messages) - len(new_messages)
        await op._update_content(session_id, user_id, channel, new_messages,
                                 expect_count=len(messages))

        return {"success": True, "deleted": deleted, "remaining": len(new_messages),
                "rewound_message": rewound_message}
    except SessionChangedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("回退失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/{session_id}/regen")
async def http_regen(request: Request, session_id: str,
                      user_id: str = Query(...), channel: str = Query(...),
                      original_id: str = Query(default=""),
                      created_at: str = Query(default=""),
                      role: str = Query(default=""),
                      completed_at: str = Query(default="")):
    """重新生成：按 original_id 或 created_at 定位到助手消息，找到对应的用户消息，
    回退到用户消息位置（删除该用户消息及之后所有），返回用户消息文本。

    优先使用 original_id 精确匹配，不可用时回退到 created_at 按角色定位（assistant 区间匹配 / 其他渐进窗口，v2.5.3）。
    入口先停止该会话运行中的流式输出（方案 B），写回前做 expect_count 并发校验。
    前端将返回的文本填入输入框并发送，触发 agent 重新生成回复。
    """
    try:
        # 方案 B：破坏性操作前先停止目标会话的流式输出（幂等）
        await _stop_session_stream(request, session_id, channel)

        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)

        # 1. 定位到助手消息
        file_idx, target_msg = await _locate_message(
            op, session_id, user_id, channel,
            original_id=original_id, created_at=created_at, role=role,
            completed_at=completed_at,
        )

        messages = await op.get_messages(session_id, user_id, channel)

        # 2. 从助手消息往前找，找到对应的用户消息
        user_idx = -1
        user_text = ""
        for i in range(file_idx, -1, -1):
            msg = messages[i]
            d = msg[0] if isinstance(msg, list) else msg
            role_ = d.get("role", "")
            if role_ == "user":
                user_idx = i
                user_text = _get_user_text(d)
                break

        if user_idx < 0:
            raise HTTPException(status_code=404, detail="重新生成失败，未找到对应的用户消息")

        # 3. rewind 到用户消息位置（删除该用户消息及之后所有）
        new_messages = messages[:user_idx]
        deleted = len(messages) - len(new_messages)
        await op._update_content(session_id, user_id, channel, new_messages,
                                 expect_count=len(messages))

        return {"success": True, "deleted": deleted, "remaining": len(new_messages),
                "rewound_message": user_text}
    except SessionChangedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("重新生成失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/session/{session_id}/fork")
async def http_fork(request: Request, session_id: str,
                     user_id: str = Query(...), channel: str = Query(...),
                     original_id: str = Query(default=""),
                     created_at: str = Query(default=""),
                     role: str = Query(default=""),
                     completed_at: str = Query(default="")):
    """分叉：按 original_id 或 created_at 定位消息后分叉新会话。

    优先使用 original_id 精确匹配，不可用时回退到 created_at 按角色定位（assistant 区间匹配 / 其他渐进窗口，v2.5.3）。
    新会话统一经 ChatManager.create_chat 注册（v2.5.0，移除手写 chats.json 分支，
    避免与运行中的 ChatManager 状态互相覆盖）。
    入口先停止源会话运行中的流式输出（v2.5.1，方案 B）：fork 的语义是
    「从该消息定格分叉」，agent 继续跑会让分叉快照内容不确定，且刷新后
    SSE 断开而后台 run 仍在跑，状态混乱——fork 与破坏性操作同样需要先停。
    """
    try:
        # 方案 B：先停止源会话的流式输出（幂等），保证分叉快照确定
        await _stop_session_stream(request, session_id, channel)

        ws = await _get_workspace(request)
        ws_dir = Path(ws.workspace_dir).expanduser().resolve()
        op = _ensure_operator(ws_dir)

        file_idx, target_msg = await _locate_message(
            op, session_id, user_id, channel,
            original_id=original_id, created_at=created_at, role=role,
            completed_at=completed_at,
        )

        messages = await op.get_messages(session_id, user_id, channel)
        truncated = messages[:file_idx + 1]

        new_id = await op.fork(session_id, user_id, channel, 0, messages=truncated)

        # 注册到会话列表 — 统一走 ChatManager（v2.5.0）
        src_name = ""
        session_name = f"Fork: {session_id}"
        cm = getattr(ws, "chat_manager", None)
        if cm is not None:
            try:
                for c in await cm.list_chats(user_id=user_id, channel=channel):
                    if c.session_id == session_id:
                        src_name = c.name or ""
                        break
            except Exception:
                logger.warning("fork: 查询源会话名称失败", exc_info=True)
            try:
                from qwenpaw.app.chats.models import ChatSpec
                await cm.create_chat(ChatSpec(
                    name=f"Fork: {src_name or session_id}",
                    session_id=new_id, user_id=user_id, channel=channel,
                    meta={"forked_from": session_id},
                ))
            except Exception as e:
                logger.warning("chat_manager 注册失败（非致命）: %s", e)
        else:
            logger.warning("fork: workspace 无 chat_manager，新会话未注册到列表")

        session_name = f"Fork: {src_name or session_id}"
        session_path = str(ws_dir / "sessions" / channel / sanitize_filename(f"{user_id}_{new_id}.json"))

        return {"success": True, "new_session_id": new_id, "messages": len(truncated),
                "session_name": session_name, "session_path": session_path}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("分叉失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/session/{session_id}/message")
async def http_delete_message(request: Request, session_id: str,
                               user_id: str = Query(...), channel: str = Query(...),
                               original_id: str = Query(default=""),
                               created_at: str = Query(default=""),
                               role: str = Query(default=""),
                               completed_at: str = Query(default="")):
    """删除消息：轮次感知地删除整个回合（v2.5.0）。

    一轮 = 本轮 user 消息 + 其后全部 assistant/system（含工具调用链），
    直到下一条 user 消息为止。替代原版只删"相邻一对"的逻辑。
    入口先停止该会话运行中的流式输出（方案 B），写回前做 expect_count 并发校验。
    """
    try:
        # 方案 B：破坏性操作前先停止目标会话的流式输出（幂等）
        await _stop_session_stream(request, session_id, channel)

        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)

        file_idx, target_msg = await _locate_message(
            op, session_id, user_id, channel,
            original_id=original_id, created_at=created_at, role=role,
            completed_at=completed_at,
        )

        messages = await op.get_messages(session_id, user_id, channel)

        # 删整轮：从被点消息往前找本轮 user 起点，往后扫到下一条 user 为止
        start_idx = find_turn_user(messages, file_idx)
        if start_idx < 0:
            # 定位消息之前没有 user 消息（异常历史），退化为只删该消息自身
            start_idx = file_idx
            end_idx = file_idx + 1
        else:
            end_idx = find_turn_end(messages, start_idx)

        new_messages = messages[:start_idx] + messages[end_idx:]
        deleted = end_idx - start_idx

        await op._update_content(session_id, user_id, channel, new_messages,
                                 expect_count=len(messages))
        return {"success": True, "deleted": deleted, "remaining": len(new_messages)}
    except SessionChangedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("删除失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}/message")
async def http_get_message(request: Request, session_id: str,
                            user_id: str = Query(...), channel: str = Query(...),
                            original_id: str = Query(default=""),
                            created_at: str = Query(default=""),
                            role: str = Query(default=""),
                            completed_at: str = Query(default="")):
    """获取消息详情：按 original_id 或 created_at 查找消息，返回完整 JSON。

    至少提供 original_id 或 created_at 中的一个参数。
    优先使用 original_id，回退到 created_at 按角色定位（assistant 区间匹配 / 其他渐进窗口，v2.5.3）。
    """
    if not original_id and not created_at:
        raise HTTPException(
            status_code=400,
            detail="至少提供 original_id 或 created_at 参数",
        )

    try:
        ws_dir = await _get_workspace_dir(request)
        op = _ensure_operator(ws_dir)

        if original_id:
            file_idx, target_msg = await op.find_message_by_id(
                session_id, user_id, channel,
                msg_id=original_id,
            )
        else:
            file_idx, target_msg = await op.find_message_by_created_at(
                session_id, user_id, channel,
                created_at=created_at, role=role,
                completed_at=completed_at,
            )

        if file_idx < 0 or target_msg is None:
            detail = f"未找到消息: original_id={original_id}, created_at={created_at}"
            raise HTTPException(status_code=404, detail=detail)

        # 提取 msg dict（兼容旧格式 [msg_dict, extra] 对）
        if isinstance(target_msg, list) and len(target_msg) > 0:
            msg_dict = target_msg[0]
        elif isinstance(target_msg, dict):
            msg_dict = target_msg
        else:
            msg_dict = target_msg

        return {"success": True, "index": file_idx, "message": msg_dict}
    except ValueError as e:
        # assistant 区间定位的 ValueError（缺 completed_at / 不可解析 / 区间歧义）→ 409
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("获取消息失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_id}/info")
async def http_session_info(request: Request, session_id: str, user_id: str = Query(default="default"),
                             channel: str = Query(default="console")):
    """获取会话信息。"""
    try:
        ws_dir = await _get_workspace_dir(request)
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
        raise HTTPException(status_code=404, detail="定位消息失败，会话文件未找到")
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