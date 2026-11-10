# Changelog

本文件记录 qwenpaw-plugin-session-tools 的所有重要变更。

## [2.5.5] - 2026-11-11

### Fixed
- 🐛 **http_get_message 补 `except ValueError → 409`**（对抗复审 P2-1 半属实）：v2.5.3 在
  assistant 分支新增的 ValueError 路径（缺 completed_at / 不可解析 / 区间歧义）在该端点
  会落进 `except Exception` → 500。对齐其余四端点的 409 语义。复审同时误报 http_regen
  缺该分支——实测 497-498 行已有，驳回。

### Added
- 🧪 测试从 49 扩至 52 用例（复审 P3-3 覆盖洞）：role="" 显式路径、completed_at 传 naive
  无时区 ISO（锁定按 UTC 解释）、区间倒挂病态输入。
- 📝 CHANGELOG 补 [2.5.2] 已回退说明（复审 P3-2）；README 安装示例版本同步（P3-1）。


## [2.5.4] - 2026-11-11

### Fixed
- 🚧 **流式中刷新页面的气泡定位死区**：提交提示词后 agent 流式输出中刷新网页，user 气泡
  可能既无 `original_id` 且 `created_at=0`（消息尚未持久化）——任何定位方式都无效。
  前端五按钮在 `created_at` 为 0 时直接提示"找不到消息：agent 正在流式输出，请等回复完成
  后刷新网页再试"，不发后端请求。


## [2.5.2] - 2026-11-10（已回退，未发布）

> 曾实现「同轮定位」方案（role=assistant 先定位轮次 user 消息再取该轮最后一条 assistant），
> 打包安装后经作者确认时差数据（漂移实测 8.8s 远超预期、created_at 双形态同指发送时刻），
> 判定方向不佳，整体回退至 v2.5.1，由 v2.5.3 区间定位方案取代。


## [2.5.3] - 2026-11-11

### Changed
- 🎯 **assistant 端改用区间定位（不同角色不同方法）**：user 端维持渐进窗口 ±1/2/3s 不变；
  assistant 端改用 `created_at ~ completed_at` 自适应区间——助手消息顶层 `created_at`
  （首段保存时刻）必然落在 [发送, 完成] 区间内，区间随生成时长自适应，
  彻底解决固定窗口对长漂移（实测 8.8s）失效的问题。
- 🚫 **completed_at 缺失显式报错**：assistant 定位缺少/无法解析 `completed_at` 直接抛错
  提示开发者检查官方结构（正常情况下前端按钮上下文始终携带该字段），不做静默降级。

### Added
- 📐 **右边界 +1s 容差**（`RANGE_END_TOLERANCE`）：`completed_at` 为整秒截断，
  快回复（首段保存距完成 <1s）时目标可能落进截断缝隙；左边界不放宽（同为向下截断，
  放宽可能框进上一轮助手消息）。
- 🌐 **五端点 + `_locate_message` 透传 `completed_at`**（rewind / regen / fork / delete / inspect）；
  前端五个按钮在 assistant 气泡且无 original_id（刷新前）时校验并携带 `ctx.data.completed_at`。
- 🧪 测试从 38 扩至 49 用例：区间命中（实测漂移 8.8s 复现）、Unix 秒起点、截断缝隙容差、
  role 排除、上一轮不误命中、歧义 409、缺失/空白/不可解析报错、user 端回归。


## [2.5.1] - 2026-11-10

### Changed
- ⏹️ **fork 也先停流（方案 B 补全）**：`http_fork` 入口新增 `_stop_session_stream()`。实测发现：agent 流式回复中点 fork，原会话 run 继续跑——分叉快照内容不确定（读取瞬间与定位消息可能错位），且刷新页面后 SSE 断开而后台 run 仍在跑，状态混乱。fork 的语义是「从该消息定格分叉」，与破坏性操作同样需要先停源会话

## [2.5.0] - 2026-11-10

