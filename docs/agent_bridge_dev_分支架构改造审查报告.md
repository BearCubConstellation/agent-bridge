# Agent Bridge dev 分支架构改造审查报告

> 审查对象：`https://github.com/SusuAgent/agent-bridge.git` 的 `dev` 分支  
> 审查目标：检查当前 dev 分支是否符合上一版《Agent Bridge 架构改造方案》  
> 审查结论：当前 dev 分支**没有完成架构改造**。目前变更非常有限，主要只是给 `core/rooms.py` 中的 adapter 响应体增加了日志记录，尚未落地事件驱动、双向 callback、Adapter V2、Room Runtime 状态机、MCP Server、Skill 等核心设计。

---

## 1. 总体结论

当前 dev 分支不能认为已经完成《Agent Bridge 架构改造方案》。它最多算是做了一个局部调试增强：

```text
在 tick_room 调用 adapter 后，把 response_body 的长度和 preview 写入 runtime.log。
```

这个改动对排查 OpenClaw / HTTP Adapter 返回体有帮助，但它没有解决核心问题：

```text
Agent Bridge 仍然是：
定时轮询 + 单向投递 + waiting_for 轮询检测 + 没有标准回写路径。
```

换句话说，当前 dev 分支仍然会遇到原始问题：

```text
OpenClaw sessions_send 返回 200 后，如果 OpenClaw 没有主动写入 active.jsonl，room 仍然会进入 waiting_response 并继续死等。
```

---

## 2. 当前 dev 分支实际改动概览

根据 main 与 dev 对比：

```text
base: main
head: dev
状态: dev ahead main by 1 commit
修改文件: core/rooms.py
变更规模: additions 9 / deletions 0
```

也就是说：

```text
没有新增 core/events.py
没有新增 core/runtime.py
没有新增 core/scheduler.py
没有新增 core/mcp_server.py
没有新增 Adapter 子模块目录
没有新增 Agent Skill
没有新增 callback API
没有新增 events API
没有新增 turn API
没有新增 schedule API
```

当前唯一有效改动位于 `core/rooms.py` 的 `tick_room()` 中：

```python
# Log response body for debugging (truncated)
if response_body and response_body.strip():
    _log_tick(shared_dir, room_id, "response_body", ...)
else:
    _log_tick(shared_dir, room_id, "response_body", ...)
```

这属于日志增强，不属于架构改造。

---

## 3. 与原方案的符合度评估

| 模块 / 目标 | 原方案要求 | dev 分支现状 | 符合度 |
|---|---|---|---|
| 双向 callback 回写 | 新增 `/api/rooms/{room_id}/agents/{agent_id}/callback` | 未实现 | ❌ 不符合 |
| EventBus | 新增 `core/events.py`，写入 `events.jsonl` | 未实现 | ❌ 不符合 |
| Scheduler | 新增 `core/scheduler.py`，事件触发调度 | 未实现 | ❌ 不符合 |
| Room Runtime 状态机 | 新增 `core/runtime.py`，使用 `current_turn` | 未实现 | ❌ 不符合 |
| Adapter V2 | DeliveryRequest / DeliveryTicket / response_mode | 未实现 | ❌ 不符合 |
| OpenClaw 专用 adapter | `openclaw_sessions`，区分唤醒成功与回复成功 | 未实现 | ❌ 不符合 |
| File outbox | 支持 `file_outbox` 回复模式 | 未实现 | ❌ 不符合 |
| MCP Server | 暴露 `reply_turn` 等 MCP tools | 未实现 | ❌ 不符合 |
| Agent Skill | 新增 Agent Bridge Room Participant Skill | 未实现 | ❌ 不符合 |
| 事件驱动替代纯轮询 | 用户发消息 / callback 后立即 schedule | 未实现 | ❌ 不符合 |
| Timeout / Retry 策略 | turn 级 timeout、retry、skip、manual | 未实现 | ❌ 不符合 |
| UI 可观察性 | 展示 current turn、callback_url、timeout 等 | 未实现 | ❌ 不符合 |
| 安全校验 | callback token、turn_id、correlation_id 校验 | 未实现 | ❌ 不符合 |
| 日志增强 | 记录 adapter response_body | 已实现一小部分 | ✅ 局部符合 |

总体评价：

```text
符合度很低。
当前 dev 分支仅完成了“调试日志增强”，没有完成架构层设计。
```

