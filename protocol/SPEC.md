# Agent Bridge — 通信协议规范 v2

## 1. 概述

### 1.1 Agent Bridge 是什么

Agent Bridge 是一个**文件存储 + 异步投递**的 agent-to-agent 通信协议。多个 agent 不直接对话，
而是通过共享目录下按房间（Room）组织的 JSONL 文件交换消息。Room Runtime 状态机负责调度
agent 轮次、投递消息、等待回复、处理超时。

本协议不绑定任何特定 agent 框架。Hermes Agent、OpenClaw、Claude Code 等任何支持
HTTP callback 或 MCP tool 的 agent 均可接入。

### 1.2 V1 → V2 演进

| 维度         | V1                                      | V2                                                        |
|-------------|------------------------------------------|-----------------------------------------------------------|
| 消息模型     | 全局 `active.jsonl`，简单 `ts/from/msg` | 按房间隔离，消息含 `id/kw/room/correlation_id` 等完整字段        |
| 调度方式     | 外部 cron 轮询 + webhook                   | 内部 Scheduler + Room Runtime 状态机                        |
| 投递         | 直接 HTTP POST 到 webhook                 | Adapter 注册表，7 种适配器，返回 DeliveryTicket               |
| 回复         | 写文件 + 对方轮询                          | callback URL / MCP reply_turn / file_outbox 三条回写通道      |
| 状态管理     | 游标文件（`.*_cursor`）                   | `state.json`（含 `current_turn`、`turn_index` 等）          |
| 可观测性     | 无                                        | EventBus（`events.jsonl` + `runtime.log`）                  |
| 超时策略     | 无                                        | skip / retry / pause / error / manual                      |
| 安全         | 基本建议                                  | token 验证、ID 校验、消息清理、HMAC 比较                       |

---

## 2. 消息格式

### 2.1 JSONL 结构

所有消息存储在房间目录下的 `active.jsonl`（UTF-8 编码，每行一条完整 JSON）：

```jsonl
{"id":"msg_20260530143000001","ts":"2026-05-30 14:30:00","room":"demo_room","from":"alice","kind":"user","msg":"你好，今天有什么进展？"}
{"id":"msg_20260530143100123","ts":"2026-05-30 14:31:00","room":"demo_room","from":"bob","kind":"agent","msg":"有的，我完成了 API 对接。","reply_to":"turn_20260530143000002","correlation_id":"corr_20260530143000003"}
{"id":"msg_20260530143200456","ts":"2026-05-30 14:32:00","room":"demo_room","from":"system","kind":"system","msg":"房间已启动，共 2 个 Agent 参与。"}
```

### 2.2 字段说明

| 字段              | 类型   | 必填 | 说明                                                                 |
|-------------------|--------|------|----------------------------------------------------------------------|
| `id`              | string | 是   | 全局唯一消息 ID，格式 `msg_YYYYMMDDHHMMSSffffff`（微秒级）             |
| `ts`              | string | 是   | 时间戳，格式 `YYYY-MM-DD HH:MM:SS`（24 小时制，本地时间）              |
| `room`            | string | 是   | 房间 ID，仅允许 `[a-zA-Z0-9_-]+`                                      |
| `from`            | string | 是   | 发送方标识（Agent ID 或 `"user"` / `"system"`）                      |
| `to`              | string | 否   | 接收方标识。省略时为广播（房间内所有非发送方可见）                       |
| `kind`            | string | 是   | 消息类型，见 §2.3                                                     |
| `msg`             | string | 是   | 消息正文；可包含换行符 `\n`，建议 < 50KB                                |
| `reply_to`        | string | 否   | 回复的目标消息 ID 或 turn_id，用于关联对话                             |
| `correlation_id`  | string | 否   | 关联 ID，用于将一条消息与某个 turn / delivery 绑定                      |
| `meta`            | object | 否   | 扩展元数据，任意 key-value。如 `{"source": "mcp_server", "model": "gpt-4"}` |

### 2.3 kind（消息类型）

