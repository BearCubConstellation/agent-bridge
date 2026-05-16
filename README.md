# Agent Bridge — 通用异步 Agent 通信中间件

> ⚠️ **开发中 — 请勿在生产环境使用。API/配置格式可能无预警变更。**
> 
> 当前阶段：核心功能可用，配置体验正在重构（见 `.hermes/plans/`）。欢迎测试和反馈。

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
│  poll.py ◄───────────── config ─────────────► poll.py      │
│     │                        │                    │
│     ▼                        ▼                    │
│  POST webhook               POST API              │
│  (唤醒 Agent A)              (唤醒 Agent B)       │
└──────────────────────────────────────────────┘
```

两个 agent 不直接对话。它们通过共享目录下的 `active.jsonl` 文件交换 JSON 消息。
每方的轮询脚本（由 cron/launchd/systemd 每 3 分钟调度）检测对方的新消息，
有消息时通过 webhook 唤醒本地 agent。无新消息时脚本零 token 消耗，直接退出。

---

## 安装前提

- **Python 3.8+**
- **Git**（仅一键安装需要，安装后不再依赖）

检查：

```bash
python3 --version && git --version
```

Windows 用户请从官网安装：
- Python: https://www.python.org/downloads/（勾选 **Add Python to PATH**）
- Git: https://git-scm.com/download/win

安装后重启终端，再执行安装命令。

## 快速开始

### 一键安装

**macOS / Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.sh | bash
```

**Windows (PowerShell):**

```powershell
powershell -c "irm https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.ps1 | iex"
```

### 初始化

```bash
bridge setup
```

交互式向导引导你配置：共享目录 → Agent 信息 → Webhook → 开机自启。完成后即可使用。

### 启动

```bash
bridge start          # 启动 UI 服务 → http://127.0.0.1:7899
bridge start --open   # 启动并打开浏览器
```

### 发送消息

```bash
bridge send "你好！"   # 发送消息
bridge post "你好！"   # send 的别名
```

### 更多命令

```bash
bridge status         # 查看运行状态和配置摘要
bridge open           # 打开浏览器
bridge config         # 查看当前配置
bridge stop           # 停止 UI 服务
bridge restart        # 重启服务
bridge uninstall      # 卸载（停止服务 + 移除命令，可选删数据）
bridge version        # 查看版本
```

### 手动安装（开发者）

```bash
git clone https://github.com/SusuAgent/agent-bridge.git
cd agent-bridge

# 直接用 CLI
python3 cli/bridge setup
python3 cli/bridge start --open

# 或直接启动 server
python3 ui/server.py --open
```

服务器会自动：
- 检测或生成 `bridge.yaml` 配置文件
- 每 3 分钟轮询一次，检查对方 agent 的新消息
- 有消息时通过 webhook 唤醒对方
- 超过 60 条或空闲 30 分钟时自动归档

### 在页面中配置

点击顶部 Agent Badge（彩色圆点）编辑身份：

| 选项 | 说明 |
|------|------|
| Agent ID | 消息发送标识，改后游标文件自动迁移 |
| 显示名称 | UI 气泡中显示的名字 |
| 颜色 | 圆形头像和气泡标签的颜色 |

页脚显示轮询状态：绿点运行中，灰点已暂停。
单击 ▶/∥ 按钮暂停/恢复轮询，双击 ▶ 立即触发一次轮询。

---

## 项目结构

```
agent-bridge/
├── cli/
│   └── bridge               # CLI 命令行工具
├── core/
│   ├── send.py               # 消息发送
│   └── poll.py               # 轮询 + 自动归档
├── ui/
│   ├── index.html            # 聊天时间线 + 设置页
│   └── server.py             # API + 配置管理 + 轮询
├── install.sh                # 一键安装 (macOS/Linux)
├── install.ps1               # 一键安装 (Windows)
├── setup/
│   ├── macos.sh              # macOS launchd 部署
│   └── linux.sh              # Linux systemd/cron 部署
├── adapters/
│   ├── hermes.yaml           # 配置模板 (Hermes ↔ OpenClaw)
│   └── openclaw.yaml
├── protocol/SPEC.md          # 通信协议规范
├── docs/
│   ├── ARCHITECTURE.md
│   ├── SETUP.md
│   └── CUSTOMIZE.md
├── tests/                    # 单元测试 (92 tests)
├── README.md
└── LICENSE
```

## 配置参考

`bridge.yaml` 在首次启动 UI 时自动生成，你也可以手动编辑：

```yaml
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
      method: POST
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

UI 也能修改 ID、名称和颜色，并自动写回 `bridge.yaml`。

## 适配自己的 Agent

每个 agent 在配置中需要定义：

| 字段          | 说明                                 |
|---------------|--------------------------------------|
| `id`          | 发送消息时的 `from` 标识             |
| `cursor`      | `line`（行号）或 `timestamp`（时间戳） |
| `filter_from` | 只处理哪个 agent 的消息               |
| `wakeup`      | webhook/API 的 URL、认证、请求体模板   |

详细见 `docs/CUSTOMIZE.md`。

## 消息格式

每行一条 JSON，UTF-8：

```json
{"ts": "2026-05-15 14:24:47", "from": "alice", "msg": "你好"}
```

| 字段   | 说明                            |
|--------|--------------------------------|
| `ts`   | `YYYY-MM-DD HH:MM:SS`，24 小时制 |
| `from` | 发送方标识                      |
| `msg`  | 消息正文（可换行）              |

## 依赖

- **运行时**：Python 3.8+（仅标准库）
- **可选**：`pyyaml`（未安装时自动用 JSON）
- **UI**：无外部框架（Python 内置 http.server）

## 许可证

MIT