---

## 4. 关键不满意点

### 4.1 没有解决 waiting_response 死等问题

当前 `tick_room()` 仍然使用旧逻辑：

```text
1. deliver_to_adapter 投递消息
2. 尝试从 response_body 提取同步回复
3. 如果没有同步回复，则：
   state["waiting_for"] = agent_id
   state["waiting_line"] = len(messages)
4. 下次 tick 再扫描 active.jsonl 中是否出现该 agent 的回复
```

问题仍然存在：

```text
如果 Agent 没有能力或没有被明确要求写回 active.jsonl，系统仍然会一直等待。
```

对 OpenClaw 来说尤其明显：

```text
sessions_send 的 HTTP 200 只能说明消息已送入 OpenClaw，不能说明 OpenClaw 已生成最终回复。
```

当前 dev 分支只是记录了 `response_body`，但没有建立任何“Agent 回复回写机制”。

必须补：

```text
POST /api/rooms/{room_id}/agents/{agent_id}/callback
```

并且在投递给 Agent 时注入：

```text
room_id
tyn_id / turn_id
correlation_id
callback_url
```

注意：这里应统一使用 `turn_id`，不要出现 `tyn_id` 这类拼写错误。

---

### 4.2 没有 turn_id / correlation_id

原方案要求每次 Agent 被唤醒时生成：

```text
turn_id
correlation_id
delivery_id
```

这些 ID 是解决“哪条回复属于哪次投递”的关键。

当前 dev 分支仍然只依赖：

```text
waiting_for
waiting_line
```

这会导致以下问题：

1. 无法精确关联回复与投递。
2. Agent 主动发言可能被误判成当前 turn 的回复。
3. 如果某个 Agent 延迟回复，可能污染后续轮次。
4. 无法安全校验 callback。
5. 无法实现 MCP `reply_turn`。

必须补：

```json
"current_turn": {
  "turn_id": "turn_xxx",
  "agent_id": "openclaw",
  "state": "waiting_response",
  "delivery_id": "deliv_xxx",
  "correlation_id": "corr_xxx",
  "started_at": "...",
  "timeout_at": "...",
  "input_message_ids": [],
  "input_line_max": 12,
  "response_message_id": "",
  "attempts": 1,
  "last_error": ""
}
```

---

### 4.3 Adapter 仍然是旧版三元组返回

当前 `deliver_to_adapter()` 仍然返回：

```python
(success, detail, response_body)
```

这不足以表达真实 Agent 通信模式。

必须改为 DeliveryTicket：

```json
{
  "ok": true,
  "delivery_id": "deliv_xxx",
  "turn_id": "turn_xxx",
  "agent_id": "openclaw",
  "adapter_type": "openclaw_sessions",
  "response_mode": "callback",
  "correlation_id": "corr_xxx",
  "detail": "HTTP 200",
  "sync_response": "",
  "raw_response": "...",
  "error": ""
}
```

当前实现无法区分：

```text
投递成功
同步回复成功
等待 callback
等待 file_outbox
等待 MCP tool reply_turn
需要手动回复
无需回复
```

这会导致 runtime 只能继续猜。

---

### 4.4 没有 response_mode

原方案要求 adapter 必须声明：

```text
sync
callback
file_outbox
pull_session
mcp_tool
manual
none
```

当前 dev 分支仍然没有 `response.mode` 概念。

这会导致 OpenClaw 这种异步 Agent 被当成普通 HTTP webhook 处理，而 HTTP 200 被误认为“也许可以从 response_body 里提取最终回复”。

必须补：

```yaml
adapter:
  type: openclaw_sessions
  wakeup:
    url: http://127.0.0.1:18789/tools/invoke
    tool: sessions_send
    sessionKey: agent:main:main
  response:
    mode: callback
    timeout_seconds: 180
```

---

### 4.5 没有 EventBus

原方案要求新增：

```text
rooms/{room_id}/events.jsonl
```

并通过 `emit_event()` 记录：

```text
message.created
agent.wakeup.requested
agent.wakeup.succeeded
agent.response.received
turn.completed
turn.timeout
room.error
```

当前 dev 分支没有 `events.jsonl`，也没有 `core/events.py`。

当前只有 `runtime.log`，它是人类可读日志，不适合作为系统事件源。

必须补：

```python
def emit_event(shared_dir, room_id, type, actor="", turn_id="", correlation_id="", message_id="", meta=None):
    ...
```

