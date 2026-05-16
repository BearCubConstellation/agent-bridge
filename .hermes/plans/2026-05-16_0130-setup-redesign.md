# 配置体验重构计划

## 目标

取消命令行交互式配置向导，将 Agent 配置全部转移到 WebUI，实现"安装 → 启动 → 浏览器里配"的无缝体验。

## 核心改动

1. **删除 `bridge setup`** — 不再有交互式向导
2. **`bridge start` 首次运行自动初始化** — 创建默认配置 + 安装自启服务 + 打开浏览器
3. **WebUI 成为唯一配置入口** — 首次访问显示引导页，无 agent 时引导创建
4. **自启服务默认安装** — 不询问用户

---

## Step 1: 改造 CLI - `bridge setup` 删除

### 改动文件

- `cli/bridge`

### 具体变更

1. **删除 `cmd_setup` 函数**（第 182-296 行）—— 整个交互式向导
2. **删除辅助函数 `_ask`、`_ask_yn`、`_ask_choice`**（第 135-163 行）—— 这些只被 setup 使用
3. **从 argparse 中删除 `setup` 子命令**（`p = sub.add_parser("setup", ...)` 那一段）
4. **从命令分发表 `commands` 中删除 `"setup": cmd_setup`**
5. **删除 `_install_service`、`_install_launchd`、`_install_systemd`**（第 299-383 行）—— 这些移到 `cmd_start` 中

### 保留的命令

```
bridge start          # 启动 UI 服务（首次运行自动初始化）
bridge stop           # 停止
bridge restart        # 重启
bridge status         # 查看状态
bridge send "..."     # 发送消息
bridge post "..."     # 别名
bridge open           # 打开浏览器
bridge config         # 查看配置
bridge version        # 版本
```

---

## Step 2: 改造 CLI - `bridge start` 首次运行自动初始化

### 改动文件

- `cli/bridge` — `cmd_start` 函数

### 流程

```
bridge start
  ├─ shared_dir (~/.agent-bridge/) 不存在？
  │   └─ 创建
  ├─ bridge.yaml 不存在？
  │   ├─ 写入默认配置（空 agents）
  │   └─ 创建空的 active.jsonl
  ├─ 自启服务未安装？
  │   ├─ macOS: 安装 launchd plist（每 3 分钟轮询）
  │   └─ Linux: 安装 systemd timer
  ├─ 启动 UI 服务
  ├─ 写 PID 文件
  └─ 打开浏览器（--open 时）
```

### 默认 bridge.yaml 模板

```yaml
shared_dir: ~/.agent-bridge
agents: {}
```

不预设任何 agent 信息。agent 配置全部在 WebUI 中完成。

### 自启服务安装逻辑

将原来 `_install_service`/`_install_launchd`/`_install_systemd` 移到 `cmd_start` 中，检测到服务未安装时自动安装（不询问）。

关键：如果已安装则不重复安装，避免每次 start 都重载。

检测方式：
- macOS: `launchctl list com.agent-bridge.poll 2>/dev/null`
- Linux: `systemctl --user is-enabled agent-bridge-poll.timer 2>/dev/null`

### 与服务的关系

当前的问题是：轮询脚本需要知道 `agent_id`（本机角色），而 agent ID 现在要到 WebUI 才能配。这产生了一个鸡生蛋的问题。

**方案**：`bridge start` 阶段不安装轮询服务。轮询由 UI 服务内置的 `PollManager` 负责（当前 server.py 已经实现了）。用户只需启动 UI 服务，在 WebUI 中配置完 agent 后，WebUI 自动开始轮询。

也就是说：
- **`bridge start` 只启动 UI 服务 + 自动打开浏览器**
- **不自装 launchd/systemd 轮询定时器**
- **轮询完全由 server.py 内部的 PollManager 管理**（当前已有实现）
- **`bridge start --daemon` 可选参数**：以 daemon 模式运行 UI 服务（后台），不打开浏览器

---

## Step 3: 本机 Agent 自动发现

### 改动文件

- `ui/server.py` — 新增 `GET /api/discover` 端点
- `adapters/hermes.yaml` — 已有的 Hermes 适配模板（参考用）
- `adapters/openclaw.yaml` — 已有的 OpenClaw 适配模板（参考用）

### 原理

`bridge start` 启动 UI 服务后，前端引导页调用 `GET /api/discover`，
服务端扫描本机已知的 Agent 框架配置，返回检测到的 Agent 列表。

检测目标：

