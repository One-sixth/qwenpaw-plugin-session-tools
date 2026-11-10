# qwenpaw-plugin-session-tools 项目记忆

> 最后更新：2026-11-10

## 项目概览

- **路径**：`D:\Data\github_repo\qwenpaw-plugin-session-tools`
- **定位**：QwenPaw 会话管理增强插件：回退对话、分叉会话、重新生成回复、删除消息、静默记录
- **版本**：v2.5.0（2026-11-10）
- **使用方式**：WebUI 按钮（消息气泡下方）+ 控制台命令（`/rewind`、`/fork`、`/regen`、`/noreply`）
- **安装**：`python scripts/build_release.py` 打包 → `qwenpaw plugin install dist/qwenpaw-plugin-session-tools-v<版本>.zip --force`，热安装后 Ctrl+F5 刷新生效（2026-11-10 起改用 zip 包安装，与 pro 一致；打包脚本含 MEMORY.md 泄漏自检）

## 架构

```
plugin.json        # 插件元数据
plugin.py          # 入口：slash 命令注册 + HTTP API
session_ops.py     # 核心操作：会话文件读写、消息查找
frontend/index.js  # WebUI：按钮注册、API 调用
```

## 核心设计

### 消息定位：original_id 优先，created_at 按角色定位（v2.5.3）
1. 优先 `ctx.data.input[0].metadata.original_id` / `output[0].metadata.original_id`（UUID）→ 按 session 文件 `msg.id` 精确匹配
2. 回退 `ctx.data.created_at` → `find_message_by_created_at()`：统一转 Unix 秒数比较，**不同角色不同方法**：
   - **assistant（v2.5.3）：区间匹配**——msg.created_at ∈ [created_at, completed_at + 1s] 且 role=assistant。
     数学依据：助手消息顶层 created_at（首段保存时刻）必然落在 [发送, 完成] 区间内，区间随生成时长自适应，
     实测漂移 8.8s（固定窗口 ±3s 失效）轻松覆盖。右边界 +1s 容差（`RANGE_END_TOLERANCE`）防 completed_at
     整秒截断缝隙（快回复首段保存距完成 <1s）；左边界不放宽（同为向下截断，放宽可能框进上一轮助手消息）。
     **completed_at 缺失/空白/不可解析 → ValueError 提示开发者**（正常情况前端按钮上下文必携带，官方改结构才缺）。
   - **user：渐进窗口 ±1s → ±2s → ±3s**（模块常量 `TIME_MATCH_TIERS`，气泡时间戳与写盘时间常有 1~3 秒漂移），**每层先按 role 过滤再判定**：0 条扩窗 / 1 条命中 / ≥2 条 409；role 由前端 `getBubbleRole(ctx)` 判断（input[0]→user、output[0]→assistant）
3. 多匹配返回 `409 Conflict`；未找到返回 `404`（提示刷新网页）
4. **流式中刷新的死区（v2.5.4）**：提交后 agent 输出中刷新网页，user 气泡可能既无 `original_id`
   且 `created_at=0`（消息尚未持久化）→ 前端五按钮检测 `created_at` 为 0 直接提示等待刷新，
   不发后端请求

### 并发防护（v2.5.0）
- **expect_count 乐观校验**：`_update_content(expect_count=N)` 写入前重读磁盘，条数不符抛 `SessionChangedError` → 409，杜绝 agent 追加消息被静默覆盖（作者曾因此丢过整个上下文）
- **方案 B 主动停流**：**全部四个 WebUI 按钮操作（rewind/regen/delete/fork）** 入口先 `_stop_session_stream()`——`chat_manager.get_chat_id_by_session` 反查 chat_id → `task_tracker.request_stop`（task.cancel + await 真停，幂等）；**await 带 15s 超时**（asyncio.wait_for）防长任务取消挂起阻塞请求；反查失败直接跳过（expect_count 兜底）；**slash 命令不做 stop**（handler 正处于 agent run 内，stop=自杀）。fork 加停流是 v2.5.1 补的（实测发现：agent 回复中 fork，原 run 继续跑 → 分叉快照不确定 + 刷新后 SSE 断但后台还在跑）
- **存量 bug 修复**：http_rewind/http_fork/http_delete_message 补 `except HTTPException: raise`（v2.4.0 起 400/404 就被吞成 500）

### 轮次感知删除（v2.5.0）
- `http_delete_message` 用 `find_turn_user`/`find_turn_end`（**模块级函数，放 session_ops.py** 便于测试；plugin.py 因依赖 agentscope 无法在测试环境导入）：删 user 起点到下一条 user 前（含 system 工具链），替代旧版"相邻一对"

### 时区处理
会话文件 `created_at` 是无时区本地时间（Asia/Shanghai），前端可能是 UTC 或 Unix 秒数 → **统一转 Unix 秒数比较**，绕开时区转换。