| kind      | 含义           | 典型发送方           |
|-----------|----------------|---------------------|
| `user`    | 用户消息        | 人类用户（通过 UI）    |
| `agent`   | Agent 消息      | AI Agent            |
| `system`  | 系统消息        | Agent Bridge 自身     |
| `event`   | 事件消息        | EventBus（保留）      |

> **V1 兼容**：旧消息仅有 `ts`、`from`、`msg` 三个字段。V2 运行时通过 `normalize_message()`
> 自动补全缺失字段（`id=""`, `kind="agent"`, `room=""` 等），确保向下兼容。

### 2.4 消息 ID 生成规则

```
msg_{timestamp_microseconds}
evt_{timestamp_microseconds}    # 事件
turn_{timestamp_microseconds}   # 轮次
deliv_{timestamp_microseconds}  # 投递
corr_{timestamp_microseconds}   # 关联
```

---

## 3. 房间模型

### 3.1 目录结构

```
~/.agent-bridge/
└── rooms/
    └── <room_id>/               # 每个房间一个子目录
        ├── active.jsonl         # 活跃消息流（实时读写）
        ├── state.json           # 房间运行时状态
        ├── events.jsonl         # 事件流（EventBus）
        ├── runtime.log          # 人类可读运行日志
        ├── room.json            # 房间配置快照
        ├── cursors/             # 每个 agent 的读取游标
        │   ├── alice
        │   └── bob
        └── history/             # 归档目录
            ├── 2026-05-30_1430.jsonl
            └── 2026-05-30_1600.jsonl
```

### 3.2 active.jsonl

活跃消息流。每行一条消息 JSON，追加写入。当满足以下任一条件时被归档：

- **空闲超时**：超过 30 分钟无新消息（`ARCHIVE_IDLE_MINUTES`）
- **消息数上限**：超过 60 条消息（`ARCHIVE_MSG_LIMIT`）

归档操作：将 `active.jsonl` 移入 `history/`，以时间戳重命名，然后创建新的空 `active.jsonl`。

### 3.3 state.json

运行时状态文件，内容示例：

```json
{
  "status": "running",
  "policy": "round_robin",
  "turn_index": 1,
  "round": 0,
  "turn_count": 3,
  "max_turns": 50,
  "order": ["alice", "bob"],
  "current_turn": {
    "turn_id": "turn_20260530143000123456",
    "agent_id": "bob",
    "state": "waiting_response",
    "delivery_id": "deliv_20260530143000123457",
    "correlation_id": "corr_20260530143000123458",
    "started_at": "2026-05-30 14:30:00",
    "timeout_at": "2026-05-30 14:33:00",
    "timeout_seconds": 180,
    "input_message_ids": ["msg_20260530142500001"],
    "input_line_max": 5,
    "response_message_id": "",
    "attempts": 1,
    "max_attempts": 2,
    "last_error": ""
  },
  "last_message_id": "msg_20260530142500001",
  "last_error": "",
  "waiting_for": "bob",
  "waiting_line": 5
}
```

### 3.4 房间状态

| 状态        | 含义                                                   |
|------------|--------------------------------------------------------|
| `running`  | 正常运行，Scheduler 会定期执行 `run_room_step()`          |
| `paused`   | 已暂停，不调度。可手动恢复或用 API 启动                     |
| `error`    | 错误状态，需人工介入。（如 agent 配置不存在、投递失败等）     |
| `archived` | 已归档（保留状态，在 UI 中标记为只读）                       |

状态转换路径：
```
paused ──(API 启动)──▶ running
running ──(max_turns 到达)──▶ paused
running ──(投递失败/agent 不存在)──▶ error
running ──(timeout=pause)──▶ paused
running ──(timeout=error)──▶ error
```

---

## 4. Room Runtime 状态机

### 4.1 核心概念

每个房间由 `run_room_step()` 驱动。每次 step 执行以下流程：