| 框架 | 检测方式 | 读取的信息 |
|------|----------|-----------|
| Hermes Agent | `~/.hermes/config.yaml` 是否存在 | 默认 model、webhook 路由（端口 + route 名称）、agent 角色名 |
| OpenClaw | `~/.openclaw/openclaw.json` 是否存在 | gateway 端口、认证 token 路径和 jsonpath |
| 已有 agent-bridge | `~/.agent-bridge/active.jsonl` 中已有消息的 from 字段 | 通过历史消息反推 Agent ID |

### API 签名

```
GET /api/discover
```

响应：

```json
{
  "ok": true,
  "detected": [
    {
      "source": "hermes",
      "id": "momo",
      "display_name": "墨墨 (Hermes)",
      "color": "#ff6b6b",
      "wakeup": {
        "url": "http://127.0.0.1:8644/webhooks/agent-reply",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body_template": {"message": "{{message}}"}
      }
    },
    {
      "source": "openclaw",
      "id": "susu",
      "display_name": "苏苏 (OpenClaw)",
      "color": "#4ecdc4",
      "wakeup": {
        "url": "http://127.0.0.1:18789/tools/invoke",
        "method": "POST",
        "auth": {
          "type": "bearer",
          "token_path": "~/.openclaw/openclaw.json",
          "token_jsonpath": "gateway.auth.password"
        },
        "body_template": {
          "tool": "sessions_send",
          "args": {
            "sessionKey": "agent:main:main",
            "message": "{{message}}"
          }
        }
      }
    }
  ]
}
```

### 实现细节

在 `server.py` 中新增 `_detect_agents()` 函数：

```python
def _detect_agents():
    """扫描本机已知 Agent 框架，返回检测到的 Agent 列表。"""
    agents = []
    hermes_cfg = Path.home() / ".hermes" / "config.yaml"
    if hermes_cfg.exists():
        # 读取 Hermes webhook 路由
        cfg = read_yaml(hermes_cfg)
        routes = (cfg or {}).get("platforms", {}).get("webhook", {}).get("extra", {}).get("routes", {})
        port = (cfg or {}).get("platforms", {}).get("webhook", {}).get("extra", {}).get("port", 8644)
        model = (cfg or {}).get("model", {}).get("default", "unknown")

        if routes:
            for route_name, route_cfg in routes.items():
                agent_id = route_name.replace("-reply", "").replace("-receive", "")
                agents.append({
                    "source": "hermes",
                    "id": agent_id,
                    "display_name": f"{agent_id.capitalize()} (Hermes)",
                    "color": "#ff6b6b",
                    "wakeup": {
                        "url": f"http://127.0.0.1:{port}/webhooks/{route_name}",
                        "method": "POST",
                        "headers": {"Content-Type": "application/json"},
                        "body_template": {"message": "{{message}}"},
                    },
                })

    openclaw_cfg = Path.home() / ".openclaw" / "openclaw.json"
    if openclaw_cfg.exists():
        try:
            oc = json.loads(openclaw_cfg.read_text())
            oc_port = oc.get("gateway", {}).get("port", 18789)
            agents.append({
                "source": "openclaw",
                "id": "susu",
                "display_name": "苏苏 (OpenClaw)",
                "color": "#4ecdc4",
                "wakeup": {
                    "url": f"http://127.0.0.1:{oc_port}/tools/invoke",
                    "method": "POST",
                    "auth": {
                        "type": "bearer",
                        "token_path": "~/.openclaw/openclaw.json",
                        "token_jsonpath": "gateway.auth.password",
                    },
                    "body_template": {
                        "tool": "sessions_send",
                        "args": {"sessionKey": "agent:main:main", "message": "{{message}}"},
                    },
                },
            })
        except Exception:
            pass

    # 去重：同一 type+id 只保留一个
    seen = set()
    unique = []
    for a in agents:
        key = (a["source"], a["id"])
        if key not in seen:
            seen.add(key)
            unique.append(a)

    return unique
```

> 检测逻辑不要求 Agent 当前处于运行状态。
> 只要配置文件存在，就认为该 Agent 可用。
> 如果有多个 Hermes route，会生成多个检测结果。

---

## Step 4: 改造 WebUI — 拖拽配对 + 实时对话

### 改动文件

- `ui/index.html` — 全新布局：Agent 列表 + 拖放区 + 对话面板
- `ui/server.py` — `GET /api/config` 不自动写入，仅返回检测结果；新增配对确认 API

### 首次启动布局

