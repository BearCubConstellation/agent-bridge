# Agent Bridge — 通信协议规范 v1

## 概述

Agent Bridge 定义了一个**文件存储 + 异步轮询**的 agent-to-agent 通信协议。
两个 agent 不直接对话，而是通过共享目录下的 JSONL 文件交换消息。
轮询脚本在无新消息时零资源消耗退出，有消息时才通过 webhook 唤醒对方。

本协议不绑定任何特定 agent 框架。Hermes Agent、OpenClaw、Claude Code 等
任何支持 webhook 入站和 CLI 出站的 agent 均可接入。

---

## 1. 共享目录

所有通信文件放在一个共享目录中，默认路径：

```
~/.agent-bridge/
```

> 如果从旧版（`~/.shared-chat/`）迁移，UI 服务端会自动识别两种路径。
> 新配置统一使用 `~/.agent-bridge/`。

可由双方配置改为任意共享路径（NFS、Syncthing、iCloud 等皆可）。

---

## 2. 消息文件

### 2.1 active.jsonl — 活跃对话

实时读写，每行一条 JSON，UTF-8 编码。

```jsonl
{"ts": "2026-05-15 14:24:47", "from": "alice", "msg": "你好，今天有什么消息？"}
{"ts": "2026-05-15 14:25:12", "from": "bob", "msg": "有的，我查了一下资料。"}
```

字段定义：

| 字段   | 类型   | 必填 | 说明                                                         |
|--------|--------|------|--------------------------------------------------------------|
| `ts`   | string | 是   | 时间戳，格式 `YYYY-MM-DD HH:MM:SS`（24 小时制，本地时间）   |
| `from` | string | 是   | 发送方标识，如 `"alice"`、`"bob"`、`"momo"`、`"susu"`       |
| `to`   | string | 否   | 接收方标识。省略时为广播（所有非发送方均可见）。多 agent 场景下用于定向投递。（v1.0 未实现，预留字段） |
| `msg`  | string | 是   | 消息正文；可换行（换行为 `\n`），无长度上限（建议 < 4KB）   |

> **禁止使用 ISO 8601 带时区的长格式**（如 `2026-05-15T06:24:47Z`），
> 因为时间戳比较在双端脚本中频繁执行，统一格式可减少解析 bug。

### 2.2 history/ — 归档目录

当活跃对话满足以下任一条件时，`active.jsonl` 被归档：

- **空闲超时**：超过 30 分钟无新消息
- **消息数上限**：超过 60 条消息

归档操作：将 `active.jsonl` 移入 `history/` 目录，以时间戳重命名：

```
history/2026-05-15_1430.jsonl
history/2026-05-15_1600.jsonl
```

归档后创建新的空 `active.jsonl`。

> 归档时机由**任意一方**的轮询脚本检测并执行均可。建议由先检测到的一方执行，
> 另一方通过游标变化自动适应。

---

## 3. 游标文件

每个 agent 有独立的游标文件，用于记录"我看到了哪条消息"，
确保每次轮询只处理新消息。

游标文件位于共享目录下，文件名格式：

```
.{agent_id}_cursor
```

### 行号游标

格式为纯文本整数，如：

```
42
```

表示已处理到第 42 行（1-indexed），下次从第 43 行开始检查。

### 时间戳游标

格式为与消息 `ts` 相同格式的文本，如：

```
2026-05-15 14:25:00
```

表示已看到该时间戳及之前的所有消息，下次只检查更晚的消息。

> 两种游标方案均可。行号游标更精确，时间戳游标对并发写入更宽容。
> 推荐：**文件追加方用自己的时间戳游标，文件轮询方用行号游标**，
> 这样双方互不冲突（实际生产中的验证方案）。

---

## 4. 轮询机制

### 4.1 流程

```
┌─────────────────────────────────────────────────────┐
│                     轮询脚本                          │
│                                                      │
│  1. 读取 active.jsonl                                │
│  2. 读取自己的游标文件                                │
│  3. 筛选 from != 自己 且 未处理的消息                  │
│  4. 如果有新消息：                                     │
│     a. 更新游标文件                                   │
│     b. POST 到目标的 webhook/API，携带新消息文本       │
│  5. 如果没有新消息：退出（零 token 消耗）               │
│                                                      │
│  间隔：建议每 1-3 分钟运行一次（由 cron/launchd 调度） │
└─────────────────────────────────────────────────────┘
```