### Added
- 🛡️ **expect_count 乐观并发校验**：`_update_content` 写入前重读磁盘，消息总数与读取时不符即抛 `SessionChangedError` 中止写入（HTTP 409），杜绝「agent 追加消息被静默覆盖导致整个上下文丢失」（自 qwenpaw-session-tools-pro 反向吸收）
- ⏹️ **主动停止流式输出（方案 B）**：WebUI 按钮触发的破坏性 HTTP 操作（rewind / regen / delete）入口处先调 `_stop_session_stream()`——经 `chat_manager.get_chat_id_by_session` 反查 chat_id，`task_tracker.request_stop` 取消运行中的 run（幂等，未运行返回 False）；**await 带 15 秒超时**防长任务取消挂起阻塞请求；反查失败/超时不阻塞操作，仍有 expect_count 校验兜底。**slash 命令不做 stop**（执行时正处于 agent run 内，stop 等于自杀）
- 🎯 **created_at 渐进窗口匹配 + role 筛选**：`find_message_by_created_at` 新增可选 `role` 参数，匹配窗口逐层放宽 ±1s → ±2s → ±3s（模块常量 `TIME_MATCH_TIERS`），每层先按 role 过滤再判定：0 条扩窗 / 1 条命中 / ≥2 条 409。前端新增 `getBubbleRole(ctx)` 判断气泡角色并随请求传递，解决「刚发消息就点 rewind 定位不到」（气泡时间戳与写盘时间漂移 1~3 秒）
- 🧹 **前端 purgeMessageCache**：破坏性操作刷新前清两类宿主本地残留——`localStorage/sessionStorage` 的 `qwenpaw_pending_user_msg_{chatId}`（宿主持久化的发送中消息，reload 后复活成气泡，即「回退后消息删不掉」的根因）+ React 内存消息 store（root fiber 遍历找 `clearSessionMessages`）。全部 best-effort；fork / regen 刷新不 purge（regen 后续还有模拟发送）

### Changed
- 🔄 **点气泡删除 = 删整轮**：`http_delete_message` 改用 `find_turn_user` / `find_turn_end`（移入 session_ops.py 便于测试）轮次感知删除（user + assistant + system 工具链，直到下一条 user），替代原版只删「相邻一对」；定位消息之前无 user 消息时退化为只删自身
- 📂 **fork 统一走 ChatManager**：`http_fork` 移除手写 `chats.json` 分支，改为 `cm.create_chat(ChatSpec(...))`，不再与运行中的 ChatManager 状态互相覆盖；源会话名改经 `cm.list_chats` 查询
- 🎯 `_locate_message` 与全部 HTTP 端点（rewind / regen / fork / delete / get_message）新增 `role` Query 参数，透传给 created_at 匹配
- 🧪 测试从 16 扩至 38 用例：渐进窗口三层、窗口闭区间边界（Δ=2.0/3.0）、role 筛选（含旧格式）、Unix 毫秒串防护、`find_turn_user`/`find_turn_end` 轮次辅助函数全集

### Fixed
- 🐛 **三端点 400/404 被吞成 500**（存量 bug，v2.4.0 起就有）：`http_rewind` / `http_fork` / `http_delete_message` 缺 `except HTTPException: raise`，`_locate_message` 抛出的 400/404 被 `except Exception` 降级为 500 且 detail 丢失——三处统一补齐（http_regen / http_get_message 原本就有）
- 🐛 Unix 时间戳解析加合理范围 `1e8 < ts < 4e9`，毫秒串误传不再产生荒谬匹配
- 🧹 清理：`session_ops.py` 死 `import json`；`_update_content` 冗余 `_detect_format` 调用；`msg_entries` 倒序遍历（无行为影响的旧残留）改正序

### Security
- 🔒 并发校验覆盖全部 HTTP 破坏性写回路径（rewind / regen / delete），写入原子性由宿主 SafeJSONSession 写入锁保证
- 🔒 stop 超时保护：`asyncio.wait_for(timeout=15)` 防 uncancelable 区域挂起阻塞 HTTP 请求

### 复审
- 🔍 3 个无上下文子 agent 并行对抗复审（后端并发安全 / 定位与轮次删除 / 前端），报 2 P1 + 12 P2；采纳修复见上（stop 超时、HTTPException 透传、测试补齐等），驳回 1 条误报（ChatSpec.created_at 实测有 default_factory 非必填），其余 P2 为存量既有行为（callApi 不查 HTTP 状态码等）保持不动