```
┌──────────────────────────────────────────────────┐
│  ◉ Agent Bridge                                   │
├──────────────────────────────────────────────────┤
│  本机 Agent                          [刷新 🔄]   │
│                                                    │
│  ┌──────┐  ┌──────┐  ┌──────────┐                │
│  │  ●    │  │  ●    │  │  ●       │               │
│  │ 墨墨  │  │ 苏苏  │  │ Claude  │               │
│  │Hermes │  │OpenClaw│  │ Code    │               │
│  └──────┘  └──────┘  └──────────┘                │
│                                                    │
├──────────────────────────────────────────────────┤
│               🎯 聊天室                            │
│                                                    │
│  ┌──────────────────────────────────────┐         │
│  │                                      │         │
│  │      将 Agent 拖到这里开始对话       │         │
│  │                                      │         │
│  │     ┌──────────┐    ┌──────────┐    │         │
│  │     │ 【空位】  │ ←→ │ 【空位】  │    │         │
│  │     └──────────┘    └──────────┘    │         │
│  │                                      │         │
│  └──────────────────────────────────────┘         │
│                                                    │
│  [✓ 确认配对] ← 拖入两个 Agent 后亮起            │
└──────────────────────────────────────────────────┘
```

### 交互流程

```
WebUI 加载
  ├─ GET /api/config
  │   └─ agents 不为空 → 显示历史聊天界面（已有配置）
  │   └─ agents 为空 → 进入配对模式
  │       ├─ GET /api/discover（并行调）
  │       └─ 显示 Agent 卡片列表

配对模式：
  1. 用户看到本机所有 Agent 卡片
  2. 拖拽两个 Agent 到聊天室的两个空位
     - 拖入时自动交换位置：不管拖到左边还是右边，按卡片顺序分配
     - 也可以点击卡片快速分配（备选操作）
  3. 两个空位都填满后，[确认配对] 按钮亮起
  4. 用户点击 [确认配对]
     ├─ PUT /api/config/full 写入 bridge.yaml
     ├─ POST /api/poll/start 启动轮询
     └─ 切换到对话面板
```

### 进入对话后的布局

```
┌──────────────────────────────────────────────────┐
│  ◉ Agent Bridge                                   │
├──────────────────────────────────────────────────┤
│  💬 墨墨 ↔ 苏苏                      [停止] [设置]│
│                                                    │
│  ┌──────────────────────────────────────┐         │
│  │                                      │         │
│  │  [10:30] ● 墨墨: 你好苏苏           │         │
│  │  [10:31] ● 苏苏: 有的，查到了       │         │
│  │  [10:32] ● 墨墨: 发给我看看吧       │         │
│  │  [10:33] ● 苏苏: 好的，这是资料... │         │
│  │                                      │         │
│  └──────────────────────────────────────┘         │
│                                                    │
│  ● 轮询运行中 (每 180s)    消息: 4 条             │
│                                                    │
│  [输入框...]                               [发送]  │
└──────────────────────────────────────────────────┘
```

### API 变更

**`GET /api/discover`** — 检测本机 Agent（只检测不写入）

```json
// 响应
{
  "ok": true,
  "detected": [
    {
      "id": "momo",
      "display_name": "墨墨",
      "source": "hermes",
      "color": "#ff6b6b",
      "wakeup": { "url": "http://127.0.0.1:8644/webhooks/agent-reply", ... }
    },
    {
      "id": "susu",
      "display_name": "苏苏",
      "source": "openclaw",
      "color": "#4ecdc4",
      "wakeup": { "url": "http://127.0.0.1:18789/tools/invoke", ... }
    }
  ]
}
```

**`POST /api/pair`** — 配对确认（新增）

```json
// 请求
{
  "agent_a": "momo",
  "agent_b": "susu"
}

// 响应
{
  "ok": true,
  "message": "配对完成：墨墨 ↔ 苏苏",
  "saved_agents": ["momo", "susu"],
  "poll_started": true
}
```

此端点内部执行：
1. 从 `_detect_agents()` 获取两个 agent 的完整配置
2. 写入 `bridge.yaml`
3. 设置各自的 `filter_from`（互指对方）
4. 启动 PollManager 轮询

### 前端要点

- **拖拽**：用原生 HTML5 Drag & Drop API（无外部依赖），或者点击快速分配
- **实时更新**：对话面板通过 `POST /api/poll/now` 手动触发轮询 加 `/api/messages` 拉取消息
  - 或者用 `setInterval` 每 3 秒轮询一次 `/api/messages` 更新显示（仅聊天过程中）
