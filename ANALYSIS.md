# QwenPaw 2.0 前端插件消息 ID 注入方案分析

> ⚠️ **历史存档（2026-07-12）**：本文档分析的 message_id 注入方案**已放弃，未作为最终方案落地**。
>
> **最终落地方案**：所有按钮统一使用 `ctx.data.created_at` 定位消息，`session_ops.py` 的 `find_message_by_created_at()` 实现秒级模糊匹配。
>
> **放弃原因**（详见 `message_id_investigation.md` §6 的详细踩坑记录）：
> 1. `requestActions` 的 `ctx.data` 只暴露 `{id, created_at, input}`，不暴露 `message_id`
> 2. history 消息的 `message_id` 需改构建产物才能透传，升级会被覆盖
> 3. 需同时改 site-packages + 构建产物，维护成本高
>
> **保留理由**：本文档的架构分析（数据流、SSE 链路、Vendor SDK 渲染机制）对后续 QwenPaw 插件开发仍有参考价值，因此保留为历史存档。
>
> 分析日期：2026-07-11
> 目标：让 `window.QwenPaw.chat.actions.add()` / `requestActions.add()` 的 `onClick({ data })` 能获取到 session 文件中存储的真实消息 ID

---

## 1. 数据流全景

### 1.1 后端 SSE 消息流

```
envelope.py: Envelope.__init__()
  └─ _response.id = "response_" + uuid.uuid4().hex   ← 响应级别 ID
  └─ _message_id = _gen_msg_id()                       ← 消息级别 ID
       └─ "msg_" + uuid.uuid4().hex
  └─ _completed_message = Message(id=_message_id, ...)

通过 SSE 推送到前端：
  - response 对象：{ id: "response_xxxx", session_id, output: [...] }
  - message 对象：{ id: "msg_xxxx", type: "message", role, content, ... }
```

**两个关键 ID：**
| ID 类型 | 格式示例 | 生成位置 | 用途 |
|---------|----------|----------|------|
| Response ID | `response_abc123...` | `envelope.py:47` | SSE 流的 response 级别 ID |
| Message ID | `msg_def456...` | `envelope.py:53` | 单条消息的 ID，**也是 session 文件中存储的消息 ID** |

### 1.2 前端渲染链路

```
vendor SDK (AgentScopeRuntimeWebUI)
  ├─ 接收 SSE 事件流 → 构建 React state (messages list)
  │
  ├─ options.actions.list[]  →  AI 回复的气泡操作按钮
  │    └─ SDK 给每个 action 传入 { data: response_object }
  │         data = { id: "response_xxxx", output: [...], ... }
  │
  ├─ options.requestActions.list[]  →  用户消息的气泡操作按钮
  │    └─ SDK 给每个 action 传入 { data: request_object }
  │         data = { id: "msg_xxxx", input: [...], created_at, ... }
  │
  └─ options.cards:
       ├─ AgentScopeRuntimeRequestCard  →  HostRequestCard
       └─ AgentScopeRuntimeResponseCard →  HostResponseCard
            └─ 插件 render/slot 收到的 data：
                 request:  { id: "msg_xxx", ... }
                 response: { id: "response_xxx", output: [{ id: "msg_xxx", ... }], ... }
```

### 1.3 `ChatActionSpec.onClick` 传参

```typescript
// qwenpaw.d.ts:48-54
export interface ChatActionSpec {
  id: string;
  icon?: React.ReactElement;
  render?: (ctx: { data: unknown }) => React.ReactElement;
  onClick?: (ctx: { data: unknown }) => void;
}
```

`ctx.data` 的类型只是 `unknown` —— 插件只能被动接受 SDK 传过来的东西。

在 `index.tsx` 中观察 SDK 的实际传参：

**Response actions (AI 回复侧)：**
```typescript
// actions.list — onClick data 的 shape：
onClick: ({ data }: { data: CopyableResponse }) => { ... }
// CopyableResponse = { output?: CopyableMessage[] }
// data 是完整的 response 对象，id 是 "response_xxxx"
```

**Request actions (用户消息侧)：**
```typescript
// requestActions.list — onClick data 的 shape：
render: ({ data }: { data: { created_at?: number } }) => { ... }
// data 包含 created_at/input/id，id 是 "msg_xxxx"
```

当前插件的 `extractMessageId()`：
```javascript
function extractMessageId(data) {
  // AI 回复: 从 data.output[].type=="message" 的元素提取 id
  if (data.output && Array.isArray(data.output)) {
    for (var o of data.output) {
      if (o.type === "message" && o.id) return o.id;  // ← msg_xxx
    }
  }
  // 兜底
  if (data.id) return data.id;  // ← request 侧直接是 msg_xxx
  return "";
}
```

