# Agent Bridge 架构改造方案

> 文档用途：本文档用于指导开发 Agent 对 `Agent Bridge` 项目进行系统性架构改造。  
> 目标：将当前“文件轮询 + 单向唤醒”的轻量系统，升级为“事件驱动 + 双向通信 + 可扩展 Adapter + MCP/Skill 接入”的轻量级 AI Agent 多人聊天室系统。  
> 重要原则：保留当前项目的轻量、透明、文件可观察、可手工修复优势，不引入过重的中间件依赖。

---

## 0. 背景与现状

Agent Bridge 是一个轻量级 AI Agent 多人聊天室系统。多个 AI Agent 在同一个 room 内按一定策略轮流发言，类似群聊或圆桌讨论。

当前项目核心存储模型为文件系统：

```text
~/.agent-bridge/
  bridge.yaml
  active.jsonl                         # legacy 对话文件
  history/
  rooms/
    {room_id}/
      active.jsonl                     # room 当前消息
      state.json                       # room 状态
      runtime.log                      # 运行日志
      cursors/
      history/
```

当前核心流程大致为：

```text
1. tick_room 检测当前 room 是否 running
2. 根据 room.order / turn_index 判断轮到哪个 Agent
3. 读取该 Agent 的 pending messages
4. 通过 adapter / wakeup 将消息投递给 Agent
5. 如果 adapter 同步返回回复，则自动写入 active.jsonl
6. 如果没有同步回复，则进入 waiting_for 状态
7. 后续轮询继续检查 waiting_for 是否已在 active.jsonl 中出现新消息
8. 如果出现回复，则推进 turn_index
```

当前已经具备的基础能力：

- 基于 room 的消息文件：`rooms/{room_id}/active.jsonl`
- room 状态文件：`state.json`
- 运行日志：`runtime.log`
- 基础 adapter 层：`native_http`、`cli`、`stdio_shim`、`file_inbox`、`manual`
- UI Server：本地 HTTP API + 控制台
- PollManager：后台定时轮询
- OpenClaw / Hermes 等本地 Agent 的初步发现和 wakeup 配置

但当前系统仍然存在架构瓶颈。

---

## 1. 当前核心问题

### 1.1 Adapter 抽象不完整

当前 adapter 更像是“投递适配器”，只解决：

```text
Bridge 如何把消息送给 Agent？
```

但没有系统性解决：

```text
Agent 如何回复？
回复是同步返回、异步回调、文件写回，还是需要 Bridge 主动拉取？
这次投递和后续回复如何关联？
Agent 是否支持主动发言？
Agent 是否支持流式输出？
Agent 是否支持 MCP 工具调用？
```

因此当前 `deliver_to_adapter()` 返回 `success/detail/response_body` 这种三元结构并不够表达 Agent 生命周期。

### 1.2 Agent 缺少统一回写路径

目前 `tick_room()` 在投递成功但未获得同步回复时，会设置：

```json
{
  "waiting_for": "openclaw",
  "waiting_line": 12
}
```

然后后续轮询通过扫描 `active.jsonl` 判断等待的 Agent 有没有在 `waiting_line` 后写过消息。

问题是：

> 系统没有保证 Agent 一定有能力将回复写回 `active.jsonl`。

例如 OpenClaw `sessions_send` 返回 200，只代表消息成功送入 OpenClaw session，不代表 OpenClaw 的最终回复已经返回给 Agent Bridge。

如果 OpenClaw 没有调用 Bridge API，也没有写 outbox 文件，Bridge 就会一直处于 `waiting_response`。

### 1.3 轮询间隔不适合交互式聊天室

当前默认轮询间隔为 180 秒。这适合“异步剧场”，但不适合交互式多人聊天室。

聊天室场景需要：

- 用户发消息后立即触发调度
- Agent 回写后立即推进下一轮
- room start 后立即启动第一轮
- timeout 后立即进入跳过、重试或失败处理

因此纯定时轮询需要降级为兜底机制，主流程应改为事件驱动。

### 1.4 消息、事件、状态职责混合

当前 `rooms.py` 同时承担：

- room 初始化
- 消息追加
- 消息读取
- cursor 管理
- round-robin 调度
- waiting 状态判断
- adapter 调用
- 归档
- runtime log

短期可以工作，但长期会导致：

- 新 Agent 类型接入困难
- waiting/timeout/retry 逻辑难扩展
- 主动发言、广播、主持人模式难落地
- MCP / Skill 接入时职责不清晰

---

## 2. 改造目标

本次架构改造目标不是把项目做成重型企业消息队列，而是保持轻量前提下补齐关键抽象。

### 2.1 总体目标

将 Agent Bridge 升级为：

```text
一个以 JSONL 为持久日志、以事件为驱动、以 Adapter Capability 为边界、支持 HTTP / CLI / File / MCP / Skill 接入的轻量级 Agent 消息总线。
```

### 2.2 必须解决的问题

1. 统一 Adapter 抽象  
   支持不同 Agent 的唤醒方式、回复方式、主动发言能力声明。

2. 双向通信  
   Agent 被唤醒后，必须有标准回写通道。

3. 事件驱动  
   用户发消息、Agent 回写、room start、timeout、file outbox 变化都应立即触发调度。

4. 状态机清晰  
   room runtime 应使用明确的 turn state，而不是仅依赖 `waiting_for` 字符串。

5. MCP 可选接入  
   Agent Bridge 可作为 MCP Server 暴露聊天室工具，但核心不依赖 MCP。

6. Skill 可选接入  
   提供 Agent 接入 Skill，让开发/执行 Agent 明确知道如何参与聊天室、如何回写消息。

7. 向后兼容  
   旧的 `wakeup` 配置应尽量自动转换为新的 adapter 配置。

---

## 3. 新架构总览

推荐改造后的架构：

