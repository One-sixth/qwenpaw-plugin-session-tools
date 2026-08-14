# original_id 消息定位增强方案

## 背景

当前所有按钮（rewind/fork/delete/regen）使用 `ctx.data.created_at` 做秒级模糊匹配定位消息。  
新版 QwenPaw 的前端 `ctx.data` 中新增了 `original_id` 字段，可直接对应会话文件中的 `msg.id`，实现精确 UUID 定位。

## 目标

优先使用 `original_id` 精确匹配，不可用时回退到 `created_at` 模糊匹配。

---

## 1. 数据流

```
[用户点击按钮]
       ↓
  提取 original_id  ←  ctx.data.input[0].metadata.original_id  (用户气泡)
                   ←  ctx.data.output[0].metadata.original_id  (助手气泡)
       ↓
  GET/POST /api/session-tools/session/{id}/rewind?original_id=xxx&created_at=xxx
       ↓
  后端：有 original_id → find_message_by_id_or_text(message_id=original_id)
       无 original_id → find_message_by_created_at(created_at=xxx)
       ↓
  返回消息索引 → 执行操作
```

---

## 2. 改动文件

### 2.1 `frontend/index.js` — 前端提取 original_id

**新增工具函数** `getOriginalId(ctx)`：

```javascript
/**
 * 从 ctx.data 中提取 original_id（消息 UUID）
 *
 * 用户气泡：ctx.data.input[0].metadata.original_id
 * 助手气泡：ctx.data.output[0].metadata.original_id
 *
 * @param {Object} ctx - onClick 的上下文对象
 * @returns {string|null} original_id 或 null
 */
function getOriginalId(ctx) {
  if (!ctx || !ctx.data) return null;
  var data = ctx.data;
  // 用户气泡：input[0].metadata.original_id
  if (data.input && data.input[0] && data.input[0].metadata) {
    var oid = data.input[0].metadata.original_id;
    if (oid) return oid;
  }
  // 助手气泡：output[0].metadata.original_id
  if (data.output && data.output[0] && data.output[0].metadata) {
    var oid = data.output[0].metadata.original_id;
    if (oid) return oid;
  }
  return null;
}
```

**5 个按钮修改**：每个按钮的 `onClick` 中，提取 `original_id` 并加入 `params`：

| 按钮 | 当前 params | 新增字段 |
|------|------------|---------|
| ⏪ rewind | `created_at` | `+ original_id` |
| 🍴 fork | `created_at` | `+ original_id` |
| 🗑 delete | `created_at` | `+ original_id` |
| 🔄 regen | `created_at` | `+ original_id` |
| 📄 inspect | `created_at` | `+ original_id` |

**改动模式示例**（以 rewind 为例）：

```javascript
// 修改前
var createdAt = ctx.data.created_at;
var params = {
  session_id: sessionId,
  user_id: userInfo.user_id,
  channel: userInfo.channel,
  created_at: String(createdAt),
};

// 修改后
var createdAt = ctx.data.created_at;
var originalId = getOriginalId(ctx);
var params = {
  session_id: sessionId,
  user_id: userInfo.user_id,
  channel: userInfo.channel,
  created_at: String(createdAt),
};
if (originalId) {
  params.original_id = originalId;
}
```

**文档注释更新**：更新文件头注释，说明 `original_id` 优先方案。

---

### 2.2 `plugin.py` — 后端 HTTP handler 加 original_id 参数

**5 个路由修改**：

| 路由 | 方法 | 当前参数 | 新增参数 |
|------|------|---------|---------|
| `/session/{session_id}/rewind` | POST | `created_at` | `+ original_id` |
| `/session/{session_id}/regen` | POST | `created_at` | `+ original_id` |
| `/session/{session_id}/fork` | POST | `created_at` | `+ original_id` |
| `/session/{session_id}/message` | DELETE | `created_at` | `+ original_id` |
| `/session/{session_id}/message` | GET | `message_id`, `created_at` | 不变（已有） |

**改动模式示例**（以 rewind 为例）：

```python
# 修改前
@router.post("/session/{session_id}/rewind")
async def http_rewind(request: Request, session_id: str,
                       user_id: str = Query(...), channel: str = Query(...),
                       created_at: str = Query(...)):
    # 定位
    file_idx, target_msg = await op.find_message_by_created_at(
        session_id, user_id, channel,
        created_at=created_at,
    )

# 修改后
@router.post("/session/{session_id}/rewind")
async def http_rewind(request: Request, session_id: str,
                       user_id: str = Query(...), channel: str = Query(...),
                       original_id: str = Query(default=""),
                       created_at: str = Query(default="")):
    # 优先使用 original_id 定位
    if original_id:
        file_idx, target_msg = await op.find_message_by_id_or_text(
            session_id, user_id, channel,
            message_id=original_id,
        )
    else:
        file_idx, target_msg = await op.find_message_by_created_at(
            session_id, user_id, channel,
            created_at=created_at,
        )
```

**注意点**：
- `created_at` 改为可选（`default=""`），因为当 `original_id` 存在时不需要它
- 当 `original_id` 和 `created_at` 都为空时，返回 400 错误
- 每个路由都需加参数校验：至少提供一个

**公共定位逻辑提取**（可选优化）：

```python
async def _locate_message(
    op: SessionOperator,
    session_id: str, user_id: str, channel: str,
    original_id: str = "", created_at: str = "",
) -> tuple[int, Any]:
    """定位消息：优先 original_id 精确匹配，回退 created_at 模糊匹配。"""
    if original_id:
        return await op.find_message_by_id_or_text(
            session_id, user_id, channel,
            message_id=original_id,
        )
    if created_at:
        return await op.find_message_by_created_at(
            session_id, user_id, channel,
            created_at=created_at,
        )
    raise HTTPException(status_code=400, detail="至少提供 original_id 或 created_at 参数")
```

---

### 2.3 `session_ops.py` — 无需改动

`find_message_by_id_or_text()` 已支持按 `message_id` 精确匹配 `msg.id`：

```python
# 现有逻辑，可直接使用
if message_id:
    for i, msg in enumerate(messages):
        if isinstance(msg, list) and len(msg) > 0:
            mid = msg[0].get("id", "")
        elif isinstance(msg, dict):
            mid = msg.get("id", "")
        if mid == message_id:
            return i, msg
```

`original_id` 就是 `msg.id`，无需新方法。

---

## 3. 向前兼容

- 旧版 QwenPaw（无 `original_id` 注入）：前端 `getOriginalId()` 返回 `null`，自动回退 `created_at`
- 新版 QwenPaw（有 `original_id`）：优先精确匹配，失败时回退 `created_at`
- 后端 `original_id` 默认为空字符串，旧版前端不传此参数时完全不影响

---

## 4. 测试要点

1. **新版前端 + 新版后端**：`original_id` 存在 → 精确匹配 → 成功
2. **新版前端 + 旧版会话**：`original_id` 存在但会话文件无此 `msg.id` → 回退 `created_at`
3. **旧版前端 + 新版后端**：`original_id` 为空 → 纯 `created_at` 匹配，行为不变
4. **多 agent 场景**：`original_id` 在不同 workspace 间独立有效

---

## 5. 文件清单

| 文件 | 改动类型 | 改动量 |
|------|---------|-------|
| `frontend/index.js` | 新增 `getOriginalId()` + 5 按钮修改 + 注释更新 | ~30 行 |
| `plugin.py` | 5 路由加参数 + `_locate_message()` 辅助函数 + 注释更新 | ~40 行 |
| `session_ops.py` | 无需改动 | 0 行 |
| `plan_original_id.md` | 新增（本方案文档） | 新增 |