```
1. 检查房间状态（非 running 则跳过）
2. 检查 max_turns（达上限则 pause）
3. 检查 current_turn 是否 waiting_response：
   a. 已收到回复 → complete turn，前进到下一个 agent
   b. 已超时 → 按策略处理（skip/retry/pause/error/manual）
   c. 仍在等待 → 返回 "waiting"
4. 无 current_turn 时 → 选择下一个 agent
5. 收集该 agent 的待处理消息（pending messages）
6. 若 agent 为 manual → 进入 manual_required 状态
7. 创建 Turn，通过 Adapter 投递消息
8. 根据 response_mode 处理回复
```

### 4.2 current_turn 字段

`state.json` 中的 `current_turn` 表示当前正在进行的轮次。为 `null` 时表示无活动轮次。

| 字段                  | 类型     | 说明                                  |
|----------------------|---------|---------------------------------------|
| `turn_id`            | string  | 轮次唯一 ID                            |
| `agent_id`           | string  | 当前被调度的 agent                      |
| `state`              | string  | 轮次状态（见 §4.3）                     |
| `delivery_id`        | string  | 投递唯一 ID                            |
| `correlation_id`     | string  | 关联 ID，贯穿整个 turn 生命周期          |
| `started_at`         | string  | 轮次开始时间戳                          |
| `timeout_at`         | string  | 超时截止时间戳                          |
| `timeout_seconds`    | int     | 超时秒数（默认 180）                     |
| `input_message_ids`  | list    | 投递给 agent 的消息 ID 列表              |
| `input_line_max`     | int     | 投递时 active.jsonl 的最大行号           |
| `response_message_id`| string  | agent 回复消息的 ID（已收到时非空）       |
| `attempts`           | int     | 当前投递尝试次数                         |
| `max_attempts`       | int     | 最大重试次数（默认 2）                    |
| `last_error`         | string  | 最近一次错误描述                         |

### 4.3 Turn 状态转换

完整状态列表及语义：

```
idle
  │
  ▼
selecting_agent      ← 选择下一个 agent（内部瞬态）
  │
  ▼
collecting_pending   ← 收集待处理消息（内部瞬态）
  │
  ▼
delivering           ← 正在向 agent 投递消息
  │
  ▼
waiting_response     ← 已投递，等待 agent 回复
  │
  ├──▶ completed     ← agent 回复已收到，正常结束
  ├──▶ timeout       ← 超时（进一步转为 skip/retry/pause/error/manual）
  ├──▶ failed        ← 投递失败
  └──▶ manual_required ← agent 为 manual 类型，无法自动触发
  │
  └──▶ skipped       ← 超时后被跳过，前进到下一 agent
```

状态转换图（核心流程）：

```
                    ┌──────────────┐
                    │    idle      │
                    └──────┬───────┘
                           │ pending messages detected
                           ▼
              ┌────────────────────────┐
              │  selecting_agent /     │
              │  collecting_pending    │ (internal transient)
              └────────────┬───────────┘
                           │
                    ┌──────▼──────┐
                    │  delivering │──────────────────────┐
                    └──────┬──────┘                      │
                           │ adapter.wake() ok           │ adapter.wake() fail
                           ▼                             ▼
              ┌─────────────────────┐          ┌─────────────────┐
              │  waiting_response   │          │     failed      │
              └──┬──────┬──────┬───┘          └────────┬────────┘
                 │      │      │                       │
      ┌──────────┘      │      └──────────┐            │ room → error
      ▼                 ▼                 ▼            │
  completed        timeout           manual_required   │
  (正常完成)       (超时)             (manual agent)    │
      │                 │                               │
      │           ┌─────┴─────┐                         │
      │           │ on_timeout│                         │
      │           │ 策略分发   │                         │
      │           └──┬──┬──┬──┘                         │
      │       ┌──────┘  │  └──────┐                     │
      │       ▼         ▼         ▼                     │
      │    skip      retry      pause/error/manual      │
      │    (跳过)    (重试)      (暂停/报错/人工)         │
      │       │         │                                │
      └───────┴─────────┘                                │
              │                                          │
              ▼                                          │
        turn completed (advance to next agent) ◄─────────┘
```

### 4.4 异常场景处理