```text
┌──────────────────────────────────────────────┐
│                  Web UI / CLI                 │
│   - 控制台                                    │
│   - bridge send                               │
│   - 手动 tick                                 │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│                Bridge API Server              │
│   - /api/rooms/{room}/send                    │
│   - /api/rooms/{room}/agents/{agent}/callback │
│   - /api/events                               │
│   - /api/status                               │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│                  Event Bus                    │
│   - append events.jsonl                       │
│   - enqueue room_id                           │
│   - dispatch room runtime                     │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│                Room Runtime                   │
│   - turn state machine                        │
│   - pending message selection                 │
│   - timeout / retry / skip                    │
│   - schedule next turn                        │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│              Agent Adapter Layer              │
│   - native_http                               │
│   - openclaw_sessions                         │
│   - cli                                       │
│   - file_mailbox                              │
│   - mcp_tool                                  │
│   - manual                                    │
└──────────────────────────────────────────────┘
```

旁路能力：

```text
┌──────────────────────────────────────────────┐
│                MCP Server                     │
│   - agent_bridge.list_rooms                   │
│   - agent_bridge.read_messages                │
│   - agent_bridge.reply_turn                   │
│   - agent_bridge.send_message                 │
│   - agent_bridge.get_current_turn             │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│                Agent Skill                    │
│   - SKILL.md                                  │
│   - protocol references                       │
│   - callback examples                         │
│   - MCP tool usage examples                   │
└──────────────────────────────────────────────┘
```

---

## 4. 推荐目录结构

建议逐步将核心能力拆分为以下结构。

```text
agent-bridge/
  core/
    storage.py               # 通用文件读写、JSONL append/read、锁封装
    rooms.py                 # room 存储模型、消息读写、状态读写
    events.py                # 事件模型、events.jsonl、内存队列
    runtime.py               # room runtime 状态机
    scheduler.py             # 调度队列、worker、立即触发机制
    protocol.py              # Message/Event/Turn/Delivery 数据结构与校验
    security.py              # token、agent 权限、callback 校验
    adapters/
      __init__.py
      base.py                # Adapter 基类 / 协议
      native_http.py
      openclaw_sessions.py
      cli.py
      file_mailbox.py
      mcp_tool.py
      manual.py
    mcp_server.py            # Agent Bridge MCP Server，可选启用
    skills/
      agent-bridge-room-participant/
        SKILL.md
        references/
          callback.md
          mcp-tools.md
          message-schema.json
          event-schema.json
  ui/
    server.py                # API + UI，尽量不放核心调度逻辑
    index.html
  cli/
    bridge
  protocol/
    SPEC.md
  docs/
    ARCHITECTURE_V2.md
    ADAPTERS.md
    MCP.md
    SKILL.md
```

### 4.1 拆分原则

- `rooms.py` 不应直接调用 adapter。
- `server.py` 不应直接承载调度状态机。
- `runtime.py` 只负责“根据当前状态决定下一步动作”。
- `events.py` 负责把外部输入统一转换为事件。
- `scheduler.py` 负责事件到 room runtime 的触发。
- `adapters/*` 只负责和具体 Agent 通信。

---

## 5. 数据模型设计

### 5.1 Message 模型

room 消息仍写入：

```text
~/.agent-bridge/rooms/{room_id}/active.jsonl
```

每行一条 JSON。

推荐新消息结构：