---

## 2. 现状与问题

### 2.1 当前已经工作的部分

| 场景 | data.id | extractMessageId() 提取结果 | 后端能否定位 |
|------|---------|---------------------------|-------------|
| **Request actions** (用户消息) | `msg_xxx` ✅ | `msg_xxx` ✅ | 能 ✅ |
| **Response actions** (AI 回复) | `response_xxx` ❌ | `output[0].id` = `msg_xxx` ✅ | 能 ✅ |

**所以实际上当前插件已经能从 `ctx.data` 中提取到 `msg_xxx` 格式的真实消息 ID。**

### 2.2 需要验证的问题

1. **`extractMessageId()` 是否在所有场景下都能稳定拿到 `msg_xxx`？**
   - `data.output` 数组中是否一定包含 `type === "message"` 的元素？
   - 当 AI 回复只有 tool calls 没有文本时，output 结构会不同吗？
   - 请求侧（requestActions）的 `data.input` 格式是否稳定？

2. **后端 API 是否真的能按 `msg_xxx` 定位？**
   - `session_ops.py` 中 `find_message_by_id_or_text()` 从 session 文件读取消息列表，按 `msg["id"]` 匹配
   - session 文件中存储的消息 ID 就是 `msg_xxx` 格式 ✅
   - 目前新旧格式兼容：旧格式 `[msg_dict, extra_list]` 对，新格式 `Msg dict` 列表

---

## 3. 方案对比

### 方案 A：完善现有方案（推荐 ✅）

**当前插件已经做对了大部分** —— `extractMessageId()` 从 `data.output` 中取出 `msg_xxx`，后端按该 ID 定位。

**修复点：**

#### 3.1 Response 侧【当前代码已实现】

```javascript
// 在 AI 回复的 onClick 中:
function extractMessageId(data) {
  if (data.output && Array.isArray(data.output)) {
    for (var o of data.output) {
      if (o.type === "message" && o.id) return o.id;  // "msg_xxx"
    }
  }
  // fallback
  if (data.id) return data.id;   // "response_xxx" (request侧才是 msg_xxx)
  return "";
}
```

**风险：** `data.output` **不一定**包含 `type === "message"` 的元素。当工具调用时，output 里可能只有 `type === "plugin_call"` 和 `type === "plugin_result"`，找不到 `msg_xxx`。

#### 3.2 请求侧【当前代码未充分处理】

```javascript
// 当前代码对 requestActions 的 data 只用了 data.id
// data.id 在 request 侧是 "msg_xxx" ✅ 直接可用
```

**所以请求侧（requestActions）的按钮是正常工作**的，因为 SDK 直接把 `msg_xxx` 传给了 `data.id`。

### 方案 B：后端新增查找消息 ID 的 API（补充）

在后端新增一个 API，前端传 `response_xxx`，后端根据 response_id 反查 msg_id：

```python
# 后端新增 API
@router.get("/session/{session_id}/resolve-msg-id")
async def resolve_message_id(session_id, response_id: str):
    """根据 response_id 找到对应的 msg_id"""
    # 读取 session 文件，在消息中查找 id == response_id 替换为 msg_id
    # 或者维护一个 response_id → msg_id 的映射
```

**但这样做太重了** —— 额外一次 API 调用 + 后端扫描全部消息。

### 方案 C：前端改写 data，在 HostBubbles 层注入 msg_id（上游改动）

修改 `HostBubbles.tsx`，在把 `data` 传给插件之前，把 `msg_id` 注入到 `data` 对象中：

```typescript
// HostBubbles.tsx 中改写 data
function injectMsgId(data) {
  // 从 data.output 中提取 msg_id 注入到 data 顶层
  const msgId = data.output?.find(o => o.type === "message")?.id;
  if (msgId) data.__msg_id = msgId;
  return data;
}
```

**缺点：** 需要改 QwenPaw 主仓库的 `HostBubbles.tsx`，插件和主仓库耦合。

### 方案 D：在 backend 中把 msg_id 写入 response 对象（✅ 最佳补充）

在 `envelope.py` 的 `emit_completed()` 中，把 `msg_id` 写入 `response` 对象：

```python
# envelope.py - 在 finalize 前
self._response.message_id = self._message_id  # 注入 msg_id
```

这样前端的 response `data` 中就能直接取到 `data.message_id`。

---

## 4. 推荐方案

> ⚠️ **现状（2026-07-12）**：以下方案是早期调研时的推荐。实际落地时已全面改用 `created_at` 时间匹配方案（见 `message_id_investigation.md` §6），`message_id` 方案已被放弃并回滚。`extractMessageId()` 相关代码已被 `getMessageId()` 替代并标记 `@deprecated`。