| 场景               | 行为                                                                    |
|--------------------|-------------------------------------------------------------------------|
| 无待处理消息        | 跳过当前 agent，保持 turn 不变（不前进 turn_index）。检查是否需要归档      |
| agent 配置不存在    | 房间进入 `error` 状态，emit `room.error` 事件                             |
| agent 为 manual     | 进入 `manual_required` 状态，不投递                                      |
| 投递失败            | 房间进入 `error` 状态，emit `agent.wakeup.failed` 事件                    |
| sync 回复为空       | 记录警告日志，仍然前进轮次（防止卡死）                                      |
| MCP tool 投递       | 进入 `waiting_response`，等待外部系统通过 MCP 回调                          |

---

## 5. Adapter 层

### 5.1 架构设计

Adapter 层将"向 agent 投递消息"这一动作抽象为统一接口。核心是 `BaseAdapter` 抽象类：

```python
class BaseAdapter(ABC):
    type: str = "base"

    @abstractmethod
    def capability(self, agent_cfg: dict) -> dict:
        """返回该 adapter + agent 配置的能力声明"""
        ...

    @abstractmethod
    def wake(self, delivery_request: dict) -> dict:
        """向 agent 投递消息，返回 DeliveryTicket"""
        ...

    @abstractmethod
    def normalize_config(self, agent_cfg: dict) -> dict:
        """从 agent 配置中标准化 adapter 配置"""
        ...
```

### 5.2 注册表机制

Adapter 通过 `@register_adapter` 装饰器自动注册：

```python
_REGISTRY = {}  # {adapter_type: adapter_class}

def register_adapter(adapter_cls):
    _REGISTRY[adapter_cls.type] = adapter_cls
    return adapter_cls
```

运行时通过 `deliver_via_registry()` 统一调度：

```python
def deliver_via_registry(agent_cfg, message_text, from_agents, context):
    adapter = normalize_adapter(agent_cfg)
    adapter_cls = get_adapter_class(adapter["type"])
    if adapter_cls is None:
        # 回退到旧版 deliver_to_adapter（兼容层）
        return fallback_deliver(...)

    delivery_req = make_delivery_request(...)
    return adapter_cls().wake(delivery_req)
```

### 5.3 7 种适配器类型

| 类型                  | 常量                       | 说明                                                  | response_mode     |
|-----------------------|----------------------------|-------------------------------------------------------|-------------------|
| `native_http`         | `ADAPTER_NATIVE_HTTP`      | 直接 HTTP POST 到目标 agent 的 webhook 端点             | `callback`        |
| `openclaw_sessions`   | `ADAPTER_OPENCLAW_SESSIONS`| 通过 OpenClaw sessions_send 工具投递                    | `callback`        |
| `cli`                 | `ADAPTER_CLI`              | 通过 CLI 命令直接调用 agent（同步等待回复）                | `sync`            |
| `file_mailbox`        | `ADAPTER_FILE_MAILBOX`     | 写入文件 mailbox，agent 自行轮询读取                      | `file_outbox`     |
| `mcp_tool`            | `ADAPTER_MCP_TOOL`         | 向 agent 发送 MCP tool 调用指令                          | `mcp_tool`        |
| `manual`              | `ADAPTER_MANUAL`           | 人工介入（不自动投递），需通过 UI 或 API 手动触发           | `manual`          |
| 兼容层（legacy）       | —                          | 旧版 `deliver_to_adapter()` 回退路径                     | 自动推断           |

### 5.4 DeliveryTicket 结构

Adapter 的 `wake()` 方法返回一个 DeliveryTicket dict：

```python
{
    "ok": True,                        # 投递是否成功
    "delivery_id": "deliv_2026...",    # 投递唯一 ID
    "turn_id": "turn_2026...",         # 关联的 turn ID
    "agent_id": "bob",                 # 目标 agent
    "adapter_type": "native_http",     # 使用的适配器类型
    "response_mode": "callback",       # 回复模式（见 §6）
    "correlation_id": "corr_2026...",  # 关联 ID
    "detail": "HTTP 200: OK",          # 投递详情（成功时）
    "sync_response": "",               # 同步回复文本（仅 sync 模式非空）
    "raw_response": "",                # 原始 HTTP 响应体（调试用）
    "error": ""                        # 错误信息（失败时）
}
```