- **Agent 卡片**：显示颜色圆点、名称、框架名、图标
- **空位**：虚线边框，hover 高亮，拖入时显示接受状态

### 对话中的配置面板

进入对话后，点击顶部栏的设置按钮或 Agent 名称，展开配置面板：

```
┌──────────────────────────────────┐
│ ■ 墨墨 (momo)                    │
│                                  │
│  Agent ID: [momo           ]     │
│  显示名称: [墨墨           ]    │
│  颜色: [■ #ff6b6b] [调色板]     │
│  游标类型: [line ▼]             │
│  只接收来自: [susu ▼]           │
│                                  │
│  ── 唤醒配置 ──                  │
│  URL: [http://127.0.0.1:8644...]│
│  Method: [POST ▼]               │
│  Headers:                        │
│  ┌ Content-Type: application/json│
│  └ [+] 添加请求头               │
│                                  │
│  认证: [Bearer Token ▼]          │
│  Token 文件: [~/.hermes/...]    │
│  JSONPath: [auth.token    ]     │
│                                  │
│  请求体模板:                     │
│  ┌──────────────────────────┐   │
│  │ {"message": "{{message}}"}│   │
│  └──────────────────────────┘   │
│                                  │
│  [◀ 返回] [删除] [保存]        │
└──────────────────────────────────┘

---

## Step 5: 前后端 API 对接

### 现有 API 清单（可以直接用）

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/config` | GET | 获取配置 |
| `/api/config/full` | PUT | 保存完整配置（含 agents、wakeup） |
| `/api/poll/start` | POST | 启动轮询 |
| `/api/poll/stop` | POST | 停止轮询 |
| `/api/poll/now` | POST | 立即轮询一次 |
| `/api/messages` | GET | 获取消息 |
| `/api/archive` | POST | 归档 |
| `/api/send` | POST | 发送消息 |
| `/api/status` | GET | 服务状态 |

### 需要补充的

1. **引导页 onbarding 状态指示** — `/api/config` 返回 `agents` 为空时前端显示引导页
2. **WebUI 中支持配置 wakeup URL + auth** — 当前 `handle_update_config` 只处理 ID/名称/颜色，完整配置需走 `handle_update_config_full`
3. **保存后自动 start poll** — 前端调用 `/api/config/full` 后自动调 `/api/poll/start`

---

## Step 6: 更新 `README.md`

去掉 `bridge setup` 的提及，更新快速开始部分：

```markdown
## 快速开始

### 安装

curl -fsSL https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.sh | bash

### 启动

bridge start --open

浏览器自动打开 http://127.0.0.1:7899
在页面中配置两个 Agent 的信息即可开始聊天。
```

---

## 文件变动清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `cli/bridge` | 修改 | 删除 setup 命令、_ask 系列函数、_install_service 系列；cmd_start 增加首次初始化逻辑 |
| `ui/server.py` | 无/微调 | API 已够用，仅需确保 `config/full` 完全支持 wakeup 配置 |
| `ui/index.html` | 修改 | 新增引导页面视图；Agent 配置面板扩展支持完整唤醒参数 |
| `docs/SETUP.md` | 修改 | 更新为无向导流程 |
| `docs/GETTING_STARTED.md` | 修改 | 更新 |
| `README.md` | 修改 | 更新快速开始 |
| `install.sh` | 无 | 安装逻辑不变 |

---

## 验证

1. 全新安装后 `bridge start --open` → 自动打开浏览器 → 显示引导页
2. 在引导页配置两个 agent → 保存后自动开始轮询 → 聊天界面出现
3. 关闭浏览器后 `bridge stop` → 重新 `bridge start` → 配置保留
4. 点击 Agent Badge 修改配置 → 写回 bridge.yaml 正确
5. `bridge status` 显示正确信息
6. 测试通过（`pytest tests/ -v`）

---

## 风险和注意事项

1. **向后兼容**：已有 `bridge.yaml` 的用户升级后 `bridge start` 不应覆盖已有配置。`cmd_start` 中的初始化逻辑应检查 `bridge.yaml` 是否存在，存在则跳过。
2. **轮询服务迁移**：当前有些用户可能已安装 launchd/systemd 定时轮询。新版本用 PollManager 内置轮询，需要确保：
   - 旧定时器不冲突（轮询脚本和 PollManager 同时运行会重复处理）
   - `bridge setup` 消失后，旧用户升级时旧服务可能还在
3. **方案**：`bridge start` 时检测是否有旧的 launchd/systemd 轮询定时器，如果有，自动卸载。