---

### 4.6 没有 Scheduler，仍然依赖 PollManager

当前 `PollManager` 仍然是：

```text
while not stop:
    _do_poll()
    wait(interval)
```

并且默认间隔仍然是 180 秒。

原方案要求：

```text
事件触发为主，轮询为兜底。
```

当前 dev 分支没有：

```text
schedule_room(room_id)
内存队列
worker loop
callback 后立即 schedule
用户 send 后立即 schedule
room start 后立即 schedule
```

必须补：

```python
def schedule_room(room_id):
    ...

class Scheduler:
    def enqueue(room_id): ...
    def worker_loop(): ...
```

---

### 4.7 server.py 仍然直接承载调度入口

当前 `ui/server.py` 仍然直接导入并调用：

```python
tick_room
tick_running_rooms
run_poll
```

这说明调度逻辑仍然没有从 UI Server 中解耦。

原方案要求：

```text
server.py 只做 API / UI。
runtime.py 负责状态机。
scheduler.py 负责触发。
events.py 负责事件。
adapters/* 负责通信。
```

当前未完成。

---

### 4.8 没有 callback API

当前 room API 解析逻辑只支持类似：

```text
/api/rooms/{room_id}/messages
/api/rooms/{room_id}/logs
/api/rooms/{room_id}/send
/api/rooms/{room_id}/start
/api/rooms/{room_id}/pause
/api/rooms/{room_id}/tick
```

缺少：

```text
POST /api/rooms/{room_id}/agents/{agent_id}/callback
POST /api/rooms/{room_id}/agents/{agent_id}/message
GET  /api/rooms/{room_id}/events
GET  /api/rooms/{room_id}/turn
POST /api/rooms/{room_id}/schedule
```

这是当前最严重缺口之一。

---

### 4.9 用户 send 后不会立即触发调度

当前 `handle_room_action -> send` 做的是：

```text
append_room_message
return ok
```

它没有：

```text
emit message.created
schedule_room(room_id)
```

因此用户发消息后仍然要等后台 poll。

必须改为：

```text
append_room_message
emit_event("message.created")
schedule_room(room_id)
return ok
```

---

### 4.10 room start 后不会立即触发调度

当前 `start` 逻辑做的是：

```text
set_room_status running
append runtime log
return ok
```

它没有：

```text
emit room.started
schedule_room(room_id)
```

结果是 room 启动后仍然依赖下一次 poll。

必须改为：

```text
set_room_status running
emit_event("room.started")
schedule_room(room_id)
```

---

### 4.11 没有 MCP Server

原方案要求新增 MCP Server，至少提供：

```text
agent_bridge.list_rooms
agent_bridge.get_current_turn
agent_bridge.read_messages
agent_bridge.get_agent_pending
agent_bridge.reply_turn
agent_bridge.send_message
```

当前 dev 分支没有 `core/mcp_server.py`，也没有任何 MCP 工具入口。

这意味着支持 MCP 的 Agent 仍然没有标准工具通道写回聊天室。

必须补。

---

### 4.12 没有 Agent Skill

原方案要求新增：

```text
core/skills/agent-bridge-room-participant/SKILL.md
```

用于告诉 Agent：

```text
你收到消息后不能只在本地会话里回答。
你必须通过 MCP reply_turn 或 HTTP callback 写回 Agent Bridge。
```

当前 dev 分支没有该 Skill。

必须补。

---

### 4.13 OpenClaw 仍然只是 native_http

当前 OpenClaw 自动发现仍然生成普通 wakeup 配置：

```yaml
url: http://127.0.0.1:18789/tools/invoke
method: POST
body_template:
  tool: sessions_send
  args:
    sessionKey: agent:main:main
    message: "{{message}}"
```

问题是：

```text
这只是把消息送进 OpenClaw session。
它没有 callback_url。
它没有 turn_id。
它没有 correlation_id。
它没有要求 OpenClaw 回写。
它没有区分 response.mode。
```

必须新增 `openclaw_sessions` adapter，或至少将 OpenClaw 的 native_http 模板升级为包含 callback 指令。

推荐模板：