### 5.5 Capability 能力声明

每个 adapter 通过 `capability(agent_cfg)` 声明自身能力：

```python
{
    "type": "native_http",             # 适配器类型
    "configured": True,                # 配置是否完整
    "automatic": True,                 # 是否支持自动触发（manual 为 False）
    "wake_modes": ["http_post"],       # 唤醒方式列表
    "response_modes": ["callback"],    # 支持的回复方式列表
    "supports_active_push": False,     # 是否支持主动推送
    "supports_streaming": False,       # 是否支持流式回复
    "requires_callback_url": True,     # 是否需要 callback URL
    "health": "configured"             # 健康状态
}
```

`automatic: false` 的 agent 不会被 Room Runtime 自动调度，而是进入 `manual_required` 状态。

### 5.6 wake() 语义

`wake(delivery_request)` 的核心约定：

1. **输入**：`DeliveryRequest` dict，包含 `room_id`、`agent_id`、`turn_id`、`message`、`from`、`callback_url`、`input_messages` 等
2. **输出**：`DeliveryTicket` dict
3. **同步模式**（`sync`）：`wake()` 阻塞等待 agent 回复，将回复文本填入 `sync_response`
4. **异步模式**（`callback`/`file_outbox`/`mcp_tool`）：`wake()` 立即返回 `ok=True`，agent 通过回写通道异步回复
5. **失败**：返回 `ok=False`，Room Runtime 将房间置为 `error`

---

## 6. 回写通道

Agent 收到投递后，通过以下三种方式之一将回复写回房间：

### 6.1 callback URL（HTTP 回调）

Room Runtime 在投递时为每个 agent 构造专属 callback URL：

```
POST http://127.0.0.1:7899/api/rooms/{room_id}/agents/{agent_id}/callback
Authorization: Bearer <token>
Content-Type: application/json

{
  "message": "回复内容",
  "turn_id": "turn_20260530143000123456",
  "correlation_id": "corr_20260530143000123458"
}
```

回调端点调用 `receive_agent_response()` → 写入 `active.jsonl` → 更新 `current_turn.response_message_id` → emit `agent.response.received` 事件 → 调度下一步 `run_room_step()`。

### 6.2 MCP reply_turn