### 短期（插件内修复）：完善 `extractMessageId()`

当前代码已经能处理 80% 的场景。需要加固的场景：

```javascript
function extractMessageId(data) {
  if (!data) return "";

  // AI 回复: output 中找 type="message" 的元素
  var output = data.output;
  if (Array.isArray(output)) {
    for (var i = 0; i < output.length; i++) {
      var o = output[i];
      if (o.type === "message" && o.id) return o.id;  // "msg_xxx"
    }
    // 兜底：取 output 最后一个元素的 id
    // 在纯 tool_call 场景下这个可能有用
    for (var j = output.length - 1; j >= 0; j--) {
      if (output[j].id && output[j].id.startsWith("msg_")) return output[j].id;
    }
  }

  // 用户消息: data.id 是 "msg_xxx" 或 "response_xxx"
  if (data.id && data.id.startsWith("msg_")) return data.id;

  // 兜底: 按文本匹配（当前已有）
  return "";
}
```

### 中期（必做）：验证后端 `response_xxx` 的反查

如果 `extractMessageId()` 在 tool_call 场景下找不到 `msg_xxx`，则需要在后端维护 `response_id → msg_id` 映射：

```python
# envelope.py - 在 __init__ 中新增
self._response_mapping = {}  # response_id → last_msg_id

# 在 finalize 时记录
self._response_mapping[self._response.id] = self._message_id
```

然后后端 API 支持按 `response_xxx` 查找：
```python
# session_ops.py 增加
async def find_by_response_id(self, session_id, user_id, channel, response_id):
    """按 response_id 找到对应的消息"""
    messages = await self.get_messages(session_id, user_id, channel)
    # 从后往前找最近的一条 assistant 消息
    for i in range(len(messages) - 1, -1, -1):
        msg_text = self._get_msg_text(messages[i])
        role = self._get_role(messages[i])
        if role == "assistant" and msg_text:
            return i, messages[i]
    return -1, None
```

### 长期（可选）：QwenPaw 上游注入 msg_id

在 `envelope.py` 中，让 response 对象携带 `message_id`：

```python
# envelope.py - 在 emit_completed / emit_response_created 中
self._response.message_id = self._message_id
```

这样前端的 `ctx.data.message_id` 直接可用，插件无需做任何提取工作。

---

## 5. 关键源码位置速查

| 文件 | 关键行 | 说明 |
|------|--------|------|
| `qwenpaw.d.ts:48-54` | `ChatActionSpec` | `onClick: (ctx: { data: unknown })` 是 API 签名 |
| `index.tsx:2608-2633` | `wrapActionSpec()` | 插件 actions 的包装函数 |
| `index.tsx:2949-2986` | `actions.list` | response 侧 actions 配置 |
| `index.tsx:2987-3008` | `requestActions.list` | request 侧 actions 配置 |
| `HostBubbles.tsx` | 全文 | request/response 气泡 wrapper |
| `envelope.py:23-24` | `_gen_msg_id()` | `"msg_" + uuid.uuid4().hex` — 消息 ID 生成 |
| `envelope.py:47` | `self._response.id` | `"response_" + uuid.uuid4().hex` — Response ID |
| `envelope.py:53-55` | `_completed_message.id` | Message 对象的 id = `msg_xxx` |
| `envelope.py:113-115` | `_finalize_text_message()` | 消息完成时推送到前端 |
| `runtime.py` | `run()` | 8 阶段生命周期 + hooks 系统 |
| `session_hook.py` | 全文 | session 加载/保存 hook |
| `session.py` | `SafeJSONSession` | 会话文件读写（JSON） |
| `plugin.py` | 全文 | 插件后端路由 |
| `session_ops.py` | 全文 | 会话操作核心（消息查找/回退/分叉） |
| `frontend/index.js` | `extractMessageId()` | 消息 ID 提取函数 |

---

## 6. 需要确认的问题

1. **`data.output` 是否总是包含一个 `type === "message"` 的元素？**
   - 在纯工具调用（无文本回复）场景下，output 的结构是什么？
   - 能否提供一个真实环境中的 `ctx.data` JSON 示例？

2. **Request 侧的 `data.id` 是否总是 `msg_xxx` 格式？**
   - 目前看来是的（SDK 直接传消息对象），但需要确认

3. **后端是否能按 `response_xxx` 直接定位消息？**
   - 如果需要，可以通过从 session 文件中按 `role === "assistant"` + 文本匹配来反查
   - 更可靠的做法是维护 `response_id → msg_id` 映射

---

## 7. 后续发现：`output[0].metadata.original_id` 与 `created_at` 降级方案（2026-07-12）

### 7.1 发现：`output[0].metadata.original_id` 可拿到真实 UUID

