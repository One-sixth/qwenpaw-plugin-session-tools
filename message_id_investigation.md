# QwenPaw 插件消息 ID 定位问题 — 调查结论（历史文档）

> ⚠️ **现状（2026-07-12）**：最终采用 `created_at` 时间匹配方案。本文档记录了从「文本+时间+邻居匹配」到「message_id 注入」再到「created_at 时间匹配」的完整演进过程，作为设计决策参考保留。
>
> **当前方案**：所有按钮使用 `ctx.data.created_at` 定位消息，`session_ops.py` 的 `find_message_by_created_at()` 实现秒级模糊匹配（补 LOCAL_TZ 本地时区）。
>
> **`output[0].metadata.original_id` 局限**：仅历史加载的助手消息可用，不可作为通用方案（详见 §补充）。

---

## 一、当前方案与问题

### 当前方案：文本+时间+邻居三层匹配

前端从 DOM 采集气泡的文本、时间、邻居信息 → 传给后端 → 后端在 session 文件中按文本前缀/时间/邻居做三层匹配。

### 遇到的失败场景

**问题 1：Markdown 渲染导致文本不一致**
- Session 文件存的是原始 Markdown：`现在是 **2026年...**（北京时间）～ 🌙`
- DOM 渲染后 `.qwenpaw-markdown` 的 textContent：`现在是 2026年...（北京时间）～ 🌙`
- `**` 被渲染为粗体后丢失，前后端文本前缀不匹配 → 404

**问题 2：时间格式不一致**
- DOM 显示时间：`07-11 23:23:35`（含日期前缀）
- Session 文件 `created_at` 提取：`23:23:35`（纯时分秒）
- 时间对比永远对不上

**问题 3：DOM 倒序 + 边界邻居越界**
- DOM 倒序渲染（最新的在上），session 文件正序（最早的在前）
- 最早/最新的消息在 DOM 中只有一侧有邻居 → 后端检查越界直接失败
- 已修复：越界邻居改为 `continue` 跳过

---

## 二、关键发现：原始 ID 已在 metadata 中

### 代码位置

`D:\Software\miniconda3\envs\qwenpaw\Lib\site-packages\qwenpaw\app\chats\utils.py` 第 449 行：

```python
metadata = {
    "original_id": msg.id,          # ← 真实 UUID 已经在这里了！
    "original_name": msg.name,
    "metadata": msg.metadata,
    "timestamp": ts_value,
}
```

`agentscope_msg_to_message()` 转换历史消息时，`metadata.original_id` 已经包含了 session 消息的真实 UUID。

### 但前端拿不到

前端 `chat.actions/requestActions.add()` 注册的按钮，`onClick({ data })` 的 `data` 对象中**不包含 `metadata` 字段**（至少未经验证），所以 `metadata.original_id` 虽然存在但前端无法读取。

### 气泡 DOM id 也不是原始 ID

| 来源 | 用户消息 ID 格式 | 助手消息 ID 格式 |
|------|-----------------|-----------------|
| Session 文件 | UUID (`96785392...`) | UUID (`3705fb88...`) |
| DOM 气泡 id | 不同的 UUID (`18fe847d...`) | SSE 流 ID (`1783788116490-...`) |
| SSE 流 data.id | `msg_xxx` | `response_xxx` |

三套 ID 体系互不兼容，无法直接映射。

---

## 三、推荐方案：改 QwenPaw 源码，直接注入 message_id

### 根本思路

让消息的真实 session UUID 作为前端 `onClick` 的 `data` 中的可直接读取字段，前端拿到后直接调后端 API 定位，**完全不需要文本匹配**。

### 改动 1：`envelope.py` — 新消息 SSE 流注入

**文件**：`D:\Software\miniconda3\envs\qwenpaw\Lib\site-packages\qwenpaw\runtime\envelope.py`

**位置**：`__init__` 方法中（约第 47 行后），在 `self._response` 创建后立即注入

```python
self._response = AgentResponse(output=[], status=RunStatus.Created)
self._response.object = "response"
self._response.id = "response_" + uuid.uuid4().hex
# ... 现有代码 ...

# === 新增：注入消息 ID ===
self._response.message_id = self._message_id
```