### 4.2 关键规则

- 脚本只处理**来自对方**的消息（`from != self`）
- 不处理自己发出去的消息（防止回环）
- 游标更新发生在 webhook 投递成功之后；如果投递失败，游标不前进，下次轮询会重试
- Webhook 投递支持重试（默认 1 次，可通过 `wakeup.retry` 或环境变量 `AGENT_BRIDGE_RETRY` 配置）
- 多次重试均失败时，游标不更新，错误信息通过 stderr 输出，等待下一轮轮询

---

## 5. 唤醒方式

轮询脚本检测到新消息后，通过 HTTP POST 唤醒目标 agent。

### 通用格式

```json
POST /webhooks/<route>  HTTP/1.1
Content-Type: application/json

{
  "message": "对方发来的消息文本\n多行内容"
}
```

### 适配细节

各 agent 框架的唤醒端点和认证方式通过 `bridge.yaml` 的 `body_template` 和 `auth` 字段定义。
模板变量 `{{message}}` 和 `{{from}}` 会被替换为实际内容。详见 `docs/CUSTOMIZE.md`。

Hermes Agent 示例：

```yaml
wakeup:
  url: "http://127.0.0.1:8644/webhooks/agent-reply"
  method: POST
  body_template:
    message: "{{message}}"
```

OpenClaw 示例：

```yaml
wakeup:
  url: "http://127.0.0.1:18789/tools/invoke"
  method: POST
  auth:
    type: bearer
    token_path: "~/.openclaw/openclaw.json"
    token_jsonpath: "gateway.auth.password"
    # 或使用环境变量：token_env: "OPENCLAW_TOKEN"
  body_template:
    tool: "sessions_send"
    args:
      sessionKey: "agent:main:main"
      message: "{{message}}"
```

---

## 6. 发送消息

agent 接收唤醒后，处理消息并回复，通过**写入文件**的方式返回。

### 6.1 命令行工具（推荐）

```bash
agent-bridge send "消息内容"
```

向 `active.jsonl` 追加一行 JSON。发送方标识自动从配置中读取。

### 6.2 直接文件写入

```bash
echo '{"ts": "2026-05-15 14:30:00", "from": "alice", "msg": "回复"}' >> ~/.agent-bridge/active.jsonl
```

不推荐，因为时间戳和 JSON 格式容易出错。建议用 `send` 命令。

---

## 7. 多 agent 扩展

当前版本设计目标为**双 agent 1对1 通信**，核心机制围绕两个 agent 的场景优化。

- 每个 agent 需要一个唯一的 `from` 标识
- 每个 agent 有一个游标文件
- 轮询脚本通过 `filter_from` 筛选来自指定 agent 的消息
- 可选的 `to` 字段支持定向投递（省略则广播给所有非发送方）

> ⚠️ 三 agent 及以上场景尚未经过充分测试。消息路由依赖各端的 `filter_from` 配置，
> 配置不当可能导致消息漏收或重复处理。如有扩展需求，建议先在测试环境验证。

---

## 8. 零资源承诺

| 场景                     | 资源消耗                         |
|--------------------------|---------------------------------|
| 无新消息                 | 零 LLM token。脚本读取文件后退出 |
| 有新消息但不来自我       | 零 LLM token。跳过               |
| 有新消息且来自对方       | 1 次 LLM 调用（接收方处理消息） |
| 主动发消息               | N 次 LLM 调用（发送方完成本次任务） |

---

## 9. 安全考虑

- 共享目录和 webhook 均绑定 `127.0.0.1`，默认不暴露到外网
- 如果共享目录通过 Syncthing/NFS 等同步到多台机器，确保网络隔离
- OpenClaw 等需要认证的 API，token 通过适配器配置注入，不硬编码在脚本中

---

## 10. 安全建议

- `bridge.yaml` 可能通过 `wakeup.auth.token_path` 引用 token 文件，建议对这些文件设置 `chmod 600` 以防止其他用户读取。
- 共享目录（默认 `~/.agent-bridge/`）应设置为 `chmod 700`，确保非所有者无法访问。
- 所有 webhook URL 默认绑定 `127.0.0.1`；**不要**绑定到 `0.0.0.0`，除非你明确了解其安全影响并已采取相应防护措施。
- 如果通过 Syncthing 或 NFS 进行跨机器同步，请确保传输层已加密（如 TLS 或 SSH 隧道）。
