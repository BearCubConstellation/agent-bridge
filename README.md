# Agent Bridge — 通用异步 Agent 通信中间件

让两个 AI Agent 通过共享文件进行异步对话。
零轮询成本、消息不丢失、双方独立运行。

> 从 **Hermes Agent** 与 **OpenClaw** 的真实对话场景中提取并通用化。

## 原理

```
┌──────────────────────────────────────────────┐
│                  共享目录                      │
│                                              │
│  Agent A  ──写入──►  active.jsonl  ◄──写入──  Agent B  │
│     ▲                        │                    │
│     │ 每 3min                │ 每 3min            │
│     ▼                        ▼                    │
│  poll.py ◄───────── config ────────► poll.py      │
│     │                        │                    │
│     ▼                        ▼                    │
│  POST webhook               POST API              │
│  (唤醒 Agent A)              (唤醒 Agent B)       │
└──────────────────────────────────────────────┘
```

两个 agent 不直接对话。它们通过一个共享目录下的 `active.jsonl` 文件交换 JSON 消息。
每方的轮询脚本（由 cron/launchd/systemd 每 3 分钟调度）检测对方的新消息，
通过 webhook 唤醒本地 agent。无新消息时脚本零 token 消耗退出。

## 快速开始

### 1. 创建配置文件

```yaml
# bridge.yaml
shared_dir: ~/.agent-bridge

agents:
  alice:
    id: alice
    display_name: "Alice"
    color: "#ff6b6b"
    cursor: line
    filter_from: bob
    wakeup:
      url: "http://127.0.0.1:8644/webhooks/agent-reply"
      headers:
        Content-Type: application/json
      body_template:
        message: "{{message}}"

  bob:
    id: bob
    display_name: "Bob"
    color: "#4ecdc4"
    cursor: timestamp
    filter_from: alice
    wakeup:
      url: "http://127.0.0.1:18789/tools/invoke"
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

### 2. 启动轮询

```bash
# macOS
bash setup/macos.sh --agent alice --config ~/agent-bridge.yaml
bash setup/macos.sh --agent bob --config ~/agent-bridge.yaml

# Linux
bash setup/linux.sh --agent alice --config ~/agent-bridge.yaml
```

### 3. 发消息

```bash
# 从 alice 发给 bob
python3 core/send.py --bridge bridge.yaml "你好！"

# 或使用环境变量快速发消息
export AGENT_ID=alice
python3 core/send.py "你好！"
```

### 4. 打开 UI

```bash
python3 ui/server.py
# → http://127.0.0.1:7899
```

## 项目结构

```
agent-bridge/
├── protocol/SPEC.md          # 通信协议规范
├── core/
│   ├── send.py               # 消息发送工具
│   └── poll.py               # 轮询+唤醒脚本
├── adapters/
│   ├── hermes.yaml           # Hermes Agent 适配配置
│   └── openclaw.yaml         # OpenClaw 适配配置
├── ui/
│   ├── index.html            # 聊天时间线页面
│   └── server.py             # 本地 HTTP API + 配置管理服务
├── setup/
│   ├── macos.sh              # macOS launchd 部署
│   └── linux.sh              # Linux systemd/cron 部署
└── docs/
    ├── ARCHITECTURE.md
    ├── SETUP.md
    └── CUSTOMIZE.md
```

## 适配自己的 Agent

1. 在配置的 `agents` 下添加新 agent，定义：
   - `id` — 消息发送标识
   - `cursor` — 游标类型（`line` / `timestamp`）
   - `filter_from` — 只处理谁的消息
   - `wakeup` — webhook/API 的 URL、认证、请求体模板

2. 在每个 agent 机器上运行轮询脚本

3. 确保共享目录可读写（本地同一台机器，或通过 Syncthing/NFS 同步）

详细见 `docs/CUSTOMIZE.md` 和 `adapters/` 下的示例。

## 消息格式

每行一条 JSON，UTF-8：

```json
{"ts": "2026-05-15 14:24:47", "from": "alice", "msg": "你好"}
```

| 字段   | 说明                                  |
|--------|---------------------------------------|
| `ts`   | `YYYY-MM-DD HH:MM:SS`，24 小时制       |
| `from` | 发送方标识                            |
| `msg`  | 消息正文（可换行）                    |

## 依赖

- **运行时**：Python 3.8+（仅标准库）
- **可选**：`pyyaml`（无时自动用 JSON 配置文件）
- **UI**：无需服务端框架（Python 内置 http.server）

## License

MIT
