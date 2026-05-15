# Agent Bridge — 自定义指南

本文档说明如何为你的 agent 框架编写适配器配置。

---

## 配置结构

```yaml
# 全局设置
shared_dir: ~/.agent-bridge

# Agent 定义
agents:
  <key>:              # 配置键（任意名称，供人类阅读）
    id: <string>      # 消息的 from 字段值
    display_name:     # UI 中显示的名称（可选）
    cursor: line|timestamp  # 游标类型
    filter_from: <id> # 只处理谁的消息
    wakeup:           # 唤醒配置
      url: <string>
      method: POST|GET
      headers: {}
      auth:
        type: bearer|none
        token_path: <file_path>
        token_jsonpath: <json.path>
      body_template: {}  # 支持 {{message}} 和 {{from}} 变量
```

> 安全提示：`token_path` 指向的文件通常包含 API 密钥。建议设置 `chmod 600` 保护权限。

---

## cursor 类型选择

| 类型        | 适用场景                         | 优点               | 缺点               |
|-----------|--------------------------------|-------------------|-------------------|
| `line`    | 文件追加方稳定（不并行写）        | 精确，永不重复      | 并发写入可能漂移 |
| `timestamp` | 多 agent 写入 / 跨机器同步     | 并发安全           | 重复精度不足       |

推荐：**文件写入频繁的一方用 timestamp，写入较少的一方用 line**。

---

## body_template 变量

模板中可用变量：

| 变量          | 说明             |
|--------------|------------------|
| `{{message}}` | 对方的消息文本    |
| `{{from}}`    | 发送方的 agent ID |

---

## 常见 Agent 适配

### Hermes Agent

```yaml
wakeup:
  url: "http://127.0.0.1:8644/webhooks/agent-reply"
  method: POST
  body_template:
    message: "{{message}}"
```

Hermes 的 webhook 接收简单的 `{"message": "..."}` 格式。

### OpenClaw

```yaml
wakeup:
  url: "http://127.0.0.1:18789/tools/invoke"
  method: POST
  auth:
    type: bearer
    token_path: ~/.openclaw/openclaw.json
    token_jsonpath: gateway.auth.password
  body_template:
    tool: sessions_send
    args:
      sessionKey: "agent:main:main"
      message: "{{message}}"
```

OpenClaw 使用 sessions_send 工具，需要认证 token。

### Claude Code

```yaml
wakeup:
  url: "http://127.0.0.1:8080/claude/incoming"
  method: POST
  body_template:
    text: "{{message}}"
```

Claude Code 可以通过 STDIO 或 HTTP 接口唤醒（取决于部署方式）。

### 自建 agent

只要你的 agent 有一个 HTTP endpoint 接收消息，格式自由定义即可。

---

## 常用场景配置

### 场景：Hermes (momo) ↔ OpenClaw (susu)

见 `adapters/hermes.yaml` 和 `adapters/openclaw.yaml` 中的完整示例。

### 场景：同一台机器的两个 Hermes 实例

```yaml
agents:
  worker_a:
    id: worker_a
    cursor: line
    filter_from: worker_b
    wakeup:
      url: "http://127.0.0.1:8644/webhooks/worker-a-incoming"
      body_template:
        message: "{{message}}"

  worker_b:
    id: worker_b
    cursor: line
    filter_from: worker_a
    wakeup:
      url: "http://127.0.0.1:8645/webhooks/worker-b-incoming"
      body_template:
        message: "{{message}}"
```

### 场景：三个 agent 共享对话

可以扩展到 3 个 agent：`filter_from` 设置为空时，处理所有来自其他人的消息。
```yaml
agents:
  hub:
    id: hub
    cursor: line
    filter_from: ""  # 处理所有人的消息
    wakeup:
      url: "http://127.0.0.1:8644/webhooks/hub"
      body_template:
        message: "{{message}} (from: {{from}})"
```

---

## 环境变量覆盖

| 环境变量               | 用途                       |
|----------------------|----------------------------|
| `AGENT_BRIDGE_DIR`   | 覆盖 shared_dir            |
| `AGENT_ID`           | 覆盖默认 agent_id          |

在 cron/systemd 中设置环境变量，可以在不修改配置文件的情况下切换 agent。
