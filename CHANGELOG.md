# Changelog

本文件记录 qwenpaw-plugin-session-tools 的所有重要变更。

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
