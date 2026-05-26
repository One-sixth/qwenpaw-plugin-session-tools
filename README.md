# 🛠️ QwenPaw会话增强工具 (qwenpaw-plugin-session-tools)

为 QwenPaw 提供会话管理能力：**回退对话**、**分叉会话**、**重新生成回复**。

## 📋 功能

| 命令 | 功能 | 说明 |
|------|------|------|
| `/rewind [N]` | ⏪ 回退 N 轮对话 | 删除最后 N 组用户+助手消息，默认 1 轮 |
| `/fork [N]` | 🍴 分叉新会话 | 从 N 轮前分叉出新会话（默认 0=全部分叉） |
| `/regen` | 🔄 重新生成回复 | 删掉最后一轮，让 AI 重新生成 |

### 使用示例

```
/rewind         → 回退 1 轮
/rewind 3       → 回退 3 轮
/fork           → 完整复制当前会话为新会话
/fork 2         → 从 2 轮前分叉，保留最后 2 轮上下文
/regen          → 重新生成上一条回复
```

## 📦 安装

### 方法一：官方命令安装（推荐）

```bash
qwenpaw plugin install <插件目录路径> --force
```

例如：

```bash
qwenpaw plugin install D:/插件测试/qwenpaw-plugin-session-tools --force
```


### 方法二：QwenPaw的插件管理安装（QwenPaw版本大于v1.8.1 推荐）

前往QwenPaw的webui中的插件管理，点击安装插件，然后把下载的插件压缩包拖放进去

### 方法三：手动复制到插件目录

将 `qwenpaw-plugin-session-tools` 目录复制到插件目录：

```bash
cp -r qwenpaw-plugin-session-tools ~/.qwenpaw/plugins/
```

### 加载插件

使用官方命令安装方法，立即生效。手动复制方法，重启 QwenPaw 后生效。

## 🏗️ 架构

```
session-tools/
├── plugin.json      # 插件元数据（名称、版本、入口等）
├── plugin.py        # 入口文件：命令拦截、monkey patch、命令分发
├── session_ops.py   # 核心操作：会话文件的读写、消息解析、rollback/fork/regen
└── README.md        # 本文件
```

### 关键设计

- **命令拦截**：通过 monkey patch `AgentRunner.query_handler` 拦截 `/fork`、`/rewind`、`/regen` 命令，在命令到达 Agent 之前处理
- **会话操作**：`SessionOperator` 类封装了会话文件的读取、解析、写入，自动处理多 agent 工作区隔离
- **消息格式**：实际消息存储在 `agent.memory.content` 中，每条消息为 `[{msg_dict}, {extra}]` 的列表对格式
- **WebUI 集成**：
  - `/fork` 通过 `ChatManager.create_chat()` 注册新会话，自动命名 `Fork #N 原名称`（编号递增）
  - `/rewind` 提示刷新页面以显示更新后的会话
  - `/regen` 即时重跑 AI 流程，无需刷新

## ⚙️ 工作原理

### `/rewind [N]`

```
1. 读取会话文件中的消息列表
2. 找到最后 N 个用户消息的索引位置
3. 截断：保留该位置之前的所有消息
4. 写回会话文件
```

### `/fork [N]`

```
1. 读取源会话文件
2. 若 N>0：保留最后 N 轮对话的消息
3. 生成新会话 ID（基于源 ID + 时间戳）
4. 写入新会话文件
5. 通过 ChatManager 注册，WebUI 可见
```

### `/regen`

```
1. 截断会话：删除最后一条用户消息及之后的所有内容
2. 提取该用户消息的文本
3. 用该文本替换 "/regen" 命令
4. 走原始 Agent 处理流程 → AI 重新生成回复
```

## 📂 会话文件位置

```
{workspace_dir}/sessions/{channel}/{user_id}_{sanitized_session_id}.json
```

示例：
```
C:/Users/ABC/.qwenpaw/workspaces/default/sessions/wecom/企微会话1.json
```

## 🔧 技术细节

- **运行目录**：`~/.qwenpaw/plugins/qwenpaw-plugin-session-tools/`
- **插件类型**：`command`
- **内部 API**：使用 `SafeJSONSession` 的 `update_session_state` / `get_session_state_dict` 方法
- **多 agent 支持**：`SessionOperator` 按 `agent_id` 缓存，每个 agent 工作区独立
- **消息存储**：`state.agent.memory.content`（列表对格式）
- **新会话注册**：通过 `ChatManager.create_chat(ChatSpec)` 确保 WebUI 可见

## 📝 注意事项

- `/rewind` 后请刷新 WebUI 页面以查看更新后的会话
- `/regen` 会丢弃上一轮的所有上下文（包括 tool_call、搜索结果等），请确保确认后再使用
- `/regen` 在第一条用户消息时不可用
- 会话文件修改前会自动备份到 `{workspace_dir}/sessions_backup/` 目录

## 🧩 依赖

- QwenPaw >= 1.1.0
- Python >= 3.10

## 📄 许可

MIT License

## 👥 作者

- [one-sixth](https://github.com/one-sixth)