### 多 agent 感知（v2.3.0）
- workspace 路径跟随当前 agent：`get_agent_for_request(request)` 解析（优先级：agent_id 参数 → request.state.agent_id → X-Agent-Id header → active agent）
- 前端 `callApi` 用**原生 fetch** + `host.getSelectedAgentId()` 手动设 `X-Agent-Id`；**禁用 `host.fetch`**（内部再拼一次 `/api` → `.../api/api/...` 404/405）

### 前端 purgeMessageCache（v2.5.0）
- rewind/delete 刷新前清：`qwenpaw_pending_user_msg_{chatId}`（localStorage+sessionStorage 双存储双 key，宿主发送中消息持久化，reload 复活成气泡=「回退后删不掉」根因）+ React 内存消息 store（root fiber 遍历 `clearSessionMessages`）
- fork/regen 刷新**不** purge（regen 后续还有模拟发送，清缓存会干扰新 run 流式渲染）
- 全部 best-effort（依赖宿主内部实现，QwenPaw 升级可能失效需复检）

### Slash 命令
`register_slash_command()` 注册，返回 `Msg` 阻止 agent 继续，或返回 `None` 让 agent 继续处理。

### WebUI 按钮注册
- 用户消息：`chat.requestActions.add()`
- 助手消息：`chat.actions.add()`
- 探查按钮默认隐藏：`localStorage.setItem('SessionTools.debug', 'true')` 开启

## 版本要点

- **v2.5.5（2026-11-11）**：无上下文对抗复审（子 agent）修复——http_get_message 补 except ValueError→409（P2-1 半属实，regen 部分系复审误报驳回）；补 3 覆盖洞测试（role=""/naive completed_at/倒挂区间）；CHANGELOG 补 2.5.2 回退说明；README 安装示例版本同步
- **v2.5.4（2026-11-11）**：流式中刷新死区拦截（前端五按钮检测 created_at=0 直接提示等待，不发请求；该场景气泡无 original_id 且消息未持久化）
- **v2.5.3（2026-11-11）**：assistant 端区间定位（created_at ~ completed_at 自适应区间，数学依据=首段保存必然落在 [发送, 完成] 区间内；v2.5.2 曾尝试的同轮定位方案经作者确认数据后被此方案取代并回退）；user 端维持渐进窗口；completed_at 缺失显式报错；五端点+前端五按钮透传；右边界 +1s 容差防整秒截断缝隙
- **v2.5.1（2026-11-10）**：fork 也先停流（方案 B 补全，作者实测发现原会话未 stop）
- **v2.5.0（2026-11-10）**：反向吸收 qwenpaw-session-tools-pro（ccy-max 基于 v2.4.0 的 fork，只吸收 4 项：expect_count/删整轮/purgeMessageCache/思路）+ 自研 2 项（渐进窗口+role 定位、方案 B 主动停流）+ fork 走 ChatManager（移除手写 chats.json）；ChatSpec.created_at 有 default_factory 可省略；pro 未吸收项：快照/undo 系统、三层文本定位（原版有死函数 find_message_by_text_time_neighbors 未接线，仍保持未接线）、antd UI、regen 移除模拟点击
- **manifest 版本范围（2026-10-30 已修）**：`qwenpaw_version.max` 放宽至 **2.99.0**（排他上界，覆盖整个 2.x；官方恢复 max 检查后仍兼容）。详见小窝知识库《QwenPaw插件加载机制2.3.0b2.md》
- **v2.4.0**：original_id 精确匹配；`find_message_by_id()` 移除文本匹配残留；清理废弃文档
- **v2.3.0**：多 agent 感知 + host.fetch 陷阱修复
- **v2.2.0**：created_at 时区修复（LOCAL_TZ）、409/404 报错机制

## 测试

- `test_find_message.py`：52 用例（pytest + asyncio），覆盖定位（ISO/Unix/旧格式/时区）、assistant 区间匹配（漂移 8.8s 复现/截断缝隙/role 排除/上轮不误命中/缺失报错/naive completed_at/倒挂区间）、渐进窗口 3 层 + 闭区间边界（Δ=2.0/3.0）、role 筛选（含旧格式与显式 role=""）、多匹配报错、`find_turn_user`/`find_turn_end` 轮次辅助、边界；跑法：`python -m pytest test_find_message.py -v`
- **2026-11-10 三子 agent 对抗复审**：报 2 P1 + 12 P2；P1（stop 长阻塞→加 15s 超时；三端点 HTTPException 被吞→补 raise）与关键 P2 全部采纳修复；驳回误报 1 条（ChatSpec.created_at 有 default_factory）；复审法沉淀：让复审员实测 `models.py` 可防"没查证就报必填缺失"类误报

## 参考

- 知识库：`memory/知识库/QwenPaw slash 命令机制.md`、`QwenPaw工具Schema生成机制.md`、`QwenPaw HTTP API 认证机制.md`、`QwenPaw源码聊天系统架构.md`（task_tracker/stop API）