## [2.4.0] - 2026-08-14

### Added
- 🎯 **original_id 消息定位增强**：优先使用 `original_id`（UUID）精确匹配消息，不可用时回退到 `created_at` 模糊匹配
  - 前端：新增 `getOriginalId()`，从 `ctx.data.input[0].metadata.original_id`（用户气泡）或 `ctx.data.output[0].metadata.original_id`（助手气泡）提取，传给 API
  - 后端：5 个 HTTP handler 增加 `original_id` 参数，新增 `_locate_message()` 统一定位逻辑（original_id → find_message_by_id，回退 created_at → find_message_by_created_at）

### Changed
- 🧹 `find_message_by_id_or_text()` → `find_message_by_id()`：移除历史残留的文本匹配分支和 `_client_id` 匹配，只保留按 `msg.id` 精确匹配
- 🧹 删除废弃的 `getMessageId()` 前端函数（读取的 `ctx.data.message_id` / `output[0].message_id` 字段均不存在）
- 🧹 `http_get_message` 移除无效的 `message_id` Query 参数

### Removed
- 🗑 移除 `message_id_investigation.md`、`ANALYSIS.md`（message_id 方案已彻底移除，存档文档失去价值）
- 🗑 移除 `plan_original_id.md`（方案已实施完成）

## [2.3.0] - 2026-08-03

### Changed
- 🧭 **多 agent 感知**：HTTP API 的 workspace 路径从固定取 `default` 改为跟随当前 agent
  - 后端：`_get_default_workspace_dir()` → `_get_workspace_dir(request)`，用官方 `get_agent_for_request` 解析当前 agent（优先级：agent_id 参数 → request.state.agent_id → X-Agent-Id header → active agent）
  - 6 个 HTTP handler（rewind / regen / fork / delete_message / get_message / session_info）全部增加 `request: Request` 参数
  - 前端：`callApi` 用原生 fetch + `host.getSelectedAgentId()` 实时设置 `X-Agent-Id` header

### Fixed
- 🐛 **多 agent 按钮请求失败**：`callApi` 原优先走 `host.fetch`，但其内部会**再拼一次 `/api`**，与 `getApiUrl()` 返回的完整 URL 冲突 → 请求 `.../api/api/...` → 404/405。改用原生 fetch + 手动 `X-Agent-Id` 后 URL 与 X-Agent-Id 均正确
- 🐛 非 default agent（如 ChqnR9）网页按钮原先错误访问 default 的 workspace 导致 404，现在与 slash 命令一致，各自用各的 workspace

## [2.2.0] - 2026-07-20

### Changed
- 🎯 `find_message_by_created_at` 重写：统一转 Unix 秒数比较，时区修复（`timezone.utc` → `LOCAL_TZ`），先严格匹配再 ±1 秒宽松匹配
- 📡 报错机制改进：多匹配返回 `409 Conflict`，404 未找到提示"请尝试刷新网页"
- 💬 报错文案统一风格："定位消息失败，基于 created_at 寻找匹配消息，……"
- 📂 `_get_default_workspace_dir` 重写：使用 `profiles["default"].workspace_dir`，去掉兜底路径
- 🔄 regen 按钮：轮询代替 80ms 单次超时，`dispatchEvent` 代替 `.click()`

### Added
- 🧪 新增 `test_find_message.py`：16 个测试用例覆盖全部核心路径

## [2.1.0] - 2026-07-12

### Added
- 🔍 调试按钮：点击打开新标签页显示完整 `ctx.data` 探测结果
- 📄 检查按钮：用 `created_at` 匹配会话文件消息并展示完整 JSON（新标签页）
- 后端 `GET /session/{session_id}/message` 路由：支持 `message_id` 和 `created_at` 参数
- `session_ops.py`：`find_message_by_created_at()` 方法
- 🔄 regen 按钮（助手消息气泡）：rewind 到该轮用户消息位置，填入输入框并自动发送
- 后端 `POST /session/{session_id}/regen` 路由：按 created_at 定位助手消息，找到对应用户消息并 rewind
- `/noreply` slash 命令：发送消息但不触发 AI 回复，直接操作 session 文件追加用户消息
- Debug 开关：通过 `localStorage.setItem('SessionTools.debug', 'true')` 开启探查按钮 🔍📄
- 按钮排序优化：用户气泡 ⏪🍴，助手气泡 🗑🔄⏪🍴

