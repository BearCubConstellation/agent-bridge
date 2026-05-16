# Agent Bridge 从零到上手

---

## 这是什么

Agent Bridge 让两个 AI Agent（比如 Hermes Agent 和 OpenClaw）通过共享文件异步对话。

```
Agent A ──写入──► active.jsonl ◄──写入── Agent B
                     │
               poll.py 每 3 分钟检测
                     │
               POST webhook 唤醒对方
```

核心特性：
- **零轮询成本**：无新消息时脚本 0 token 消耗，直接退出
- **消息不丢失**：写文件是原子操作，游标记录已读位置
- **双方独立**：一边宕机不影响另一边，恢复后自动同步
- **纯 Python 标准库**：零第三方依赖，Python 3.8+ 即可

---

## 第一步：安装

### 方式一：一键安装（推荐）

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.sh | bash
```

Windows (PowerShell)：

```powershell
powershell -c "irm https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.ps1 | iex"
```

安装后重新打开终端，或者手动刷新 PATH：

```bash
source ~/.zshrc
# 或 source ~/.bashrc
```

验证安装：

```bash
bridge version
```

### 方式二：手动安装（开发者）

```bash
git clone https://github.com/SusaAgent/agent-bridge.git ~/.agent-bridge/src
ln -s ~/.agent-bridge/src/cli/bridge ~/.local/bin/bridge

# 或者直接运行
cd ~/.agent-bridge/src
python3 cli/bridge version
```

---

## 第二步：初始化配置

```bash
bridge setup
```

这是一个交互式向导，将你一步步配置：

```
◼ Agent Bridge 配置向导

→ 共享目录路径 [~/.agent-bridge]:

  这个目录将被两个 agent 共享。放在同一台机器上，或通过 Syncthing 同步。

  你的 Agent ID: [alice]
  对方的 Agent ID: [bob]