```json
{
  "id": "msg_20260522103000123456",
  "ts": "2026-05-22 10:30:00",
  "room": "room_main",
  "from": "openclaw",
  "to": "hermes",
  "kind": "agent",
  "msg": "这是回复内容",
  "reply_to": "turn_20260522102930123456",
  "correlation_id": "corr_20260522102930123456",
  "meta": {
    "source": "callback",
    "adapter": "openclaw_sessions"
  }
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `id` | 是 | 消息唯一 ID |
| `ts` | 是 | 写入时间，格式沿用当前项目 `YYYY-MM-DD HH:MM:SS` |
| `room` | 是 | room ID |
| `from` | 是 | 发送方 Agent ID，用户可用 `user` |
| `to` | 否 | 目标 Agent。为空表示广播给 room 内所有 Agent |
| `kind` | 是 | `user` / `agent` / `system` / `event` |
| `msg` | 是 | 消息正文 |
| `reply_to` | 否 | 如果是某个 turn 的回复，填写 turn_id |
| `correlation_id` | 否 | 用于关联投递与回复 |
| `meta` | 否 | 扩展信息 |

兼容要求：

- 旧消息只有 `ts/from/msg` 时仍要可读。
- 读取时应补充 `_line`，但不要写入原文件。
- 旧 UI 应尽量仍能展示新消息。

---

### 5.2 Event 模型

新增文件：

```text
~/.agent-bridge/rooms/{room_id}/events.jsonl
```

每一行是系统事件，不等于聊天消息。

推荐事件结构：

```json
{
  "id": "evt_20260522103000123456",
  "ts": "2026-05-22 10:30:00",
  "room": "room_main",
  "type": "agent.response.received",
  "actor": "openclaw",
  "turn_id": "turn_20260522102930123456",
  "correlation_id": "corr_20260522102930123456",
  "message_id": "msg_20260522103000123456",
  "meta": {
    "source": "callback"
  }
}
```

推荐事件类型：

| 事件类型 | 触发时机 |
|---|---|
| `room.started` | room 被启动 |
| `room.paused` | room 被暂停 |
| `message.created` | 用户或 Agent 写入消息 |
| `turn.selected` | runtime 选中下一个 Agent |
| `agent.wakeup.requested` | 准备唤醒 Agent |
| `agent.wakeup.succeeded` | Agent 唤醒成功 |
| `agent.wakeup.failed` | Agent 唤醒失败 |
| `agent.response.received` | Agent 回复已收到 |
| `turn.completed` | 当前 turn 正常完成 |
| `turn.timeout` | 当前 turn 超时 |
| `turn.skipped` | 当前 turn 被跳过 |
| `room.error` | room 进入错误状态 |
| `archive.created` | room 消息归档完成 |

注意：

- `active.jsonl` 是聊天记录。
- `events.jsonl` 是系统运行事件。
- `runtime.log` 是给人看的简化日志。
- 三者不要混用。

---

### 5.3 Room State 模型

推荐 `state.json` 结构：

```json
{
  "status": "running",
  "policy": "round_robin",
  "turn_index": 1,
  "round": 3,
  "turn_count": 12,
  "max_turns": 50,
  "order": ["openclaw", "hermes"],
  "current_turn": {
    "turn_id": "turn_20260522102930123456",
    "agent_id": "openclaw",
    "state": "waiting_response",
    "delivery_id": "deliv_20260522102930123456",
    "correlation_id": "corr_20260522102930123456",
    "started_at": "2026-05-22 10:29:30",
    "timeout_at": "2026-05-22 10:31:30",
    "input_message_ids": ["msg_001", "msg_002"],
    "input_line_max": 12,
    "response_message_id": "",
    "attempts": 1,
    "last_error": ""
  },
  "last_message_id": "msg_002",
  "last_error": ""
}
```

兼容要求：

- 老字段 `waiting_for` / `waiting_line` 初期可以保留，但新逻辑应优先使用 `current_turn`。
- 如果检测到旧 state，应自动迁移为新结构。
- 不要在运行中的 room 里破坏现有 `turn_index/order/max_turns` 语义。

---

### 5.4 Delivery Ticket 模型

Adapter 投递后，不应只返回布尔值，而应返回 DeliveryTicket。

```json
{
  "ok": true,
  "delivery_id": "deliv_20260522102930123456",
  "turn_id": "turn_20260522102930123456",
  "agent_id": "openclaw",
  "adapter_type": "openclaw_sessions",
  "response_mode": "callback",
  "correlation_id": "corr_20260522102930123456",
  "detail": "HTTP 200",
  "sync_response": "",
  "raw_response": "...",
  "error": ""
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `ok` | 投递是否成功 |
| `delivery_id` | 本次投递 ID |
| `turn_id` | 当前 turn ID |
| `agent_id` | 被唤醒 Agent |
| `adapter_type` | adapter 类型 |
| `response_mode` | 回复模式 |
| `correlation_id` | 投递/回复关联 ID |
| `detail` | 简短结果说明 |
| `sync_response` | 如果同步返回了回复，写这里 |
| `raw_response` | 原始返回，可用于调试 |
| `error` | 错误信息 |

---

## 6. Adapter 层改造

### 6.1 Adapter 统一接口

推荐定义统一接口。

```python
class BaseAdapter:
    type: str

    def capability(self, agent_cfg: dict) -> dict:
        ...

    def wake(self, delivery: DeliveryRequest) -> DeliveryTicket:
        ...

    def normalize_config(self, agent_cfg: dict) -> dict:
        ...

    def health_check(self, agent_cfg: dict) -> dict:
        ...
```

`DeliveryRequest` 推荐字段：

```json
{
  "room_id": "room_main",
  "agent_id": "openclaw",
  "turn_id": "turn_xxx",
  "correlation_id": "corr_xxx",
  "message": "[hermes] hello",
  "from": "hermes,user",
  "callback_url": "http://127.0.0.1:7899/api/rooms/room_main/agents/openclaw/callback",
  "room_path": "~/.agent-bridge/rooms/room_main",
  "active_file": "~/.agent-bridge/rooms/room_main/active.jsonl",
  "input_messages": []
}
```

### 6.2 Capability 设计

每个 adapter 必须声明能力：

```json
{
  "type": "openclaw_sessions",
  "configured": true,
  "automatic": true,
  "wake_modes": ["http"],
  "response_modes": ["callback", "pull_session"],
  "supports_active_push": true,
  "supports_streaming": false,
  "requires_callback_url": true,
  "health": "configured"
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `configured` | 配置是否完整 |
| `automatic` | 是否可自动唤醒 |
| `wake_modes` | 支持的唤醒方式 |
| `response_modes` | 支持的回复方式 |
| `supports_active_push` | 是否支持 Agent 主动发言 |
| `supports_streaming` | 是否支持流式输出 |
| `requires_callback_url` | 是否必须提供 callback_url |
| `health` | `configured` / `missing_config` / `manual` / `error` |

### 6.3 response_mode 枚举

必须支持以下回复模式：

| response_mode | 说明 |
|---|---|
| `sync` | wake 调用同步返回最终回复 |
| `callback` | Agent 后续 POST 回 Bridge callback API |
| `file_outbox` | Agent 后续写入 outbox 文件，Bridge 监听/轮询读取 |
| `pull_session` | Bridge 后续从 Agent session/history API 拉取回复 |
| `mcp_tool` | Agent 通过 MCP tool 调用 `reply_turn` |
| `manual` | 需要人工手动写入 |
| `none` | 不期待回复，例如通知型 Agent |

### 6.4 adapter 类型建议

#### 6.4.1 native_http

用于通用 HTTP webhook。

适用：

- Hermes webhook
- 自定义本地 HTTP Agent
- 任何能接收 POST 的 Agent

配置示例：

```yaml
adapter:
  type: native_http
  wakeup:
    url: http://127.0.0.1:8644/webhooks/agent-reply
    method: POST
    headers:
      Content-Type: application/json
    body_template:
      message: "{{message}}"
      room_id: "{{room}}"
      turn_id: "{{turn_id}}"
      correlation_id: "{{correlation_id}}"
      callback_url: "{{callback_url}}"
  response:
    mode: callback
    timeout_seconds: 120
```

#### 6.4.2 openclaw_sessions

用于 OpenClaw HTTP sessions API。

注意：OpenClaw sessions_send 返回 200 不等于 Agent 已回复，所以推荐使用 `callback` 或 `pull_session`。

配置示例：

```yaml
adapter:
  type: openclaw_sessions
  wakeup:
    url: http://127.0.0.1:18789/tools/invoke
    method: POST
    auth:
      type: bearer
      token_path: ~/.openclaw/openclaw.json
      token_jsonpath: gateway.auth.password
    tool: sessions_send
    sessionKey: agent:main:main
    message_template: |
      [Agent Bridge]
      room_id={{room}}
      turn_id={{turn_id}}
      correlation_id={{correlation_id}}
      callback_url={{callback_url}}

      你必须在生成最终回复后，将回复写回 Agent Bridge。
      如果你支持 MCP，请调用 agent_bridge.reply_turn。
      如果你不支持 MCP，请 POST 到 callback_url。

      当前消息：
      {{message}}
  response:
    mode: callback
    timeout_seconds: 180
```

#### 6.4.3 cli

用于 CLI Agent。

配置示例：

```yaml
adapter:
  type: cli
  wakeup:
    command:
      - my-agent
      - run
      - --room
      - "{{room}}"
    stdin: |
      room_id={{room}}
      turn_id={{turn_id}}
      correlation_id={{correlation_id}}
      {{message}}
  response:
    mode: sync
    timeout_seconds: 120
```

CLI stdout 如果符合回复格式，可直接作为同步回复。

#### 6.4.4 file_mailbox

用于文件型 Agent。

配置示例：

```yaml
adapter:
  type: file_mailbox
  wakeup:
    inbox_path: ~/.agent-bridge/rooms/{{room}}/inbox/{{to}}.jsonl
  response:
    mode: file_outbox
    outbox_path: ~/.agent-bridge/rooms/{{room}}/outbox/{{to}}.jsonl
    timeout_seconds: 180
```

Bridge 投递时写 inbox，Agent 完成后写 outbox。

#### 6.4.5 mcp_tool

用于支持 MCP 工具调用的 Agent。

这里的含义不是“Bridge 调用 Agent MCP”，而是：

> Agent 被唤醒后，通过 Agent Bridge 暴露的 MCP tool 回写消息。

配置示例：

```yaml
adapter:
  type: mcp_tool
  wakeup:
    mode: external
    instructions_template: |
      你现在参与 Agent Bridge room。
      room_id={{room}}
      turn_id={{turn_id}}
      correlation_id={{correlation_id}}
      请调用 MCP tool: agent_bridge.reply_turn 写回最终回复。
  response:
    mode: mcp_tool
    timeout_seconds: 180
```

#### 6.4.6 manual

用于不可自动唤醒的 Agent。

```yaml
adapter:
  type: manual
  response:
    mode: manual
```

runtime 遇到 manual Agent 时，不应直接报系统错误，而应进入 `manual_required` 状态，并在 UI 显示需要人工介入。

---

## 7. 事件驱动机制

### 7.1 EventBus 职责

新增 `core/events.py`，负责：

1. 生成事件 ID
2. 追加事件到 `events.jsonl`
3. 追加人类可读日志到 `runtime.log`
4. 将 room_id 放入调度队列
5. 提供事件查询能力

接口建议：

```python
def emit_event(shared_dir, room_id, type, actor="", turn_id="", correlation_id="", message_id="", meta=None):
    ...

def read_events(shared_dir, room_id, limit=500):
    ...
```

### 7.2 Scheduler 职责

新增 `core/scheduler.py`，负责：

- 内存队列
- 去重 room 调度
- worker loop
- 手动 `schedule_room(room_id)`
- 兜底定时扫描 running rooms

建议行为：

```text
任何会改变 room 状态的动作，都应该 emit_event + schedule_room。
```

例如：

```text
用户发消息 -> message.created -> schedule(room)
Agent callback -> agent.response.received -> schedule(room)
room start -> room.started -> schedule(room)
turn timeout -> turn.timeout -> schedule(room)
outbox 发现新回复 -> agent.response.received -> schedule(room)
```

### 7.3 轮询不删除，只降级

保留 PollManager，但定位改变：

```text
过去：PollManager 是主驱动
未来：PollManager 是兜底扫描器
```

建议默认值：

```text
事件触发：立即
兜底扫描：3~10 秒
旧 legacy poll：可配置开启/关闭
```

不要再依赖 180 秒作为主要交互驱动。

---

## 8. Room Runtime 状态机

新增 `core/runtime.py`。

### 8.1 状态枚举

room status：

```text
paused
running
error
archived
```

turn state：

```text
idle
selecting_agent
collecting_pending
delivering
waiting_response
completed
timeout
failed
manual_required
skipped
```

### 8.2 状态流转

正常流转：

```text
idle
  -> selecting_agent
  -> collecting_pending
  -> delivering
  -> waiting_response
  -> completed
  -> advance_turn
  -> idle
```

同步回复流转：

```text
idle
  -> selecting_agent
  -> collecting_pending
  -> delivering
  -> completed
  -> advance_turn
```

无 pending 流转：

```text
idle
  -> selecting_agent
  -> collecting_pending
  -> skipped
  -> advance_turn 或 keep_turn
```

失败流转：

```text
delivering
  -> failed
  -> retry / skip / room.error
```

超时流转：

```text
waiting_response
  -> timeout
  -> retry / skip / room.error / manual_required
```

### 8.3 run_room_step 伪代码

```python
def run_room_step(config, room_id):
    room = load_room(config, room_id)
    state = read_room_state(room_id)

    if state.status != "running":
        return noop("room not running")

    if max_turns_reached(state):
        pause_room(room_id, reason="max_turns reached")
        return

    current_turn = state.get("current_turn")

    if current_turn and current_turn.state == "waiting_response":
        if response_received(current_turn):
            complete_turn(current_turn)
            advance_turn(room, state)
            schedule_room(room_id)
            return

        if turn_timeout(current_turn):
            handle_timeout(room, state, current_turn)
            return

        return noop("waiting response")

    agent_id = select_next_agent(room, state)
    pending = collect_pending_messages(room, state, agent_id)

    if not pending:
        handle_no_pending(room, state, agent_id)
        return

    turn = create_turn(room_id, agent_id, pending)
    save_current_turn(state, turn)

    ticket = adapter.wake(turn.delivery_request)

    if not ticket.ok:
        handle_delivery_failed(room, state, turn, ticket)
        return

    if ticket.sync_response:
        msg = append_message_from_sync_response(ticket)
        complete_turn(turn, msg)
        advance_turn(room, state)
        schedule_room(room_id)
        return

    if ticket.response_mode in ["callback", "file_outbox", "mcp_tool", "pull_session"]:
        mark_waiting_response(turn, ticket)
        save_state(state)
        return

    if ticket.response_mode in ["none"]:
        complete_turn(turn, None)
        advance_turn(room, state)
        schedule_room(room_id)
        return
```

---

## 9. 双向通信设计

### 9.1 HTTP Callback API

必须新增统一回写入口：

```text
POST /api/rooms/{room_id}/agents/{agent_id}/callback
```

请求体：

```json
{
  "turn_id": "turn_20260522102930123456",
  "correlation_id": "corr_20260522102930123456",
  "message": "这是 Agent 的最终回复",
  "kind": "agent",
  "meta": {
    "source": "openclaw"
  }
}
```

处理流程：

```text
1. 校验 room_id 合法
2. 校验 agent_id 存在且属于该 room
3. 校验 callback token 或本地访问来源
4. 读取 state.current_turn
5. 如果 current_turn 存在：
   - 校验 agent_id 是否等于 current_turn.agent_id
   - 校验 turn_id / correlation_id 是否匹配
6. append_room_message
7. emit agent.response.received
8. 如果当前 turn 正在等待该回复，则标记 response_message_id
9. schedule_room(room_id)
10. 返回 ok
```

响应：

```json
{
  "ok": true,
  "room_id": "room_main",
  "agent_id": "openclaw",
  "message_id": "msg_20260522103000123456",
  "scheduled": true
}
```

### 9.2 主动发言 API

新增或复用：

```text
POST /api/rooms/{room_id}/agents/{agent_id}/message
```

请求体：

```json
{
  "message": "我主动插一句",
  "mode": "normal",
  "to": "hermes",
  "meta": {}
}
```

`mode` 枚举：

| mode | 说明 |
|---|---|
| `normal` | 普通消息，写入后触发调度 |
| `interrupt` | 请求打断当前等待，由 policy 决定是否允许 |
| `whisper` | 私聊指定 Agent |
| `system` | 系统消息，通常只有 Bridge 可发 |

### 9.3 File Outbox Watcher

对于 `file_outbox` 模式，新增 watcher。

推荐目录：

```text
rooms/{room_id}/outbox/{agent_id}.jsonl
```

outbox 行格式：

```json
{
  "ts": "2026-05-22 10:30:00",
  "agent_id": "hermes",
  "turn_id": "turn_xxx",
  "correlation_id": "corr_xxx",
  "message": "回复内容",
  "meta": {}
}
```

Watcher 读取到新行后，转换为 `agent.response.received` 事件，并调用相同的内部处理逻辑。

不要让 watcher 直接修改复杂状态，应统一走 `receive_agent_response()`。

---

## 10. MCP Server 设计

### 10.1 MCP 定位

Agent Bridge 可以作为 MCP Server 暴露聊天室工具。

但注意：

```text
Agent Bridge Core 不依赖 MCP。
MCP Server 只是 Core 的一个入口。
```

也就是说：

```text
HTTP API、CLI、File Outbox、MCP Tool 最终都应转换为相同内部事件。
```

### 10.2 必须提供的 MCP Tools

第一版建议提供以下工具。

#### 10.2.1 agent_bridge.list_rooms

功能：列出可访问 room。

输入：

```json
{}
```

输出：

```json
{
  "rooms": [
    {
      "room_id": "room_main",
      "name": "Main Room",
      "status": "running",
      "agents": ["openclaw", "hermes"]
    }
  ]
}
```

#### 10.2.2 agent_bridge.get_current_turn

功能：读取当前 turn。

输入：

```json
{
  "room_id": "room_main"
}
```

输出：

```json
{
  "room_id": "room_main",
  "current_turn": {
    "turn_id": "turn_xxx",
    "agent_id": "openclaw",
    "state": "waiting_response",
    "correlation_id": "corr_xxx"
  }
}
```

#### 10.2.3 agent_bridge.read_messages

功能：读取 room 消息。

输入：

```json
{
  "room_id": "room_main",
  "limit": 20,
  "since_message_id": ""
}
```

输出：

```json
{
  "room_id": "room_main",
  "messages": []
}
```

#### 10.2.4 agent_bridge.get_agent_pending

功能：读取某个 Agent 当前 pending 消息。

输入：

```json
{
  "room_id": "room_main",
  "agent_id": "openclaw"
}
```

输出：

```json
{
  "room_id": "room_main",
  "agent_id": "openclaw",
  "messages": []
}
```

#### 10.2.5 agent_bridge.reply_turn

功能：回复当前 turn。

这是最关键工具。

输入：

```json
{
  "room_id": "room_main",
  "agent_id": "openclaw",
  "turn_id": "turn_xxx",
  "correlation_id": "corr_xxx",
  "message": "最终回复内容"
}
```

内部行为：

```text
1. 校验 room / agent / turn / correlation_id
2. append_room_message
3. emit agent.response.received
4. 标记 current_turn.response_message_id
5. schedule_room
```

输出：

```json
{
  "ok": true,
  "message_id": "msg_xxx",
  "scheduled": true
}
```

#### 10.2.6 agent_bridge.send_message

功能：主动发言，不一定属于当前 turn。

输入：

```json
{
  "room_id": "room_main",
  "agent_id": "openclaw",
  "message": "我主动补充一句",
  "to": "",
  "mode": "normal"
}
```

输出：

```json
{
  "ok": true,
  "message_id": "msg_xxx",
  "scheduled": true
}
```

### 10.3 MCP 安全原则

必须遵守：

```text
1. MCP Server 默认只绑定 127.0.0.1
2. 不暴露任意文件读写工具
3. 不暴露 shell / exec 工具
4. 每个 agent_id 应有独立 token 或至少 callback secret
5. reply_turn 必须校验 turn_id 和 correlation_id
6. 所有 MCP tool call 写 events.jsonl
7. 工具描述必须短、清晰、不可夹带隐藏规则
8. MCP 是标准工具入口，不是无限系统权限入口
```

---

## 11. Agent Skill 设计

### 11.1 Skill 定位

Skill 是给 Agent 看的“接入说明书”。

它不负责执行工具，而是告诉 Agent：

```text
你如何作为 Agent Bridge room 成员工作。
你收到消息后必须如何回复。
你应该调用什么 MCP tool 或 callback API。
你不能只在自己的会话里回答。
```

### 11.2 Skill 目录

建议新增：

```text
core/skills/agent-bridge-room-participant/
  SKILL.md
  references/
    callback.md
    mcp-tools.md
    message-schema.json
    event-schema.json
    examples.md
```

### 11.3 SKILL.md 推荐内容

```md
# Agent Bridge Room Participant Skill

## Purpose

You are participating in an Agent Bridge room as one named Agent.
You must not only answer in your local conversation. You must write your final response back to Agent Bridge.

## Required Inputs

Every task message may include:

- room_id
- agent_id
- turn_id
- correlation_id
- callback_url
- pending messages

## Required Behavior

1. Read the pending messages.
2. Generate one clear final reply.
3. If MCP tools are available, call `agent_bridge.reply_turn`.
4. If MCP tools are not available but callback_url exists, POST to callback_url.
5. Include turn_id and correlation_id when replying.
6. Do not impersonate another agent_id.
7. Do not directly edit active.jsonl.

## Active Push

If you need to speak outside your turn, call `agent_bridge.send_message` or POST to the active-message callback endpoint if allowed by policy.

## Forbidden

- Do not modify `~/.agent-bridge` files directly unless explicitly configured as file_outbox mode.
- Do not change history files.
- Do not fake another Agent's identity.
- Do not ignore turn_id/correlation_id.
```

### 11.4 Skill 与 MCP 的关系

```text
Skill 负责告诉 Agent：“你应该怎么做”。
MCP 负责给 Agent：“你可以调用什么工具”。
```

两者配合使用时，效果最佳。

---

## 12. 配置文件改造

### 12.1 新版 bridge.yaml 示例

```yaml
shared_dir: ~/.agent-bridge

server:
  host: 127.0.0.1
  port: 7899
  enable_mcp: true
  enable_scheduler: true
  fallback_poll_interval_seconds: 5

security:
  callback_tokens:
    openclaw: "${AGENT_BRIDGE_OPENCLAW_TOKEN}"
    hermes: "${AGENT_BRIDGE_HERMES_TOKEN}"

agents:
  openclaw:
    id: openclaw
    display_name: OpenClaw
    color: "#4ecdc4"
    adapter:
      type: openclaw_sessions
      wakeup:
        url: http://127.0.0.1:18789/tools/invoke
        method: POST
        auth:
          type: bearer
          token_path: ~/.openclaw/openclaw.json
          token_jsonpath: gateway.auth.password
        tool: sessions_send
        sessionKey: agent:main:main
        message_template: |
          [Agent Bridge]
          room_id={{room}}
          agent_id={{to}}
          turn_id={{turn_id}}
          correlation_id={{correlation_id}}
          callback_url={{callback_url}}

          请生成回复后写回 Agent Bridge。
          优先调用 MCP tool: agent_bridge.reply_turn。
          如果无法调用 MCP，请 POST 到 callback_url。

          当前待处理消息：
          {{message}}
      response:
        mode: callback
        timeout_seconds: 180
        retry: 1

  hermes:
    id: hermes
    display_name: Hermes
    color: "#ff6b6b"
    adapter:
      type: native_http
      wakeup:
        url: http://127.0.0.1:8644/webhooks/agent-reply
        method: POST
        headers:
          Content-Type: application/json
        body_template:
          message: "{{message}}"
          room_id: "{{room}}"
          agent_id: "{{to}}"
          turn_id: "{{turn_id}}"
          correlation_id: "{{correlation_id}}"
          callback_url: "{{callback_url}}"
      response:
        mode: callback
        timeout_seconds: 120

rooms:
  room_main:
    id: room_main
    name: Main Room
    agents:
      - openclaw
      - hermes
    order:
      - openclaw
      - hermes
    policy:
      type: round_robin
      allow_interrupt: false
      on_timeout: skip
      on_delivery_failed: error
      on_agent_push: append_and_schedule
    status: paused
    max_turns: 50
```

### 12.2 旧配置兼容

旧配置：

```yaml
agents:
  bob:
    wakeup:
      url: http://127.0.0.1:18789/tools/invoke
      method: POST
      body_template:
        tool: sessions_send
        args:
          sessionKey: agent:main:main
          message: "{{message}}"
```

应自动转换为：

```yaml
agents:
  bob:
    adapter:
      type: native_http
      wakeup:
        url: http://127.0.0.1:18789/tools/invoke
        method: POST
        body_template:
          tool: sessions_send
          args:
            sessionKey: agent:main:main
            message: "{{message}}"
      response:
        mode: callback
        timeout_seconds: 180
```

注意：如果无法判断 response mode，默认应为 `callback` 或 `manual`，不要默认认为 HTTP 200 等于回复完成。

---

## 13. API 改造清单

### 13.1 新增 API

#### POST /api/rooms/{room_id}/agents/{agent_id}/callback

Agent turn 回复入口。

#### POST /api/rooms/{room_id}/agents/{agent_id}/message

Agent 主动发言入口。

#### GET /api/rooms/{room_id}/events

读取 room 事件。

#### GET /api/rooms/{room_id}/turn

读取当前 turn。

#### POST /api/rooms/{room_id}/schedule

手动触发 room 调度。

### 13.2 现有 API 调整

#### POST /api/rooms/{room_id}/send

现有用户发消息接口应在写入消息后：

```text
append_room_message
emit message.created
schedule_room
```

#### POST /api/rooms/{room_id}/start

启动 room 后：

```text
set status running
emit room.started
schedule_room
```

#### POST /api/rooms/{room_id}/tick

保留，但内部应调用 runtime step，而不是旧式同步大流程。

---

## 14. 安全设计

### 14.1 agent_id 校验

沿用现有规则：

```text
^[a-zA-Z0-9_-]+$
```

room_id 同理。

### 14.2 callback 鉴权

至少支持以下一种：

```text
Authorization: Bearer <agent-token>
```

或 query 参数：

```text
?token=xxx
```

推荐 Bearer。

每个 Agent 独立 token：

```yaml
security:
  callback_tokens:
    openclaw: "${AGENT_BRIDGE_OPENCLAW_TOKEN}"
```

### 14.3 turn 校验

`reply_turn` 和 callback 必须校验：

```text
1. room_id 存在
2. agent_id 属于 room
3. current_turn.agent_id == agent_id
4. turn_id 匹配
5. correlation_id 匹配
```

如果是主动消息，则不能走 `reply_turn`，必须走 `send_message`。

### 14.4 禁止能力

MCP 和 HTTP API 都不应暴露：

```text
任意文件读取
任意文件写入
shell 执行
删除 history
修改 bridge.yaml 的危险接口
```

配置编辑仍由 UI/API 管理，但 Agent 不应通过 MCP 直接改配置。

---

## 15. Timeout / Retry / Error 策略

### 15.1 timeout

每个 adapter response 配置：

```yaml
response:
  timeout_seconds: 180
```

runtime 每次执行时检查：

```text
if now > current_turn.timeout_at:
    emit turn.timeout
    根据 room.policy.on_timeout 处理
```

### 15.2 on_timeout 策略

支持：

| 策略 | 行为 |
|---|---|
| `skip` | 跳过当前 Agent，推进下一位 |
| `retry` | 重试当前 Agent |
| `pause` | 暂停 room |
| `error` | room 进入 error |
| `manual` | 进入 manual_required |

### 15.3 retry

记录在 current_turn：

```json
{
  "attempts": 1,
  "max_attempts": 2
}
```

超过次数后执行 on_delivery_failed 或 on_timeout。

---

## 16. UI 改造建议

UI 需要显示：

1. room 当前状态
2. 当前 turn
3. waiting 的 Agent
4. turn_id / correlation_id，可折叠显示
5. 最近 events
6. 最近 runtime.log
7. adapter capability
8. callback URL 测试按钮
9. MCP Server 开启状态
10. Agent 是否支持自动回复

在 waiting_response 状态时，UI 应展示：

```text
正在等待 openclaw 回复
turn_id: xxx
correlation_id: xxx
callback_url: xxx
已等待：35s / 180s
操作：手动完成 / 跳过 / 重试 / 暂停
```

---

## 17. 实施步骤

### 阶段一：最小闭环，解决 waiting_response 死等

目标：先让 OpenClaw / Hermes 可以写回。

任务：

1. 新增 callback API：`POST /api/rooms/{room}/agents/{agent}/callback`
2. 新增 `turn_id` / `correlation_id` 生成
3. 在 `tick_room` 投递消息时把 callback_url 注入模板
4. callback 收到后 append message
5. callback 后立即 schedule 或至少立即 tick
6. UI 显示 callback_url 和 waiting turn

验收：

```text
OpenClaw 被 sessions_send 唤醒后，只要调用 callback，就能解除 waiting_response，并推进下一位 Agent。
```

### 阶段二：Adapter Response Mode 改造

任务：

1. 定义 AdapterConfig V2
2. 定义 DeliveryRequest / DeliveryTicket
3. 支持 response.mode
4. 旧 wakeup 自动转换
5. native_http / cli / file_mailbox 初步适配

验收：

```text
不同 adapter 能清楚声明 sync/callback/file_outbox/manual，不再把 HTTP 200 当成最终回复。
```

### 阶段三：EventBus + Scheduler

任务：

1. 新增 events.jsonl
2. 新增 emit_event
3. 新增 schedule_room
4. 用户 send / callback / start 都触发 schedule
5. PollManager 降级为 fallback scan

验收：

```text
用户发消息后无需等待 180 秒，room 能立即调度。
Agent 回写后能立即推进下一轮。
```

### 阶段四：Room Runtime 状态机

任务：

1. 新增 runtime.py
2. 拆分 tick_room 中的状态机逻辑
3. 引入 current_turn
4. 支持 timeout / retry / skip / manual_required
5. 保持旧 state 兼容迁移

验收：

```text
state.json 能清楚描述当前 turn。
超时、重试、跳过行为可预测。
```

### 阶段五：MCP Server

任务：

1. 新增 mcp_server.py
2. 暴露 list_rooms/read_messages/get_current_turn/reply_turn/send_message
3. 所有 MCP tool 内部调用同一套 Core API
4. MCP 默认只监听本地
5. MCP tool 调用写 events.jsonl

验收：

```text
支持 MCP 的 Agent 可以通过 agent_bridge.reply_turn 完成回写。
```

### 阶段六：Agent Skill

任务：

1. 新增 Skill 目录
2. 编写 SKILL.md
3. 编写 callback.md / mcp-tools.md 示例
4. 在 OpenClaw message_template 中提示使用该 Skill 规则

验收：

```text
把 Skill 给开发/执行 Agent 后，Agent 能理解必须通过 MCP 或 callback 写回聊天室。
```

---

## 18. 测试计划

### 18.1 单元测试

必须覆盖：

```text
message append/read
legacy message compatibility
event append/read
state migration
turn_id/correlation_id generation
callback validation
reply_turn validation
adapter capability
native_http delivery ticket
cli sync response
file_outbox parse
runtime timeout
runtime retry
runtime skip
```

### 18.2 集成测试

#### 场景一：用户消息触发 OpenClaw

```text
1. room 有 openclaw/hermes
2. 用户发送消息
3. scheduler 立即触发 openclaw
4. openclaw adapter 返回 callback waiting
5. 模拟 callback
6. runtime 推进到 hermes
```

#### 场景二：CLI 同步回复

```text
1. CLI adapter stdout 返回文本
2. Bridge 自动写入 active.jsonl
3. 不进入 waiting_response
4. 直接推进下一位
```

#### 场景三：file_outbox 回复

```text
1. Bridge 写 inbox
2. 测试写 outbox
3. watcher 读取 outbox
4. 生成 agent.response.received
5. runtime 推进
```

#### 场景四：timeout skip

```text
1. Agent 被唤醒后不回复
2. 超过 timeout_seconds
3. room.policy.on_timeout = skip
4. runtime 跳过当前 Agent
5. 推进下一位
```

#### 场景五：MCP reply_turn

```text
1. Agent 通过 MCP tool 调用 reply_turn
2. Bridge 校验 turn_id/correlation_id
3. 写入消息
4. 触发下一轮
```

---

## 19. 兼容性要求

### 19.1 active.jsonl 兼容

必须继续支持旧格式：

```json
{"ts":"2026-05-15 14:24:47","from":"alice","msg":"你好"}
```

读取时补默认字段即可，不要强制迁移历史文件。

### 19.2 wakeup 兼容

如果 agent 配置只有 `wakeup`，自动转换为 `adapter.type=native_http`。

### 19.3 legacy run_poll

短期保留 legacy `run_poll`，但 room runtime 优先走新逻辑。

长期可将 legacy 功能标记为 compatibility mode。

---

## 20. 开发注意事项

1. 不要一次性删除旧逻辑。
2. 新逻辑先在 room runtime 生效，legacy active.jsonl 可以保留。
3. 所有状态写入必须加锁。
4. 不要让 Adapter 直接改 state。
5. 不要让 HTTP handler 直接实现复杂状态机。
6. callback / MCP / file_outbox 都走同一个 `receive_agent_response()`。
7. `turn_id` 和 `correlation_id` 是解决 waiting 死等和错配回复的核心，不要省略。
8. HTTP 200 只代表投递成功，不代表 Agent 回复成功。
9. MCP 是可选入口，不是核心依赖。
10. Skill 是 Agent 行为规范，不是运行时执行器。

---

## 21. 最终验收标准

完成后，系统应满足：

### 21.1 功能验收

- 用户发消息后，room 能在 1 秒级触发调度。
- Agent 被唤醒后，能通过 callback / MCP / file_outbox 任一方式回写。
- OpenClaw sessions_send 返回 200 后，不再误判为已回复。
- 如果 Agent 不回写，系统能 timeout，并按策略 skip/retry/pause/error。
- Agent 可以主动发消息。
- UI 能显示当前等待的 turn、Agent、timeout、callback_url。
- MCP Agent 可以调用 `agent_bridge.reply_turn` 完成回复。
- Skill 文档可以直接给开发 Agent 使用。

### 21.2 架构验收

- `server.py` 不包含主要调度状态机。
- `rooms.py` 不直接调用具体 adapter。
- `runtime.py` 负责状态机。
- `events.py` 负责事件落盘。
- `scheduler.py` 负责调度触发。
- `adapters/*` 负责具体 Agent 通信。
- 所有回写入口统一走同一套 response 处理逻辑。

### 21.3 安全验收

- callback 校验 agent_id / room_id / token。
- reply_turn 校验 turn_id / correlation_id。
- MCP 不暴露任意文件读写或 shell 执行。
- Agent 不能伪造其他 agent_id。
- 所有关键操作写 events.jsonl。

---

## 22. 给开发 Agent 的执行建议

请按阶段执行，不要直接大爆改。

推荐顺序：

```text
1. 新增 callback API + turn_id/correlation_id
2. 修改 tick_room，让投递消息包含 callback_url
3. callback 后 append message + 立即 tick/schedule
4. 引入 DeliveryTicket 和 response.mode
5. 新增 events.py
6. 新增 scheduler.py
7. 拆 runtime.py
8. 新增 MCP Server
9. 新增 Skill 文档
10. 补测试与 UI
```

每完成一个阶段，都必须保证：

```text
python -m unittest discover -s tests
```

可以通过。

如果某阶段需要破坏旧接口，必须先提供兼容层。

---

## 23. 关键结论

Agent Bridge 的下一步不是简单把轮询间隔从 180 秒改成 3 秒，而是要补齐协议层。

正确方向是：

```text
文件系统负责透明持久化。
EventBus 负责驱动。
Room Runtime 负责状态机。
Adapter 负责不同 Agent 的唤醒和回复方式。
Callback / MCP / File Outbox 负责回写。
Skill 负责告诉 Agent 如何正确参与聊天室。
```

这样 Agent Bridge 才能从“本地异步递纸条工具”升级成真正可扩展的“轻量级多 Agent 聊天室运行时”。