### Fixed
- 🔍 按钮因 JSDoc 注释未闭合导致 `deepProbe` 被吞进注释，修复为 `*/` 闭合
- `created_at` 时间匹配的时区陷阱：会话文件时间是无时区本地时间（Asia/Shanghai），Unix 时间戳是 UTC，差 8 小时导致匹配失败。修复：给消息时间补 `LOCAL_TZ` 而非 `timezone.utc`，目标时间也转本地时间再比较
- ISO 格式补充 `%Y-%m-%dT%H:%M:%S.%f`（无时区带毫秒）
- 🔄 regen 按钮 React 受控组件问题：QwenPaw 输入框是 React 受控组件，直接 `inputEl.value = xxx` 不生效。使用 `nativeInputValueSetter`（`Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set`）绕过 React value 代理后生效
- 🔄 regen 按钮移除自动刷新，改为填入输入框后通过 `setTimeout` 等待 React 更新 state，再点击发送按钮，1.5s 后刷新
- 🔄 regen 按钮加确认框避免误点击
- 🔄 regen 按钮 rewind 逻辑修复：从删除助手消息改为删除该轮用户消息+助手消息（从 `messages[:file_idx]` 改为找用户消息位置 `messages[:user_idx]`）
- 🗑 删除按钮修复：用户消息连着时，检查下一条是否是助手消息，是则删一对，否则只删用户消息
- `/noreply` 修复：`created_at` 用 `datetime.now().isoformat()` 字符串替代 `datetime.now()` 对象，避免 JSON 序列化失败
- 所有按钮（⏪🍴🗑）从 `message_id` 定位改为 `created_at` 定位，统一消息定位方案

### Changed
- 前端按钮注册顺序：用户气泡只保留 ⏪🍴，助手气泡保留 🗑🔄⏪🍴
- 探查按钮 🔍📄 默认不显示，需通过 debug 开关开启
- `plugin.py` 三个路由（rewind/fork/delete）参数从 `message_id` 改为 `created_at`

### Removed
- `/continue` slash 命令（尝试后移除，无法在不依赖 ctx 的情况下实现干净的一键继续）
- 所有按钮中 `getMessageId()` 的调用（不再需要 message_id 定位）

### Known Limitations
- `output[0].metadata.original_id` 包含真实 UUID，但仅历史加载的助手消息可拿到，即时新生成的助手消息和用户消息无法获取
- `created_at` 时间匹配作为替代方案，秒级精度，适用于低并发场景
- 🔄 regen 按钮模拟 Enter 发送在部分 QwenPaw 版本中可能不生效，需手动按 Enter 发送

### 🚀 重大变更

#### 迁移到 QwenPaw 2.x 插件系统
- **移除 `min_qwenpaw_version`** → 改用 `qwenpaw_version: {min: "2.0.0", max: "2.1.0"}`
- **移除 AgentRunner monkey-patch** → 新版 QwenPaw 使用 `Runtime` 替代 `AgentRunner`
- **控制命令改用新版 `ControlContext`** → `RewindCommandHandler` / `ForkCommandHandler` 使用新版 `BaseControlCommandHandler` 模式
- **`/regen` 改用 `register_slash_command()`** → 修改 `ctx.input_msgs` 后返回 `None`，让 agent 继续处理

#### 🐛 修复：虚拟滚动导致回退不准
- **根因**：旧版通过 `MutationObserver` + `querySelectorAll` 获取 DOM 气泡索引，QwenPaw 的虚拟滚动导致 DOM 只渲染可见消息，索引与文件索引无法对应
- **修复**：前端改用 `window.QwenPaw.chat.requestActions.add()` 和 `chat.actions.add()` 注册按钮，通过 React state 驱动，`onClick` 回调中拿到消息 ID（`data.id`），彻底绕过 DOM 索引