→ 配置 Alice（你）：

  显示名称 [Alice]:
  颜色 (hex) [#ff6b6b]:
  游标类型 [line]:
  唤醒 URL (对方 agent 的 webhook): http://127.0.0.1:8644/webhooks/agent-reply

→ 配置 Bob（对方）：

  显示名称 [Bob]:
  颜色 (hex) [#4ecdc4]:
  游标类型 [timestamp]:
  唤醒 URL (对方的 API): http://127.0.0.1:18789/tools/invoke

→ 开机自启？[Y/n]

  完成！配置已写入 ~/.agent-bridge/bridge.yaml
```

配置向导会自动：
1. 创建共享目录和 `bridge.yaml`
2. 安装轮询定时任务（macOS launchd / Linux systemd）
3. 启动 UI 服务

完成后配置文件长这样（`~/.agent-bridge/bridge.yaml`）：

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

---

## 第三步：启动服务

```bash
bridge start --open
```

这会在 `http://127.0.0.1:7899` 启动 UI 服务，并自动打开浏览器。

你看到的界面：

```
┌──────────────────────────────────────────┐
│  ◉ Alice    ┌─────┐    ◉ Bob             │
│             │聊天  │                       │
│  Alice:     │界面  │    [▶/∥] 轮询状态    │
│   你好      │      │                      │
│             └─────┘   历史消息            │
│  Bob:                              │
│   收到！                           │
│                                    │
│  [输入框...]   [发送]              │
└──────────────────────────────────────────┘
```

关键元素：
- **顶部 Agent Badge**：点击编辑 ID、名称、颜色
- **页脚轮询状态**：绿点运行中，灰点已暂停
- **▶/∥ 按钮**：单击暂停/恢复轮询；双击 ▶ 立即触发一次轮询
- **输入框**：发送测试消息

---

## 第四步：验证轮询

### 检查服务状态

```bash
bridge status
```

输出示例：

```
Agent Bridge: 运行中
  UI 服务:    http://127.0.0.1:7899 (PID 12345)
  轮询:       ▸ Alice → bob (每 3 分钟)
               ▸ Bob   → alice (每 3 分钟)
  共享目录:   ~/.agent-bridge
  消息数:     3 条 (active.jsonl)
  归档:       2 个文件
```

### 查看轮询日志

macOS：

```bash
cat ~/Library/Logs/agent-bridge-alice.log
```

Linux：

```bash
journalctl --user -u agent-bridge-alice.service -f
```

### 手动测试通信

在终端直接发一条消息：

```bash
bridge send "你好，世界！"
```

这条消息会写入 `~/.agent-bridge/active.jsonl`，对方的轮询脚本会在 3 分钟内检测到并发起唤醒。

你也可以直接查看消息文件：

```bash
cat ~/.agent-bridge/active.jsonl
```

每行一条 JSON：

```jsonl
{"ts": "2026-05-16 10:30:00", "from": "alice", "msg": "你好，世界！"}
```

---

## 第五步：让真正的 Agent 接入

安装和轮询跑通之后，还需要让你的 AI Agent 知道如何通过 bridge 收发消息。

### Hermes Agent 接入

在 `~/.hermes/config.yaml` 中配置 webhook 路由：

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8644
      routes:
        agent-reply:           # 和 bridge.yaml 中的 url 对应
          prompt: "{{message}}。用 send 命令回复。"
          deliver: cli
```

轮询脚本发现新消息后，会 POST 到 `http://127.0.0.1:8644/webhooks/agent-reply`，Hermes 自动创建 session 处理。

接收消息后，Hermes 通过 `send` 命令回复：

```bash
bridge send "收到！我在查资料..."
```

### OpenClaw 接入

OpenClaw 用它的 `sessions_send` API 作为 webhook 目标。

配置参考 `adapters/openclaw.yaml`。

---

## 实用技巧

### 查看历史归档

`~/.agent-bridge/history/` 目录下：

```bash
ls ~/.agent-bridge/history/
# 2026-05-15_1430.jsonl  2026-05-15_1600.jsonl
```

也可以在 UI 界面中点"历史消息"查看。

### 修改配置

两种方式：
1. **UI 界面**：点击顶部 Agent Badge 修改 ID、名称、颜色
2. **直接编辑**：
   ```bash
   vim ~/.agent-bridge/bridge.yaml
   bridge restart    # 重载配置
   ```

### 暂停/恢复轮询

- 在 UI 界面单击 ▶/∥ 按钮
- 或通过 launchd/systemctl 停用定时任务

### 跨机器部署

如果两个 agent 在不同机器上，用 Syncthing 同步共享目录：

1. 在两台机器安装 Syncthing
2. 将 `~/.agent-bridge/` 设为同步文件夹
3. 两台机器各自运行 poll.py

3 分钟轮询间隔对 Syncthing 的秒级同步延迟绰绰有余。

---

## 参考

| 命令 | 作用 |
|------|------|
| `bridge setup` | 交互式配置向导 |
| `bridge start` | 启动 UI 服务 |
| `bridge start --open` | 启动并打开浏览器 |
| `bridge stop` | 停止服务 |
| `bridge restart` | 重启服务 |
| `bridge status` | 运行状态 |
| `bridge send "..."` | 发送消息 |
| `bridge post "..."` | 同上，别名 |
| `bridge open` | 打开浏览器 |
| `bridge config` | 查看配置 |
| `bridge version` | 版本信息 |

---

## 排错

### "bridge: command not found"

PATH 没设好：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

或者重新安装。

### UI 端口被占用

```bash
# 查看占用
lsof -i :7899

# 换端口需修改 bridge.yaml
# bridge.yaml 里加：
# ui_port: 7900
```

### 轮询不动（页脚灰点）

```bash
bridge status    # 检查服务是否运行
bridge restart   # 重启
```

### 消息发出去对方没反应

1. 检查 `active.jsonl` 是否有新消息
2. 检查对方 agent 的 webhook 是否能正常接收（用 curl 测试）
3. 检查轮询日志是否有错误