`AgentResponse` 有 `model_config = ConfigDict(extra="allow")`，允许额外字段 ✅

**效果**：新消息 SSE 推送到前端后，`chat.actions.onClick` 的 `data.message_id` 即可读取真实消息 ID。

### 改动 2：`utils.py` — 历史消息注入

**文件**：`D:\Software\miniconda3\envs\qwenpaw\Lib\site-packages\qwenpaw\app\chats\utils.py`

**位置**：`agentscope_msg_to_message()` 函数中，在创建 `Message` 对象后（约第 455 行后）

```python
message = Message(type=MessageType.MESSAGE, role=role)
message.metadata = metadata
# === 新增：注入消息 ID ===
message.message_id = msg.id

text_content = TextContent(...)
message.add_content(new_content=text_content)
results.append(message)
```

**需要确认**：`Message` 类是否允许额外字段（`extra="allow"`）。

**效果**：历史消息加载时，每条 `Message` 携带 `message_id`，前端 `data.message_id` 可读取。

### 改动 3：`frontend/index.js` — 从 data.message_id 直接读取

去掉文本匹配，直接从 `data` 读取：

```javascript
function getMessageId(ctx) {
    // 直接从 data.message_id 读取（新消息和历史消息都支持）
    if (ctx.data && ctx.data.message_id) {
        return ctx.data.message_id;
    }
    // 兜底：从 data.metadata?.original_id 读取
    if (ctx.data && ctx.data.metadata && ctx.data.metadata.original_id) {
        return ctx.data.metadata.original_id;
    }
    return "";
}
```

---

## 四、备选：不改源码也能用的降级方案

如果不想改 QwenPaw 源码，当前插件已经具备以下能力：

| 场景 | 方案 | 可靠性 |
|------|------|--------|
| 唯一文本 | 文本匹配第一层命中 | ✅ 高 |
| 重复文本+不同时间 | 文本+时间第二层命中 | ✅ 高 |
| 重复文本+相同时间 | 邻居第三层命中 | ⚠️ 受 DOM 顺序影响 |
| 含 Markdown 的消息 | 需后端去 Markdown 后再匹配 | ⚠️ 需加 strip_md |
| 边界消息 | 已修复越界跳过 | ✅ 已修复 |

当前存在的未修复问题：
- 时间格式不匹配（DOM `07-11 HH:MM:SS` vs 后端 `HH:MM:SS`）
- Markdown 渲染丢失标记

---

## 五、（已废弃）message_id 注入改方案 — 未落地

> ⚠️ 本章描述的 message_id 注入方案（改 QwenPaw 源码 envelope.py / utils.py + 构建产物补丁）**在调试中发现问题后已被放弃**，未作为最终方案落地。
>
> **放弃原因**（详见 §6）：
> 1. `requestActions` 的 `ctx.data` 只暴露 `{id, created_at, input}`，不暴露 `message_id` — 前端根本拿不到注入的字段
> 2. 历史消息的 `data.message_id` 需要通过修改构建产物 `fs()` 函数才能透传，维护成本高
> 3. 需要同时改 site-packages + 构建产物 + 官方仓库三处，升级 QwenPaw 会被覆盖
>
> **最终落地的是 `created_at` 时间匹配方案**（详见 §6 补充），本节仅作为历史探索记录保留。

### 尝试过的改动 ①：`envelope.py` — 新消息 SSE 流注入
**文件**：`D:\Software\miniconda3\envs\qwenpaw\Lib\site-packages\qwenpaw\runtime\envelope.py`

**改动内容**：
- `__init__` 中将 `self._message_id = _gen_msg_id()` 提前到 `self._response` 创建之前
- 在 `self._response.session_id = session_id` 后追加：
  ```python
  self._response.message_id = self._message_id
  ```

### 尝试过的改动 ②：`utils.py` — 历史消息注入
**文件**：`D:\Software\miniconda3\envs\qwenpaw\Lib\site-packages\qwenpaw\app\chats\utils.py`

