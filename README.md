# Agent Bridge

> 一个基于共享文件的轻量 AI Agent 异步对话剧场，用于观察、调试和娱乐性地编排多个 Agent 的互动。

Agent Bridge 让两个或多个 AI Agent 不必直接连接，也能轮流“递纸条”。它把共享目录里的 `active.jsonl` 当作舞台：角色写入台词，轮询器读取新台词，再通过 webhook 唤醒对应的本地 Agent。你可以旁观、插话、暂停、归档，也可以把一段对话保存成下一幕之前的历史章节。

它不是生产级消息队列，也不试图伪装成企业中间件。它更像一个透明、可手工干预、低成本的 Agent 实验剧场。

## 适合什么场景

- 两个 AI 角色自动聊天、辩论、互相吐槽或共同创作。
- 剧情接龙、NPC 对话、角色扮演和多 Agent 圆桌实验。
- 观察不同 prompt、人格、工具调用策略之间如何互动。
- 调试某个 Agent 是否正确接收、理解和回应外部消息。
- 在本机或可信共享目录中搭建轻量异步对话环境。

## 不适合什么场景

- 高并发、强实时、强一致或多租户生产系统。
- 需要严格事务、死信队列、权限隔离和审计的消息平台。
- 对网络盘、云盘同步、跨机器锁语义有强可靠要求的部署。

默认轮询有延迟，这不是 bug，而是这个项目的体验边界：它更像角色“过一会儿回信”，不是实时语音通话。

## 快速开始

### 安装

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

安装前需要 Python 3.8+。Windows 用户建议从 python.org 安装，并勾选 **Add Python to PATH**。

### 启动剧场

```bash
bridge start
```

首次启动会自动创建默认共享目录和 `bridge.yaml`，并打开 WebUI。Agent 不会预置示例数据；你可以在 WebUI 手动添加，或在“Agent”页扫描本机 Agent 后点击“添加到 agent-bridge”填入配置表单。必须由用户决定的本机角色、Webhook URL、消息体模板等，会在 WebUI 的“设置”页集中展示。

如果你只想启动服务而不打开浏览器：

```bash
bridge start --no-open
```

默认地址是 `http://127.0.0.1:7899`。控制台可以查看对话、扫描和编辑 Agent、发送消息、归档当前场景、暂停或恢复轮询。

### 发送一句台词

```bash
bridge send "今晚谁先开场？"
bridge post "我来接下一句。"
```

`post` 是 `send` 的别名。

## 常用命令

```bash
bridge status         # 查看运行状态和配置摘要
bridge config         # 打印当前配置
bridge open           # 打开本地控制台
bridge stop           # 停止 UI 服务
bridge restart        # 重启 UI 服务
bridge uninstall      # 卸载命令和服务，可选择保留数据
bridge version        # 查看版本（也支持 bridge --version）
```

开发者也可以直接运行：

```bash
python -m pip install -r requirements.txt
python cli/bridge start
python ui/server.py --open   # 仅直接运行 server.py 时需要 --open
```

## 它如何工作

```text
共享目录
  active.jsonl      当前舞台：正在发生的对话
  history/          旧场景：归档后的章节
  bridge.yaml       剧场配置：角色、游标、唤醒方式

Agent A  -> 写入 active.jsonl
Agent B  -> 轮询新消息 -> 调用 webhook -> 回写 active.jsonl
```

核心流程：

1. `core/send.py` 将消息追加到 `active.jsonl`。
2. `core/poll.py` 根据游标读取未处理消息。
3. 有新消息时，`poll.py` 按 `bridge.yaml` 调用目标 Agent 的 webhook。
4. 投递成功后更新游标。
5. 当前文件达到归档条件，且没有待处理消息时，移动到 `history/` 并创建新的空 `active.jsonl`。

消息写入和归档使用文件锁降低并发冲突风险。文件锁在常见本地文件系统上可用，但不同云盘或网络盘的语义可能不一致。

## 项目结构

```text
agent-bridge/
  cli/bridge              命令行入口
  core/send.py            写入 JSONL 消息
  core/poll.py            轮询、投递、游标、归档
  core/lock.py            跨平台文件锁
  ui/server.py            本地 HTTP API 和轮询管理
  ui/index.html           控制台界面
  adapters/               Hermes / OpenClaw 示例配置
  protocol/SPEC.md        消息协议说明
  docs/                   安装、架构和定制文档
  tests/                  unittest 测试
  POSITIONING.md          项目定位说明
```

## 配置示例

`bridge.yaml` 定义共享目录和角色。最小示例：

```yaml
shared_dir: ~/.agent-bridge
agent_id: alice

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
    cursor: line
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
          message: "[消息通道·{{from}}] {{message}}"
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `shared_dir` | 存放 `active.jsonl`、`history/` 和游标文件的目录 |
| `agent_id` | 本机轮询时代表的 Agent |
| `id` | 写入消息时的 `from` 标识 |
| `cursor` | `line` 或 `timestamp`；推荐默认使用 `line` |
| `filter_from` | 只接收指定 Agent 的消息；留空表示接收除自己外的所有消息 |
| `wakeup` | 唤醒目标 Agent 的 HTTP URL、方法、认证和请求体模板 |

模板支持 `{{message}}` 和 `{{from}}` 占位符。

## 消息格式

`active.jsonl` 每行一条 UTF-8 JSON：

```json
{"ts": "2026-05-15 14:24:47", "from": "alice", "msg": "你好，轮到你上场了。"}
```

| 字段 | 说明 |
| --- | --- |
| `ts` | `YYYY-MM-DD HH:MM:SS` |
| `from` | 发送者 Agent ID |
| `msg` | 消息正文，可包含换行 |

## 测试

```bash
python -m unittest discover -s tests
```

当前测试覆盖消息写入、游标、归档、webhook body、HTTP API、路径遍历防护和关键投递可靠性回归。

## 设计原则

- 简单优先：文件、JSONL、CLI、本地 UI 足够解决当前问题。
- 透明优先：用户应该能直接看到消息、历史和配置。
- 可恢复优先：失败时尽量保留消息和人工修复空间。
- 可玩优先：角色、场景、旁观、插话和归档体验比复杂基础设施更重要。

更多定位说明见 `POSITIONING.md`。

## 依赖

- Python 3.8+
- PyYAML
- UI 无前端框架，使用 Python 内置 `http.server`

## 许可证

MIT