```yaml
message_template: |
  [Agent Bridge]
  room_id={{room}}
  agent_id={{to}}
  turn_id={{turn_id}}
  correlation_id={{correlation_id}}
  callback_url={{callback_url}}

  你必须在生成最终回复后写回 Agent Bridge。
  优先调用 MCP tool: agent_bridge.reply_turn。
  如果无法调用 MCP，请 POST 到 callback_url。

  当前待处理消息：
  {{message}}
```

---

### 4.14 logging response_body 有泄露风险

dev 分支新增的 response_body 日志虽然有调试价值，但也有潜在问题：

```text
1. response_body 可能包含 token、认证错误详情、内部路径、模型输出隐私内容。
2. 当前 preview 截取 300 字符，但没有脱敏。
3. runtime.log 是本地文件，可能被误发、归档或同步。
```

建议保留该日志，但必须做成可配置：

```yaml
debug:
  log_adapter_response_body: false
  response_body_preview_chars: 300
```

默认应关闭或至少做脱敏。

脱敏规则建议：

```text
Authorization: Bearer xxx -> Authorization: Bearer [REDACTED]
token/password/secret/api_key -> [REDACTED]
```

---

## 5. 当前 dev 分支可以保留的部分

虽然整体改造未完成，但以下局部可以保留：

### 5.1 response_body 日志

保留价值：

```text
可以帮助确认 OpenClaw / HTTP Adapter 到底有没有同步返回正文。
```

但必须调整：

```text
1. 默认关闭或受 debug 配置控制。
2. 加脱敏。
3. 不要把它当成架构改造完成的依据。
```

### 5.2 现有 room 基础能力

当前已有这些基础能力，可以继续复用：

```text
append_room_message
read_room_messages
read_room_state
write_room_state
append_room_log
tick_room 基础 round-robin 思路
```

但应逐步把 `tick_room()` 拆到 `runtime.py`，避免 `rooms.py` 继续膨胀。

---

## 6. 必须补做的任务清单

下面是给开发 Agent 的下一步执行清单。请按顺序执行，不要继续只加日志。

---

### 阶段 1：最小闭环，先解决死等

#### 任务 1.1 新增 callback API

实现：

```text
POST /api/rooms/{room_id}/agents/{agent_id}/callback
```

请求体：

```json
{
  "turn_id": "turn_xxx",
  "correlation_id": "corr_xxx",
  "message": "最终回复",
  "meta": {}
}
```

处理逻辑：

```text
1. 校验 room_id
2. 校验 agent_id
3. 校验 agent_id 属于 room
4. 校验 current_turn.agent_id == agent_id
5. 校验 turn_id / correlation_id
6. append_room_message
7. 清除 waiting 状态 / 标记 response_message_id
8. 立即 tick 或 schedule
```

#### 任务 1.2 生成 turn_id / correlation_id

在每次唤醒 Agent 前生成：

```text
turn_id
correlation_id
delivery_id
```

并写入 state.current_turn。

#### 任务 1.3 投递消息时注入 callback_url

投递给 Agent 的 context 必须包含：

```text
room
room_id
to
agent_id
turn_id
correlation_id
callback_url
```

#### 任务 1.4 OpenClaw 模板必须要求回写

OpenClaw 的 message_template 必须明确写：

```text
不能只在 OpenClaw 本地 session 中回答。
必须调用 MCP reply_turn 或 POST callback_url。
```

#### 阶段 1 验收

```text
模拟 callback 后，waiting_response 必须解除，并推进到下一位 Agent。
```

---

### 阶段 2：Adapter V2

#### 任务 2.1 定义 DeliveryRequest / DeliveryTicket

新增数据结构或 dict schema。

#### 任务 2.2 支持 response.mode

至少支持：

```text
sync
callback
file_outbox
mcp_tool
manual
none
```

#### 任务 2.3 改造 deliver_to_adapter

从：

```python
(success, detail, response_body)
```

改为：

```python
DeliveryTicket
```

#### 阶段 2 验收

```text
native_http 可以声明 callback。
cli 可以声明 sync。
manual 不会直接导致 room error，而是进入 manual_required。
```

---

### 阶段 3：EventBus + Scheduler

#### 任务 3.1 新增 core/events.py

实现：

```python
emit_event(...)
read_events(...)
```

写入：

```text
rooms/{room_id}/events.jsonl
```

#### 任务 3.2 新增 core/scheduler.py

实现：

```python
schedule_room(room_id)
run_worker_loop()
```

#### 任务 3.3 改造 send/start/callback

这些动作必须：