Agent 进程通过 stdio 连接 MCP Server，调用 `agent_bridge.reply_turn` 工具：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "agent_bridge.reply_turn",
    "arguments": {
      "room_id": "demo_room",
      "agent_id": "bob",
      "message": "收到，我来处理。",
      "turn_id": "turn_20260530143000123456",
      "correlation_id": "corr_20260530143000123458"
    }
  }
}
```

MCP Server 内部调用与 HTTP callback **完全相同的** `receive_agent_response()` 逻辑，确保行为一致。

### 6.3 file_outbox

Agent 将回复写入共享目录下的文件 mailbox（由 `file_mailbox` adapter 定义），
Room Runtime 的文件监控器检测到新文件后读取并处理。适用于不支持 HTTP 或 MCP 的 agent。

### 6.4 回复模式总结

| response_mode    | 投递后行为                                     | 是否阻塞 |
|------------------|-----------------------------------------------|---------|
| `sync`           | `wake()` 等待并返回回复文本，立即完成 turn       | 是      |
| `callback`       | `wake()` 返回 ok，agent 通过 HTTP 回调异步回复   | 否      |
| `file_outbox`    | `wake()` 返回 ok，agent 写文件，文件监控器读取    | 否      |
| `mcp_tool`       | `wake()` 返回 ok，agent 通过 MCP reply_turn 回复 | 否      |
| `pull_session`   | `wake()` 返回 ok，agent 主动拉取（保留）          | 否      |
| `manual`         | 不投递，需人工通过 UI/API 手动写入回复             | —       |
| `none`           | 投递但无需回复（如通知类消息）                     | —       |

---

## 7. EventBus

### 7.1 概述

EventBus 是 V2 新增的可观测性层。每个房间有两个事件输出：

- **events.jsonl**：结构化事件流，每行一条 JSON
- **runtime.log**：人类可读的运行日志

### 7.2 事件类型

所有事件常量定义在 `protocol.py`：

| 事件常量                         | 含义                                 | 触发时机                                    |
|----------------------------------|--------------------------------------|---------------------------------------------|
| `room.started`                   | 房间启动                              | 房间状态变为 running 时                      |
| `room.paused`                    | 房间暂停                              | 达到 max_turns 或 timeout=pause              |
| `message.created`                | 消息创建                              | 新消息写入 active.jsonl                      |
| `turn.selected`                  | 轮次选中                              | 选择 agent 并创建 turn 后                    |
| `agent.wakeup.requested`         | 请求唤醒 agent                        | 开始向 agent 投递消息                        |
| `agent.wakeup.succeeded`         | 唤醒 agent 成功                       | adapter.wake() 返回 ok=true                  |
| `agent.wakeup.failed`            | 唤醒 agent 失败                       | adapter.wake() 返回 ok=false                 |
| `agent.response.received`        | 收到 agent 回复                       | agent 通过任一通道回复后                      |
| `turn.completed`                 | 轮次完成                              | turn 正常结束，前进到下一个 agent              |
| `turn.timeout`                   | 轮次超时                              | current_turn 超时                            |
| `turn.skipped`                   | 轮次跳过                              | 超时后执行 skip 策略                          |
| `room.error`                     | 房间错误                              | 投递失败、agent 不存在等                      |
| `archive.created`                | 归档创建                              | active.jsonl 被归档                          |

### 7.3 events.jsonl 格式

每行一条事件 JSON：

```jsonl
{"id":"evt_20260530143000123","ts":"2026-05-30 14:30:00","room":"demo_room","type":"turn.selected","actor":"bob","turn_id":"turn_20260530143000123456","correlation_id":"corr_20260530143000123458","message_id":"","meta":{"turn_index":1,"pending":2}}
{"id":"evt_20260530143000456","ts":"2026-05-30 14:30:01","room":"demo_room","type":"agent.wakeup.succeeded","actor":"bob","turn_id":"turn_20260530143000123456","correlation_id":"corr_20260530143000123458","message_id":"","meta":{"detail":"HTTP 200","elapsed":0.45}}
```

| 字段              | 类型   | 说明                                 |
|-------------------|--------|--------------------------------------|
| `id`              | string | 事件唯一 ID（`evt_...`）              |
| `ts`              | string | 时间戳                                |
| `room`            | string | 房间 ID                               |
| `type`            | string | 事件类型（上述 EVT_* 常量）             |
| `actor`           | string | 触发事件的 agent（可为空）              |
| `turn_id`         | string | 关联的 turn ID                        |
| `correlation_id`  | string | 关联 ID                               |
| `message_id`      | string | 关联的消息 ID                          |
| `meta`            | object | 扩展元数据                             |

### 7.4 runtime.log

人类可读的运行日志，格式为 JSONL：

```jsonl
{"ts":"2026-05-30 14:30:00","room":"demo_room","level":"info","event":"turn.selected","msg":"[turn.selected] actor=bob turn=turn_20260530143000123456","agent":"bob","meta":{"event_id":"evt_...","turn_id":"turn_..."}}
{"ts":"2026-05-30 14:33:00","room":"demo_room","level":"warn","event":"turn_timeout","msg":"bob 超时未回复，执行策略：skip","agent":"bob"}
```

级别：`info` / `warn` / `error`。

---

## 8. 安全

### 8.1 Token 验证

Callback endpoint 支持 Bearer Token 认证：

```yaml
# bridge.yaml
security:
  callback_token: "global-secret"       # 全局 fallback
  callback_tokens:                       # 按 agent 单独配置
    bob: "${BOB_CALLBACK_TOKEN}"
    alice: "~/.agent-bridge/tokens/alice"