**改动内容**：在 `agentscope_msg_to_message()` 函数中，10 处注入 `message.message_id = msg.id`

### 尝试过的改动 ③：`plugin.py` — 后端 API 切换为 message_id
**文件**：`D:\Data\github_repo\qwenpaw-plugin-session-tools\plugin.py`

**改动内容**：三个 HTTP API 路由参数从文本匹配改为 `message_id: str = Query(...)`

> ⚠️ **已回滚**：最终改为 `created_at: str = Query(...)`（见 CHANGELOG v2.1.0）

### 尝试过的改动 ④：`frontend/index.js` — 新增 `getMessageId()`
**文件**：`D:\Data\github_repo\qwenpaw-plugin-session-tools\frontend\index.js`

**改动内容**：新增 `getMessageId(ctx)` 函数尝试从 `ctx.data.message_id` 读取

> ⚠️ **已废弃**：`getMessageId()` 标记为 `@deprecated`，所有按钮改用 `ctx.data.created_at` 定位

### 尝试过的改动 ⑤：构建产物补丁
**文件**：QwenPaw Vendor 构建产物 `index-BPuL8_6t.js`

**改动内容**：`fs()` 函数中 `data.id` 改为 `e.message_id || e.id`

> ⚠️ **已回滚**：已恢复原始构建产物，不再需要

---

## 六、补充改动 & 踩坑记录（2026-07-12 下午）

> 实际操作中发现推荐方案无法直接落地，经历了多次调试和修复。

### 问题一：`window.QwenPaw.host.addChatAction` 已不存在

**现象**：`qwenpaw plugin install --force` 后，网页端插件按钮全部消失。

**根因**：QwenPaw 2.x 中 `chat.actions` / `chat.requestActions` 的注册方式已改为：
```typescript
window.QwenPaw.chat.requestActions.add(pluginId, ChatActionSpec)
```
旧的 `window.QwenPaw.host.addChatAction(spec)` API 已被移除。

**修复**：`frontend/index.js` 的 `registerButtons()` 改为：
```javascript
chat.requestActions.add(PLUGIN_NAME, makeRewindButton());
```

### 问题二：`icon` 必须是 ReactElement，不能是 HTMLElement

**现象**：注册 React 报错 #130，页面崩溃无法打开。

**根因**：`ChatActionSpec.icon` 类型为 `React.ReactElement`，但 `createIcon()` 用 `document.createElement('span')` 返回了原生 DOM 元素。

**修复**：改用 `window.QwenPaw.host.React.createElement('span', {...}, emoji)`。

### 问题三：`requestActions` 的 `ctx.data` 只暴露了 `created_at` 和 `input`

**现象**：按钮出现后，点击报"无法获取消息 ID"。

**根因**：`requestActions` 的 `ctx.data` 来自 Vendor 的 `AgentScopeRuntimeRequestCard` 组件内部，只包含 `created_at` 和 `input` 两个字段，**不包含消息 ID**。这是 QwenPaw 前端 Vendor SDK 的设计限制。

**调试方法**：在 `getMessageId` 中加 `console.log` 打印 `ctx.data.keys`，确认只有 `created_at` 和 `input`。

### 问题四：`e.id` 不是 session 文件中的 `msg.id`

**现象**：在前端 `fs()` 函数中把 `e.id` 注入 `data` 后，后端仍报 404。

**根因**：后端 API `GET /api/chats/{id}` 返回的消息经过 `agentscope_msg_to_message()` 转换，QwenPaw `Message.id` 默认是 `uuid4().hex`（新生成的 UUID），**不是** session 文件中 agentscope Msg 的原始 `id`。

而我们之前在 `utils.py` 中注入的 `message.message_id = msg.id` 存放在 **`e.message_id`** 字段中。

**修复**：改构建产物 `fs()` 函数，用 `e.message_id || e.id` 替代 `e.id`。

### （已废弃）message_id 注入方案改动总结

