# 🛠️ QwenPaw会话增强工具 (qwenpaw-plugin-session-tools)

为 QwenPaw 提供会话管理能力：**回退对话**、**分叉会话**、**重新生成回复**、**删除消息**、**静默记录**。

支持两种使用方式：
- **WebUI 按钮**：在消息气泡下方显示操作按钮
- **控制台命令**：通过 `/rewind`、`/fork`、`/regen`、`/noreply` 命令操作

## 📋 功能

### WebUI 按钮

| 按钮 | 用户消息 | 助手消息 | 说明 |
|------|---------|---------|------|
| ⏪ 回退 | ✅ | ❌ | 删除该消息及之后所有，文本填入输入框 |
| 🍴 分叉 | ✅ | ✅ | 从该位置创建新会话 |
| 🗑 删除 | ✅ | ✅ | 删该用户消息+对应助手回复（或仅用户消息若无回复） |
| 🔄 重新生成 | ❌ | ✅ | 删除该轮并重新填入输入框发送 |

> 🔍📄 探查按钮默认隐藏，开启：`localStorage.setItem('SessionTools.debug', 'true')` 后刷新

### 控制台命令

| 命令 | 功能 | 说明 |
|------|------|------|
| `/rewind [N]` | ⏪ 回退 N 轮对话 | 删除最后 N 组用户+助手消息，默认 1 轮 |
| `/fork [N]` | 🍴 分叉新会话 | 从 N 轮前分叉出新会话（默认 0=全部分叉） |
| `/regen` | 🔄 重新生成回复 | 删掉最后一轮，让 AI 重新生成 |
| `/noreply 消息` | 📝 静默记录 | 记录用户消息但不触发 AI 回复 |

### 使用示例

**WebUI**：
- 点击用户消息的 ⏪ → 回退到该消息，文本填入输入框
- 点击任意消息的 🍴 → 从该位置分叉新会话
- 点击用户消息的 🗑 → 删除该用户消息+其助手回复（若无助手回复则仅删用户）
- 点击助手消息的 🔄 → 删除该轮，自动填入并发送用户消息重新生成

**控制台**：
```
/rewind         → 回退 1 轮
/rewind 3       → 回退 3 轮
/fork           → 完整复制当前会话为新会话
/fork 2         → 从 2 轮前分叉，保留最后 2 轮上下文
/regen          → 重新生成上一条回复
/noreply 收到   → 记录"收到"但不触发 AI 回复
```

## 📦 安装

```bash
qwenpaw plugin install D:\Data\github_repo\qwenpaw-plugin-session-tools --force
```

热安装后刷新页面（Ctrl+F5）生效。

## 🏗️ 架构

```
session-tools/
├── plugin.json        # 插件元数据
├── plugin.py          # 入口文件：slash 命令 + HTTP API
├── session_ops.py     # 核心操作：会话文件读写、消息查找
├── frontend/
│   └── index.js       # WebUI 前端：按钮注册、API 调用
└── README.md          # 本文件
```

### 关键设计

- **消息定位**：统一使用 `created_at` 时间戳（秒级模糊匹配），替代旧的 `message_id` 和 DOM 索引方案
- **Slash 命令**：通过 `register_slash_command()` 注册，返回 `Msg` 阻止 agent 继续，或返回 `None` 让 agent 继续处理
- **HTTP API**：提供 RESTful API 供 WebUI 按钮调用
- **会话操作**：`SessionOperator` 类封装会话文件读写，兼容新旧两种文件格式
- **WebUI 按钮**：通过 `chat.requestActions.add()`（用户消息）和 `chat.actions.add()`（助手消息）注册，React state 驱动

## ⚙️ 工作原理

### 消息定位：`created_at` 时间匹配

所有按钮使用 `ctx.data.created_at` 定位消息，而非 `message_id` 或 DOM 索引：

```
ctx.data.created_at (Unix秒数 / ISO字符串)
  → 后端 find_message_by_created_at()
  → 秒级模糊匹配（补 LOCAL_TZ 转本地时间）
  → 返回消息索引 + 消息对象
```

**时区处理**：会话文件中的 `created_at` 是无时区本地时间（Asia/Shanghai），Unix 时间戳是 UTC。匹配时给消息时间补 `LOCAL_TZ`（UTC+8），目标时间转本地时间后再比较。

### WebUI 按钮

#### ⏪ 回退
```
1. 取 ctx.data.created_at → 后端定位消息
2. messages[:file_idx] 保留该消息之前
3. 返回删掉的消息文本（填入输入框）
4. 前端刷新页面
```

#### 🍴 分叉
```
1. 取 ctx.data.created_at → 后端定位消息
2. messages[:file_idx+1] 保留到该消息（含）
3. 创建新会话文件 + 注册到 chat_manager
```

#### 🗑 删除
```
1. 取 ctx.data.created_at → 后端定位消息
2. 用户消息：检查下一条是否是助手消息
   - 是 → 删用户+助手（end=file_idx+2）
   - 否 → 只删用户（end=file_idx+1）
3. 助手消息：删前面的用户+自己（start=file_idx-1, end=file_idx+1）
```

#### 🔄 重新生成（仅助手消息）
```
1. 取 ctx.data.created_at → 后端定位助手消息
2. 从助手消息向前找对应的用户消息索引 user_idx
3. messages[:user_idx] 删除整轮
4. 返回用户消息文本
5. 前端用 nativeInputValueSetter 填入输入框 → 点击发送按钮
6. 1.5秒后刷新页面
```

### 控制台命令

#### `/noreply 消息`
```
1. 解析 args 获取消息文本
2. 直接操作 session 文件追加一条用户消息
3. 返回 Msg 阻止 agent 继续处理
```

其他命令（`/rewind`、`/fork`、`/regen`）逻辑与 WebUI 按钮相同。

## 📂 会话文件位置

```
{workspace_dir}/sessions/{channel}/{user_id}_{sanitized_session_id}.json
```

支持新旧两种格式：
- **新格式**（`agent.state.context`）：Msg dict 列表，`created_at` 为 DateTime 对象
- **旧格式**（`agent.memory.content`）：[msg_dict, extra] 对列表

## 🔧 技术细节

- **运行目录**：`~/.qwenpaw/plugins/qwenpaw-plugin-session-tools/`
- **插件类型**：`command`
- **消息定位**：`created_at` 时间戳（秒级模糊匹配）
- **输入框填值**：`nativeInputValueSetter`（React 受控组件兼容）
- **发送按钮**：`button.qwenpaw-sender-actions-btn`
- **调试开关**：`localStorage.setItem('SessionTools.debug', 'true')`
- **多 agent 支持**：`SessionOperator` 按 workspace_dir 缓存

## 📝 注意事项

- **修改后需要 Ctrl+F5 硬刷新**，浏览器可能缓存旧 JS
- **`created_at` 精度为秒级**，同秒内多条消息可能匹配不准确
- **🔄 regen 后 1.5 秒自动刷新**，等待 agent 生成回复
- **`/noreply` 不会触发 AI 回复**，仅记录用户消息

## 🧩 依赖

- QwenPaw >= 2.0.0
- Python >= 3.10

## 📄 许可

MIT License

## 👥 作者

- [one-sixth](https://github.com/one-sixth)