```

Token 解析规则（`resolve_token()`）：

1. `${ENV_VAR}` 格式 → 从环境变量读取
2. 文件路径（`~/.agent-bridge/...`）→ 从文件读取首行
3. 其他 → 作为明文 token

验证使用 `hmac.compare_digest()` 进行常量时间比较，防止时序攻击。

Token 提取优先级：`Authorization: Bearer <token>` header > `?token=<token>` query param。

**本地模式**：如果未配置任何 token（`callback_tokens` 和 `callback_token` 均为空），
callback endpoint 允许所有请求通过（假定运行在 `127.0.0.1` 可信环境）。

### 8.2 ID 校验

所有 room_id 和 agent_id 必须匹配正则 `^[a-zA-Z0-9_-]+$`：

```python
VALID_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

def validate_room_id(room_id):
    return bool(room_id and VALID_ID_RE.match(str(room_id)))

def validate_agent_id(agent_id):
    return bool(agent_id and VALID_ID_RE.match(str(agent_id)))
```

这防止路径穿越和注入攻击。

### 8.3 Agent-Room 成员校验

`agent_in_room(config, room_id, agent_id)` 检查 agent 是否属于指定房间。
需要 agent 出现在 `room.agents` 或 `room.order` 列表中。

### 8.4 消息清理

`sanitize_message(text, max_length=50000)`：

- 必须是 string 类型，非空
- 去除首尾空白
- 长度不超过 50KB
- 移除 null 字节（`\x00`）

### 8.5 目录权限建议

```bash
chmod 700 ~/.agent-bridge/
chmod 600 ~/.agent-bridge/rooms/*/state.json
chmod 600 ~/.agent-bridge/bridge.yaml
```

---

## 9. MCP Server

### 9.1 协议

MCP Server 实现 JSON-RPC 2.0 over stdin/stdout。Agent 进程通过 stdio 连接，
调用 MCP tools 实现与 Agent Bridge 的交互。

```
python3 core/mcp_server.py --shared-dir ~/.agent-bridge
# 或通过环境变量
AGENT_BRIDGE_SHARED_DIR=~/.agent-bridge python3 core/mcp_server.py
```

### 9.2 工具列表

所有工具名称前缀为 `agent_bridge.`：

#### agent_bridge.list_rooms

列出所有配置的房间及其状态。

```
参数：无
```

响应示例：

```json
{
  "rooms": [
    {
      "id": "demo_room",
      "name": "demo_room",
      "status": "running",
      "agents": ["alice", "bob"],
      "turn_count": 5,
      "max_turns": 50,
      "policy": "round_robin",
      "current_turn": { ... }
    }
  ],
  "count": 1
}
```

#### agent_bridge.get_current_turn

获取指定房间的当前轮次信息。

```
参数：
  room_id (string, required): 房间 ID
```

返回 `state.json` 中的 `current_turn` 及相关联的状态摘要。

#### agent_bridge.read_messages

读取房间的消息记录。

```
参数：
  room_id (string, required): 房间 ID
  limit  (integer, optional): 返回消息数量上限，默认 100
  after  (string, optional):  只返回该时间之后的消息
```

#### agent_bridge.get_agent_pending

检查指定 Agent 是否有待处理的轮次。

```
参数：
  room_id  (string, required): 房间 ID
  agent_id (string, required): Agent ID
```

返回 `has_pending_turn`（bool）和 `current_turn`（若 pending）。

#### agent_bridge.reply_turn

**核心工具**：回复当前轮次。内部调用与 HTTP callback 完全相同的 `receive_agent_response()`。

```
参数：
  room_id        (string, required): 房间 ID
  agent_id       (string, required): Agent ID
  message        (string, required): 回复消息文本
  turn_id        (string, optional): 轮次 ID（用于校验）
  correlation_id (string, optional): 关联 ID（用于校验）
```

#### agent_bridge.send_message

以 Agent 身份向房间发送一条新消息（不关联任何 turn）。

```
参数：
  room_id  (string, required): 房间 ID
  agent_id (string, required): Agent ID（发送者）
  message  (string, required): 消息文本
```

### 9.3 JSON-RPC 错误码

| 错误码     | 含义              |
|-----------|-------------------|
| -32700    | JSON 解析错误      |
| -32600    | 无效请求           |
| -32601    | 方法未找到         |
| -32602    | 无效参数           |
| -32603    | 内部错误           |
| -32001    | 房间未找到         |
| -32002    | Agent 未找到       |
| -32003    | 无活动轮次         |
| -32004    | 轮次不匹配         |
| -32005    | 配置未找到         |
| -32006    | 参数校验失败       |

---

## 10. 超时策略

### 10.1 超时检测

Room Runtime 在每次 `run_room_step()` 时检查 `current_turn.timeout_at`。
若当前时间超过 `timeout_at`，触发超时处理。

默认超时时间：**180 秒**（可在 adapter 配置中覆盖）。

### 10.2 五种策略

由房间配置 `policy.on_timeout` 决定：

```yaml
rooms:
  demo_room:
    policy:
      on_timeout: skip   # skip | retry | pause | error | manual
```

| 策略     | 行为                                                                           |
|---------|--------------------------------------------------------------------------------|
| `skip`  | 跳过当前 agent，前进到下一个。Turn 状态变为 `skipped`。emit `turn.skipped`。      |
| `retry` | 重试投递（最多 `max_attempts` 次，默认 2 次）。重置 timeout 计时器并重新调用 `wake()`。超过重试次数后降级为 `skip`。 |
| `pause` | 暂停房间，状态变为 `paused`。需手动恢复。emit `room.paused`。                     |
| `error` | 房间进入错误状态，状态变为 `error`。需人工介入。emit `room.error`。                |
| `manual`| Turn 状态变为 `manual_required`，等待人工介入（通过 UI/API 写入回复）。             |

### 10.3 重试机制

当 `on_timeout=retry` 时：

1. 检查 `current_turn.attempts < max_attempts`
2. 若可重试：`attempts += 1`，重置 `timeout_at`，重新调用 `adapter.wake()`
3. 若已达上限：降级为 `skip`

---

## 附录 A：完整 bridge.yaml 配置示例

```yaml
shared_dir: "~/.agent-bridge"

server:
  host: "127.0.0.1"
  port: 7899

security:
  callback_token: "my-global-secret"
  # callback_tokens:   # 按 agent 单独配置（优先级更高）
  #   bob: "${BOB_TOKEN}"

agents:
  alice:
    name: "Alice (OpenClaw)"
    adapter:
      type: openclaw_sessions
      wakeup:
        url: "http://127.0.0.1:18789/tools/invoke"
        auth:
          type: bearer
          token_env: "OPENCLAW_TOKEN"
        body_template:
          tool: "sessions_send"
          args:
            sessionKey: "agent:main:main"
            message: "{{message}}"
      response:
        mode: callback
        timeout_seconds: 300

  bob:
    name: "Bob (CLI)"
    adapter:
      type: cli
      command: "hermes-cli chat --message {{message}}"
      response:
        mode: sync
        timeout_seconds: 120

  charlie:
    name: "Charlie (Manual)"
    adapter:
      type: manual

rooms:
  demo_room:
    name: "Demo Room"
    status: running    # running | paused
    order:
      - alice
      - bob
    max_turns: 100
    policy:
      on_timeout: skip
```

## 附录 B：V1 兼容性说明

V2 完全向下兼容 V1 的消息格式。旧版消息仅有 `ts`、`from`、`msg` 三个字段，
V2 运行时通过 `normalize_message()` 和 `migrate_room_state()` 自动补全缺失字段。

旧版共享目录（`~/.agent-bridge/active.jsonl` 在根目录）通过单房间模式迁移到
`~/.agent-bridge/rooms/default/` 结构。旧版 `bridge.yaml` 中的 `wakeup` 配置
被 `adapter.native_http` 兼容层自动识别。
