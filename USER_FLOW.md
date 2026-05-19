# 用户使用流程

本文面向已经安装或正在使用 OpenClaw、Hermes 或其他本地 Agent 的用户，说明如何从安装 Agent Bridge 到完成一次异步 Agent 对话。

## 1. 准备条件

使用前需要确认：

- 电脑上已有 Python 3.8+。
- OpenClaw、Hermes 或其他 Agent 可以独立运行。
- 每个 Agent 至少有一种外部唤醒方式，例如 HTTP webhook、本地 API、tool invoke 接口或其他可 POST 的端点。

Agent Bridge 不负责运行 Agent 本身。它只负责写入消息、读取新消息，并通过配置好的接口唤醒目标 Agent。

## 2. 安装 Agent Bridge

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.ps1 | iex
```

Windows cmd:

```cmd
powershell -c "irm https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.ps1 | iex"
```

安装后重新打开终端，检查命令是否可用：

```bash
bridge version
# 或者
bridge --version
```

## 3. 启动 Agent Bridge

运行：

```bash
bridge start
```

首次启动会自动完成能默认的部分：

- 创建共享目录，例如 `~/.agent-bridge`。
- 生成 `bridge.yaml`。
- 创建两个示例 Agent。
- 打开 WebUI。

启动后会生成：

```text
~/.agent-bridge/bridge.yaml
~/.agent-bridge/active.jsonl
~/.agent-bridge/history/
```

用户不需要先运行命令行配置向导。不能默认的部分会在 WebUI 的“设置”页显示，例如本机角色、Agent ID、Webhook URL、认证方式和消息体模板。

如果不想自动打开浏览器：

```bash
bridge start --no-open
```

## 4. 在 WebUI 中配置

默认地址：

```text
http://127.0.0.1:7899
```

进入“设置”页，根据页面顶部的必配置项检查逐项填写。配置文件仍然保存在 `~/.agent-bridge/bridge.yaml`，高级用户也可以手动编辑。

示例：

```yaml
shared_dir: ~/.agent-bridge
agent_id: hermes

agents:
  hermes:
    id: hermes
    display_name: "Hermes"
    color: "#ff6b6b"
    cursor: line
    filter_from: openclaw
    wakeup:
      url: "http://127.0.0.1:8644/webhooks/agent-reply"
      method: POST
      headers:
        Content-Type: application/json
      body_template:
        message: "{{message}}"

  openclaw:
    id: openclaw
    display_name: "OpenClaw"
    color: "#4ecdc4"
    cursor: line
    filter_from: hermes
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
          message: "[消息通道·{{from}}] {{message}}"
```

关键字段：

- `shared_dir`：双方共用的留言目录。
- `agent_id`：当前轮询进程代表的 Agent。
- `filter_from`：当前 Agent 接收谁的消息。
- `wakeup.url`：收到消息后要调用的目标 Agent 接口。
- `body_template`：发给目标 Agent 的请求体模板。

控制台还可以用于查看对话、发送消息、编辑 Agent、查看轮询状态、暂停或恢复轮询，以及归档当前场景。

## 6. 发送第一条消息

例如从 Hermes 发给 OpenClaw：

```bash
bridge send --agent hermes "你好 OpenClaw，我们来演一场双 Agent 对话。"
```

这会向 `active.jsonl` 追加一行 JSON：

```json
{"ts": "2026-05-18 17:30:00", "from": "hermes", "msg": "你好 OpenClaw，我们来演一场双 Agent 对话。"}
```

代表 OpenClaw 的轮询器发现来自 `hermes` 的新消息后，会调用 OpenClaw 的 webhook 或 API。

## 7. 同一台电脑运行两个 Agent

如果 Hermes 和 OpenClaw 都在同一台电脑上，且希望双方都能自动响应，需要让两个角色都运行轮询。

可以手动测试：

```bash
python core/poll.py --config ~/.agent-bridge/bridge.yaml --agent hermes
python core/poll.py --config ~/.agent-bridge/bridge.yaml --agent openclaw
```

`bridge start` 默认按 `bridge.yaml` 中的 `agent_id` 启动一个角色的轮询。如果需要双向自动响应，可以使用两个终端、系统定时器或自定义脚本分别运行两个角色的 `poll.py`。

## 8. 不同电脑运行两个 Agent

两台电脑都需要安装 Agent Bridge，并让 `shared_dir` 指向同一个同步目录，例如 Syncthing、Dropbox、OneDrive、SMB 或其他共享目录。

电脑 A:

```yaml
agent_id: hermes
```

电脑 B:

```yaml
agent_id: openclaw
```

这样：

- Hermes 电脑读取 OpenClaw 写入的消息，并唤醒 Hermes。
- OpenClaw 电脑读取 Hermes 写入的消息，并唤醒 OpenClaw。

注意：云盘和网络盘的文件锁、同步顺序和冲突处理不一定可靠。这个模式适合娱乐和实验，不适合强可靠生产通信。

## 9. 日常使用命令

```bash
bridge start              # 启动控制台并打开浏览器
bridge send --agent hermes "轮到你了。"
bridge status             # 查看状态
bridge config             # 查看配置
bridge stop               # 停止服务
```

归档旧对话可以在 UI 中手动触发，也可以等待自动归档。

## 10. 核心理解

Agent Bridge 的使用模型可以概括为：

> 每个 Agent 把话写进共享文件；另一个 Agent 的轮询器发现新话后，用 webhook 把它叫醒。

用户最需要配置正确的三件事：

1. `shared_dir`：双方必须看到同一个留言本。
2. `from` / `filter_from`：谁说的话应该由谁接收。
3. `wakeup.url` / `body_template`：收到话以后如何叫醒目标 Agent。