| 步骤 | 文件 | 改动 | 状态 |
|------|------|------|------|
| ① | `envelope.py` | `_response.message_id = _message_id` | ❌ 已回滚（已恢复原始代码） |
| ② | `utils.py` | 10 处注入 `message.message_id = msg.id` | ❌ 已回滚（已恢复原始代码） |
| ③ | `plugin.py` | API 路由改为 `message_id: str = Query(...)` | ❌ 已回滚（改为 `created_at`） |
| ④ | `frontend/index.js` | 注册 API 改用 `chat.requestActions.add`，`createIcon` 改用 `React.createElement` | ✅ 保留（与 message_id 方案无关，是 QwenPaw 2.x 迁移必须的改动） |
| ⑤ | **构建产物 JS** | `fs()` 函数中 `data.id` 改为 `e.message_id \|\| e.id` | ❌ 已回滚（已恢复原始代码） |

### message_id 方案失败的根本原因
- `requestActions` 的 `ctx.data` 结构是 `{id, created_at, input}`，**不包含 `message_id` 字段** — 这是 QwenPaw 前端 Vendor SDK 的设计限制，无法通过改后端源码解决
- 历史消息的 `message_id` 需要通过修改构建产物 `fs()` 函数才能透传，升级 QwenPaw 会被覆盖
- 最终落地的是 **`created_at` 时间匹配方案**（详见 §6 补充项）

### 补充：`output[0].metadata.original_id` 的局限性

`output[0].metadata.original_id` 包含真实 UUID，但**严重受限**：

| 场景 | 能否拿到 |
|------|---------|
| 历史加载的助手消息 | ✅ 可拿到 `output[0].metadata.original_id` |
| 即时新生成的助手消息 | ❌ `output[0]` 无 `metadata.original_id` 字段 |
| 用户消息气泡 | ❌ `requestActions` 的 `ctx.data` 只有 `{id, created_at, input}` |

**结论**：不能作为通用方案，只能退而求其次用 `created_at` 时间匹配。

### 补充：`created_at` 时间匹配方案的时区陷阱

**核心问题**：会话文件中的 `created_at` 是**本地时间**（Asia/Shanghai，无时区），而前端传来的 created_at 可能是 Unix 秒数（UTC 时间）或 ISO 字符串。

**匹配策略**（2026-07-12 修复）：
1. 给会话文件中的无时区 datetime 补 `LOCAL_TZ`（UTC+8），**不是** `timezone.utc`
2. 把目标时间（可能来自 Unix 时间戳 UTC）转成本地时间
3. 秒级模糊比较：`strftime("%Y-%m-%d %H:%M:%S")`

**修复位置**：`session_ops.py` 的 `find_message_by_created_at()` 方法

### 补充：删除按钮的用户消息相邻处理

**问题**：当用户消息连着出现（中间没有助手消息）时，原来硬编码 `end_idx = file_idx + 2` 会误删两条用户消息。

**修复**（2026-07-12）：检查下一条消息的 role，如果是 assistant 则删一对（用户+助手），否则只删该用户消息。

### 补充：regen 按钮的 rewind 策略

**错误做法**：找到助手消息索引 `file_idx`，`messages[:file_idx]` 只删助手消息 → 残留用户消息。

**正确做法**：从助手消息向前找对应的用户消息索引 `user_idx`，`messages[:user_idx]` 删除整轮。

### 补充：React 受控组件填值技巧

**问题**：QwenPaw 的输入框是 React 受控组件，`inputEl.value = xxx` 不生效。

**修复**：使用 `nativeInputValueSetter`：
```javascript
var nativeSetter = Object.getOwnPropertyDescriptor(
  window.HTMLTextAreaElement.prototype, "value"
).set;
nativeSetter.call(textarea, "新内容");
textarea.dispatchEvent(new Event("input", { bubbles: true }));
```

### 补充：Debug 开关

通过 `localStorage` 控制探查按钮 🔍📄 的显示：
- 开启：`localStorage.setItem('SessionTools.debug', 'true'); location.reload();`
- 关闭：`localStorage.removeItem('SessionTools.debug'); location.reload();`

### 补充：Slash 命令 `/noreply`

直接操作 session 文件追加用户消息，返回 `Msg` 阻止 agent 回复。注意 `created_at` 需用 `isoformat()` 字符串。