```text
emit_event
schedule_room
```

#### 阶段 3 验收

```text
用户发消息后不需要等待 PollManager，room 会立即被调度。
```

---

### 阶段 4：Room Runtime 状态机

#### 任务 4.1 新增 core/runtime.py

把 `tick_room()` 中核心调度逻辑迁移到 runtime。

#### 任务 4.2 引入 current_turn

state.json 必须支持：

```json
{
  "current_turn": {
    "turn_id": "turn_xxx",
    "agent_id": "openclaw",
    "state": "waiting_response",
    "correlation_id": "corr_xxx"
  }
}
```

#### 任务 4.3 timeout / retry / skip

实现：

```text
on_timeout: skip / retry / pause / error / manual
on_delivery_failed: retry / pause / error
```

#### 阶段 4 验收

```text
Agent 不回复时，系统不会无限死等。
```

---

### 阶段 5：MCP Server

新增：

```text
core/mcp_server.py
```

至少实现 tools：

```text
agent_bridge.list_rooms
agent_bridge.get_current_turn
agent_bridge.read_messages
agent_bridge.get_agent_pending
agent_bridge.reply_turn
agent_bridge.send_message
```

核心工具是：

```text
agent_bridge.reply_turn
```

它必须走与 callback 相同的内部逻辑。

---

### 阶段 6：Agent Skill

新增：

```text
core/skills/agent-bridge-room-participant/SKILL.md
```

内容必须明确：

```text
1. Agent 是 room 成员。
2. 收到消息后必须写回 Agent Bridge。
3. 优先使用 MCP reply_turn。
4. 不支持 MCP 时 POST callback_url。
5. 禁止伪造 agent_id。
6. 禁止直接修改 active.jsonl。
```

---

## 7. 建议给开发 Agent 的整改指令

可以直接把下面这段发给开发 Agent：

```text
你当前对 Agent Bridge dev 分支的改造没有完成架构升级，只在 core/rooms.py 里增加了 response_body 调试日志。请不要继续只做日志层修补。

请按照以下顺序继续改造：

1. 实现 POST /api/rooms/{room_id}/agents/{agent_id}/callback。
2. 每次唤醒 Agent 前生成 turn_id、correlation_id、delivery_id，并写入 state.current_turn。
3. 投递给 Agent 的 context 和模板中必须包含 room_id、agent_id、turn_id、correlation_id、callback_url。
4. callback 收到 Agent 回复后，必须校验 room_id、agent_id、turn_id、correlation_id，然后 append_room_message，并立即推进 room。
5. 不要再只依赖 waiting_for/waiting_line。它们可以暂时保留兼容，但新逻辑必须以 current_turn 为准。
6. 将 deliver_to_adapter 从三元组返回改为 DeliveryTicket，并支持 response.mode。
7. 新增 core/events.py，写入 rooms/{room_id}/events.jsonl。
8. 新增 core/scheduler.py，实现 schedule_room(room_id)，让 send/start/callback 后立即调度。
9. 新增 core/runtime.py，把 tick_room 状态机迁移出去。
10. 新增 core/mcp_server.py，至少提供 reply_turn/send_message/read_messages/get_current_turn。
11. 新增 core/skills/agent-bridge-room-participant/SKILL.md，明确 Agent 必须通过 MCP 或 callback 写回。
12. response_body 日志可以保留，但必须加 debug 开关和敏感信息脱敏。

验收标准：OpenClaw sessions_send 返回 200 后，不应被认为已经完成回复；只有 callback、MCP reply_turn、file_outbox 或 sync_response 才能完成 turn。Agent 不回复时必须 timeout，并按 room policy 处理，不能无限 waiting_response。
```

---

## 8. 最终评价

当前 dev 分支的方向不算错，但完成度严重不足。

它做了：

```text
✅ adapter response_body 日志记录
```

它没有做：

```text
❌ 双向 callback
❌ turn_id / correlation_id
❌ DeliveryTicket
❌ response_mode
❌ EventBus
❌ Scheduler
❌ Runtime 状态机
❌ MCP Server
❌ Agent Skill
❌ OpenClaw 专用异步回写方案
❌ timeout / retry / skip
```

所以当前版本不能合并为“架构改造完成版”。

推荐结论：

```text
拒绝作为完整架构改造合并。
可以保留 response_body 调试日志，但必须继续按阶段完成核心改造。
```