助手消息的 `ctx.data` 中，`output[0].metadata.original_id` 包含了 session 文件中存储的真实消息 UUID（如 `b685a6ea104f493a91bd9266966fab78`），与 `msg.id` 完全一致。

### 7.2 局限性：严重受限

| 场景 | `output[0].metadata.original_id` | 说明 |
|------|----------------------------------|------|
| **历史加载的助手消息** | ✅ 可用 | 页面刷新后从 session 文件加载，`agentscope_msg_to_message()` 注入了 `metadata.original_id` |
| **即时新生成的助手消息** | ❌ 不可用 | SSE 流推送的新消息，`output[0]` 没有 `metadata.original_id` 字段 |
| **用户消息气泡** | ❌ 不可用 | `requestActions` 的 `ctx.data` 只有 `{id, created_at, input}`，没有 `output` 字段 |

**结论**：`output[0].metadata.original_id` 只能覆盖"历史加载的助手消息"这一种场景，不能作为通用方案。

### 7.3 实际方案：`created_at` 时间匹配

由于以上限制，退而求其次使用 `created_at` 作为消息定位的通用方案：

**前端**：`ctx.data.created_at` 在所有消息类型中都可获取
- 格式可能是 Unix 秒数（如 `1783851829`）或 ISO 字符串（如 `2026-07-12T18:23:49.798788`）

**后端**：`find_message_by_created_at()` 按时间匹配

**时区陷阱**：会话文件中的 `created_at` 是**本地时间**（Asia/Shanghai，无时区），而 Unix 时间戳解析后是 **UTC 时间**。匹配时必须：
1. 给无时区的消息时间补 `LOCAL_TZ`（UTC+8）
2. 把目标时间也转成本地时间再比较
3. 秒级模糊匹配（`strftime("%Y-%m-%d %H:%M:%S")` 比较）

**匹配精度**：秒级，同秒内的消息视为匹配。在并发不高的场景下（同一秒内不会有多条消息），这个精度足够。

### 7.4 消息 ID 定位方案总览

| 方案 | 适用范围 | 可靠性 | 状态 |
|------|---------|--------|------|
| `message_id`（UUID 精确匹配） | 新消息 SSE 流 + 历史消息注入 | ✅ 最高 | ❌ **已放弃并回滚**（前端拿不到 `message_id` 字段，见 `message_id_investigation.md` §6） |
| `created_at` 时间匹配 | 全部消息类型 | ✅ 高（秒级） | ✅ 已实现（**当前主力方案**） |
| `output[0].metadata.original_id` | 仅历史加载的助手消息 | ⚠️ 受限 | ❌ 已放弃（覆盖场景太少） |
| 文本+时间+邻居匹配 | 全部消息类型 | ❌ 已废弃 | 代码已注释保留 |

### 7.5 React 受控组件填值技巧（2026-07-12 沉淀）

**场景**：QwenPaw 的输入框是 React 受控组件，直接 `inputEl.value = xxx` 不会更新 React state。

**现象**：填入文字后按钮仍是灰色（禁用状态），点击输入框后文字自动消失（React 重新渲染覆盖）。

**修复**：使用 `nativeInputValueSetter` 绕过 React 的 value 属性代理：
```javascript
var nativeSetter = Object.getOwnPropertyDescriptor(
  window.HTMLTextAreaElement.prototype, "value"
).set;
nativeSetter.call(textarea, "新内容");
textarea.dispatchEvent(new Event("input", { bubbles: true }));
// 等 React 更新 state 后（~80ms）再操作按钮
```

### 7.6 regen 按钮的 rewind 策略（2026-07-12 沉淀）

**错误做法**：找到助手消息索引 `file_idx`，`messages[:file_idx]` 只删除助手消息 → 残留用户消息，前端再发一次导致两条用户消息重复。

**正确做法**：从助手消息往前找对应的用户消息索引 `user_idx`，`messages[:user_idx]` 删除整轮（用户消息+助手消息），再返回用户消息文本让前端重新发送。

### 7.7 `/noreply` slash 命令实现方式

**原理**：直接操作 session 文件追加一条用户消息，返回 `Msg` 阻止 agent 继续处理。

**注意**：`created_at` 必须用 `datetime.now().isoformat()` 字符串，不能用 `datetime.now()` 对象，否则 JSON 序列化失败。

### 7.8 Debug 开关设计

**实现**：通过 `localStorage` 控制，无需后端支持。
- 开启：`localStorage.setItem('SessionTools.debug', 'true'); location.reload();`
- 关闭：`localStorage.removeItem('SessionTools.debug'); location.reload();`
- 默认关闭，探查按钮 🔍📄 仅在 debug 模式下注册