#### 消息 ID 定位替代 DOM 索引
- **HTTP API 参数**：`to_message_index` → `message_id`
- **后端新增**：`SessionOperator.find_message_by_id()` 按消息 ID 精准定位
- **移除**：`_dom_to_file_index()` 函数

### ✨ 功能改进

#### 会话文件格式兼容
- **自动检测新旧格式**：`_detect_format()` 识别 `state["agent"]["memory"]["content"]`（旧）和 `state["agent"]["state"]["context"]`（新）
- **保持原格式写回**：`_update_content()` 和 `fork()` 自动检测格式，保持原样保存
- **消息提取兼容**：`trim_last_round()` 和 `_get_role()` 支持新旧两种消息结构

#### ChatManager 访问更新
- 改用新版 `workspace.chat_manager` 属性（ServiceManager 模式）
- 分叉会话自动注册到 ChatManager，WebUI 侧边栏可见

---

## [1.2.0] - 2026-06-02

### 🐛 Bug 修复

#### 回退功能 (Bug1)
- **问题**：回退操作漏掉了目标会话（只删除了助手回复，保留了用户消息）
- **修复**：回退现在会删除整个目标消息及之后的所有内容
- **新增**：回退后，如果输入框为空，会自动填入被回退的用户消息文本

#### 助手消息按钮 (Bug2)
- **问题**：助手消息气泡没有显示操作按钮
- **修复**：基于实际 DOM 结构（`.qwenpaw-bubble-footer-left/right`）精准注入按钮
- **优化**：用户消息按钮在右侧，助手消息按钮在左侧

### ✨ 功能改进

#### 删除按钮
- **修改前**：删除整轮对话（用户消息 + 所有助手回复）
- **修改后**：
  - 用户消息：只删除该条用户消息
  - 助手消息：删除整个回复链（包含所有工具调用和最终回复）

#### 分叉按钮
- **修改前**：只能从用户消息位置分叉
- **修改后**：任意位置都可以分叉
  - 用户消息：保留到该用户消息（含）
  - 助手消息：保留到整个回复链结束

#### 回退按钮
- **修改前**：所有消息都显示回退按钮
- **修改后**：只在用户消息显示回退按钮

### 🔧 技术改进

#### 助手回复链处理
- 识别并正确处理一次助手回复包含多条记录的情况：
  ```
  assistant[tool_use] → system[tool_result] → ... → assistant[text]
  ```
- 删除/分叉操作会识别整个回复链的边界（以用户消息为界）

#### DOM 选择器优化
- 使用精确选择器替代模糊匹配：
  - 消息容器：`.qwenpaw-bubble[data-role]`
  - Footer 容器：`.qwenpaw-bubble-footer`
  - 用户按钮区：`.qwenpaw-bubble-footer-right`
  - 助手按钮区：`.qwenpaw-bubble-footer-left`
  - 输入框：`.qwenpaw-sender-content`

### 📝 文档更新
- 更新 README.md，添加 WebUI 按钮功能说明
- 添加详细的架构说明和工作原理
- 添加 DOM 选择器说明

---

## [1.1.0] - 2026-05-30

### 🐛 Bug 修复
- 修复 fork API 参数名错误（`at_message_index` → `to_message_index`）
- 使用 `get_or_create_chat` 替代 `create_chat` 注册分叉会话
- 增强 `_try_get_chat_manager` 的 fallback 机制

### ✨ 功能改进
- 新增 HTTP API 端点（rewind/fork/delete/info）
- WebUI 注入操作按钮（⏪🍴🗑）
- 不修改核心代码，纯插件式扩展

---

## [1.0.0] - 2026-05-28

### 🎉 初始版本
- 支持 `/rewind [N]` 命令回退 N 轮对话
- 支持 `/fork [N]` 命令分叉新会话
- 支持 `/regen` 命令重新生成回复
- 会话文件自动备份
- 多 agent 工作区隔离
