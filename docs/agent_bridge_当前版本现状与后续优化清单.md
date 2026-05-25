# Agent Bridge 当前版本现状与后续优化清单

> 文档用途：本文档用于总结当前 `dev` 分支的架构改造现状、已完成能力、可联调链路、待优化问题、待实现功能和后续开发优先级。  
> 适用对象：Agent Bridge 项目开发 Agent、维护者、后续接手开发者。  
> 当前分支：`dev`  
> 当前定位：Agent Bridge 已从“文件轮询 + 单向投递”的早期形态，升级为“事件驱动 + Runtime V2 + Adapter V2 + Callback/MCP 回写 + 产品化 UI 联调”的轻量级多 Agent 聊天室系统。

---

## 1. 当前版本总体结论

当前 `dev` 分支已经基本完成了核心架构改造，具备以下关键能力：

```text
1. Adapter V2 抽象已经建立。
2. Room Runtime V2 状态机已经建立。
3. EventBus / events.jsonl 已经建立。
4. Scheduler 已经建立并成为 V2 主驱动。
5. Callback 回写链路已经建立。
6. MCP Server 原型已经建立。
7. Agent Skill 文档已经建立。
8. OpenClaw 专用 adapter 已经建立。
9. UI 已经支持普通用户友好的 Agent 配置与联调流程。
10. 自动测试聊天室、测试房间标识和清理入口已经建立。
```

当前版本已经不再是单纯的“日志增强”或“局部修补”，而是完成了一轮比较系统的架构升级。

但是，当前版本仍然处于：

```text
可实机联调 / 合并前最后验证 / 产品体验继续打磨
```

还不建议完全视为最终稳定版。后续仍需要重点完成：

```text
1. OpenClaw 真机 callback / MCP reply_turn 链路验证。
2. MCP Server 与真实 MCP Client 的兼容性验证。
3. 测试房间删除的后端原子化能力。
4. Adapter V2 的更多 Agent 类型实测。
5. UI 联调状态展示继续增强。
6. 文档和安装流程补齐 dev → main 的发布准备。
```

---

## 2. 当前系统架构概览

当前 Agent Bridge 的核心架构可以理解为：

```text
┌──────────────────────────────────────────────┐
│                 Web UI / CLI                  │
│  - Agent 配置                                │
│  - Room 管理                                 │
│  - 测试连接                                  │
│  - 发送测试消息 / 自动创建测试聊天室          │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│                UI Server / API                │
│  - /api/config/full                           │
│  - /api/agent/test                            │
│  - /api/agent/integration-test                │
│  - /api/rooms/{room}/send                     │
│  - /api/rooms/{room}/agents/{agent}/callback  │
│  - /api/rooms/{room}/events                   │
│  - /api/rooms/{room}/turn                     │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│                 EventBus                      │
│  - events.jsonl                               │
│  - message.created                            │
│  - agent.wakeup.*                             │
│  - agent.response.received                    │
│  - turn.completed / turn.timeout              │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│                Scheduler                      │
│  - schedule_room(room_id)                     │
│  - scan_running_rooms                         │
│  - worker loop                                │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│              Room Runtime V2                  │
│  - current_turn                               │
│  - turn_id / correlation_id                   │
│  - waiting_response                           │
│  - timeout / retry / skip                     │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│              Adapter V2 Layer                 │
│  - openclaw_sessions                          │
│  - native_http                                │
│  - cli                                        │
│  - file_mailbox                               │
│  - mcp_tool                                   │
│  - manual                                     │
└──────────────────────────────────────────────┘
```

旁路能力：

```text
MCP Server
  - agent_bridge.reply_turn
  - agent_bridge.send_message
  - agent_bridge.read_messages
  - agent_bridge.get_current_turn

Agent Skill
  - agent-bridge-room-participant/SKILL.md
  - 告诉 Agent 如何参与 room、如何 callback/MCP 写回
```

---

## 3. 当前已完成能力

### 3.1 Adapter V2 已完成基础抽象

当前版本已经将原来的 `core/adapters.py` 拆分为 `core/adapters/` 目录，并新增多个 adapter 实现。

当前已有 adapter 类型：

```text
manual
native_http
openclaw_sessions
cli
file_mailbox
mcp_tool
```

当前 Adapter V2 的核心目标已经达成：

```text
1. 不再只靠旧 wakeup 三元组。
2. 能区分不同 Agent 的唤醒方式和回复模式。
3. 支持 DeliveryRequest / DeliveryTicket 思路。
4. 支持 response.mode。
5. 支持 capability 声明。
6. 兼容 legacy wakeup。
```

当前重点 adapter：

```text
openclaw_sessions
```

它用于将消息投递到 OpenClaw Gateway 的 `tools/invoke -> sessions_send`，并在 `args.message` 正文中注入：

```text
room_id
agent_id
turn_id
correlation_id
callback_url
MCP reply_turn 指令
HTTP callback 指令
```

这解决了之前 OpenClaw 只收到普通消息，但不知道如何回写 Agent Bridge 的问题。

---

### 3.2 Room Runtime V2 已建立

当前版本已经引入 Runtime V2 状态机，核心状态由 `state.json` 中的 `current_turn` 表达。

典型结构：

```json
{
  "status": "running",
  "turn_index": 0,
  "current_turn": {
    "turn_id": "turn_xxx",
    "agent_id": "openclaw",
    "state": "waiting_response",
    "delivery_id": "deliv_xxx",
    "correlation_id": "corr_xxx",
    "started_at": "2026-xx-xx xx:xx:xx",
    "timeout_at": "2026-xx-xx xx:xx:xx",
    "response_message_id": ""
  }
}
```

Runtime V2 当前已经支持：

```text
1. 选择当前应发言 Agent。
2. 读取 pending messages。
3. 调用 Adapter V2 唤醒 Agent。
4. 区分 sync_response 与 async waiting。
5. 写入 current_turn。
6. 等待 callback / MCP / file_outbox 回写。
7. 收到回复后推进 turn。
8. timeout 检测。
9. legacy waiting_for/waiting_line 兼容。
```

---

### 3.3 EventBus 已建立

当前版本新增 `events.jsonl` 作为系统事件日志。

每个 room 目录下可包含：

```text
rooms/{room_id}/events.jsonl
```

典型事件包括：

```text
message.created
agent.wakeup.requested
agent.wakeup.succeeded
agent.wakeup.failed
agent.response.received
turn.completed
turn.timeout
room.started
room.paused
room.error
```

这使系统不再只依赖 `runtime.log` 这种人类可读日志，也不再只依赖 `active.jsonl` 作为隐式状态来源。

---

### 3.4 Scheduler 已成为 V2 主驱动

当前版本已经补齐 Scheduler：

```text
core/scheduler.py
```

并完成关键修正：

```text
1. 服务启动时启动 Scheduler。
2. Scheduler 会 set_config。
3. _schedule_room 每次都会刷新 config。
4. PollManager 在 V2 模式下不再调用旧 tick_running_rooms。
5. PollManager 降级为 fallback scan。
```

这解决了之前“旧 tick_room 和新 Runtime V2 双驱动 room”的风险。

---

### 3.5 Callback 回写链路已建立

当前版本已经新增 Agent 回写入口：

```text
POST /api/rooms/{room_id}/agents/{agent_id}/callback
```

标准请求体：

```json
{
  "turn_id": "turn_xxx",
  "correlation_id": "corr_xxx",
  "message": "Agent 的最终回复",
  "meta": {}
}
```

当前 callback 链路应具备：

```text
1. 校验 room_id。
2. 校验 agent_id。
3. 校验当前 current_turn。
4. 校验 turn_id / correlation_id。
5. append_room_message。
6. 写入 reply_to / correlation_id。
7. emit agent.response.received。
8. schedule 下一轮。
```

这已经解决了最初的核心问题：

```text
Agent 被唤醒后没有统一回写路径，导致 waiting_response 死等。
```

---

### 3.6 MCP Server 原型已建立

当前版本已经新增：

```text
core/mcp_server.py
```

已设计的 MCP 工具包括：

```text
agent_bridge.list_rooms
agent_bridge.get_current_turn
agent_bridge.read_messages
agent_bridge.get_agent_pending
agent_bridge.reply_turn
agent_bridge.send_message
```

其中最关键的是：

```text
agent_bridge.reply_turn
```

它应与 HTTP callback 走同一套内部回写逻辑。

注意：当前 MCP Server 更接近“自实现 stdio JSON-RPC MCP-like server”，后续仍需要使用真实 MCP client 做兼容性验证。

---

### 3.7 Agent Skill 已建立

当前版本已经新增：

```text
core/skills/agent-bridge-room-participant/SKILL.md
```

Skill 的作用是告诉参与 Agent：

```text
1. 你是 Agent Bridge room 的一个成员。
2. 你收到消息后不能只在本地会话回答。
3. 你必须通过 MCP reply_turn 或 HTTP callback 写回 Agent Bridge。
4. 你必须携带 turn_id 和 correlation_id。
5. 你不能伪造其他 Agent 身份。
6. 你不能直接修改 active.jsonl。
```

这个 Skill 是让 OpenClaw、Claude Code、Hermes 等 Agent 正确参与聊天室的重要行为约束。

---

### 3.8 UI 已产品化改造

当前 UI 已经从“开发者调试配置面板”进化为更适合普通用户操作的产品界面。

当前 UI 已完成：

```text
1. Agent 简洁配置卡片。
2. 高级设置。
3. 开发者配置。
4. OpenClaw 专属配置卡片。
5. 显示名称。
6. 唯一标识。
7. Agent 类型。
8. 连接状态。
9. OpenClaw 会话。
10. 测试连接。
11. 发送测试消息。
12. 保存后的下一步提示。
13. 配置摘要。
```

OpenClaw 用户现在理论上只需要关注：

```text
显示名称
唯一标识
Agent 类型：OpenClaw
OpenClaw 会话：agent:main:main
测试连接
发送测试消息
```

复杂字段被折叠到了：

```text
高级设置
开发者配置
```

---

### 3.9 测试连接与完整联调已区分

当前 UI 已区分两个动作：

```text
测试连接
发送测试消息
```

区别：

```text
测试连接：
  只验证 OpenClaw Gateway / Webhook 地址是否可达。
  成功不代表 callback 已完成。

发送测试消息：
  走 Runtime V2。
  写入一条测试消息。
  唤醒目标 Agent。
  等待 callback / sync_response。
```

这避免了用户误以为“连接可达 = 完整联调成功”。

---

### 3.10 自动测试聊天室已建立

当前版本已经支持：

```text
如果 Agent 没有加入任何 room，点击“发送测试消息”时，UI 会提示是否自动创建临时测试聊天室。
```

用户确认后，后端会创建：

```text
test_{agent_id}_{random}
```

测试 room 会被设置为：

```json
{
  "agents": ["agent_id"],
  "order": ["agent_id"],
  "policy": "round_robin",
  "status": "running",
  "meta": {
    "temporary": true
  }
}
```

UI 中该 room 会显示：

```text
测试房间
```

并提供：

```text
清理
```

清理时会先暂停运行中的测试房间，再删除配置。

---

## 4. 当前推荐的真实联调流程

### 4.1 安装 / 启动

```bash
git checkout dev
git pull
bridge start
```

或者开发模式：

```bash
python cli/bridge start
```

打开：

```text
http://127.0.0.1:7899
```

---

### 4.2 OpenClaw 联调流程

```text
1. 打开 Agent 页面。
2. 点击扫描本机 Agent。
3. 找到 OpenClaw。
4. 添加到 Agent Bridge。
5. 确认：
   - 显示名称
   - 唯一标识：openclaw
   - Agent 类型：OpenClaw
   - OpenClaw 会话：agent:main:main
6. 点击保存。
7. 点击测试连接。
8. 如果连接可达，点击发送测试消息。
9. 如果尚无聊天室，确认自动创建测试聊天室。
10. 观察 OpenClaw 是否收到包含 callback_url 的消息。
11. 观察 UI 是否收到 callback 回写。
12. 观察测试房间消息、events、runtime log。
13. 完成后清理测试房间。
```

---

## 5. 当前待优化 / 待修改 / 待实现清单

下面是当前版本后续仍建议继续完成的内容。

---

# A. 高优先级：合并 main 前建议完成

## A1. OpenClaw 真机完整链路验证

当前代码层已具备 OpenClaw 联调能力，但仍需要实机确认：

```text
Agent Bridge -> OpenClaw sessions_send -> OpenClaw 生成回复 -> callback/MCP reply_turn -> Agent Bridge 回写 -> Runtime V2 推进下一轮
```

需要验证：

```text
1. OpenClaw Gateway 是否接受当前 tools/invoke payload。
2. OpenClaw 是否能看到 args.message 中的 callback_url。
3. OpenClaw 是否会按指令执行 callback。
4. 如果不能自动 callback，是否需要 OpenClaw 侧 Skill / AGENT.md / 工具权限补充。
5. callback token 是否正确。
6. Runtime V2 是否能完成 turn。
```

验收标准：

```text
发送测试消息后，OpenClaw 能真实回写到 Agent Bridge，并在 UI 中显示回复。
```

---

## A2. MCP Server 与真实 MCP Client 兼容性验证

当前 MCP Server 已实现，但仍需确认它是否符合真实 MCP Client 的协议预期。

需要验证：

```text
1. Claude Desktop / OpenClaw / 其他 MCP Client 是否能启动该 MCP Server。
2. tools/list 是否能看到 agent_bridge.* 工具。
3. tools/call 是否能调用 reply_turn。
4. reply_turn 是否与 callback 走同一套回写逻辑。
5. 错误返回是否符合 MCP Client 可读格式。
```

待优化：

```text
如果当前自实现 JSON-RPC 与标准 MCP 有兼容差异，建议引入官方 MCP Python SDK 或补齐协议细节。
```

验收标准：

```text
至少一个真实 MCP Client 可以成功调用 agent_bridge.reply_turn。
```

---

## A3. 测试房间删除建议后端原子化

当前测试房间清理逻辑主要由前端完成：

```text
1. 如果 running，先 pause。
2. 再调用 delete。
```

这已经可用，但更稳的方式是给后端 `/api/rooms/delete` 增加参数：

```json
{
  "id": "test_xxx",
  "force_if_temporary": true
}
```

后端逻辑：

```text
1. 检查 room 是否存在。
2. 检查 room.meta.temporary 是否为 true。
3. 如果 running，则自动置为 paused。
4. 删除 room 配置。
5. 返回删除成功。
```

好处：

```text
1. 避免前端 pause 成功、delete 失败的中间态。
2. 防止普通房间被误删。
3. 后端逻辑更集中。
```

验收标准：

```text
前端清理测试房间只需调用一次 delete API，后端安全完成自动暂停和删除。
```

---

## A4. 安装脚本与 dev/main 发布路径确认

当前项目存在安装脚本与分支发布问题。

需要确认：

```text
1. install.sh / install.ps1 默认安装 main 还是 dev。
2. dev 合并 main 前，README 安装命令是否仍正确。
3. 用户通过 bridge start 启动的是否是新 UI。
4. 旧版本缓存是否会导致 UI 看起来没变。
5. 静态资源是否带 no-cache。
```

待优化：

```text
1. 增加 bridge version / build info。
2. UI 页脚显示当前分支或版本号。
3. 安装后提示当前安装路径。
4. 提供 bridge update 命令。
```

验收标准：

```text
用户全新安装后，启动 UI 看到的是当前新版本界面。
```

---

## A5. 补齐端到端集成测试

当前已经有较多测试，但仍建议补一批完整链路测试。

建议新增：

```text
1. test_ui_openclaw_discovery_to_adapter_config
2. test_agent_integration_test_auto_creates_temporary_room
3. test_cleanup_temporary_room_force_delete
4. test_callback_completes_current_turn
5. test_scheduler_does_not_call_legacy_tick_when_v2_enabled
6. test_openclaw_sessions_message_contains_callback_instruction
7. test_mcp_reply_turn_completes_turn
```

验收标准：

```bash
python -m unittest discover -s tests
```

全部通过。

---

# B. 中优先级：建议近期完成

## B1. Room 当前 turn 状态可视化增强

当前系统已经有 `/api/rooms/{room_id}/turn`，但 UI 中对当前 turn 的展示仍可继续增强。

建议在 room 对话页显示：

```text
当前状态：running / paused / waiting_response
当前等待：openclaw
turn_id：可折叠
correlation_id：可折叠
等待时长：xx / timeout_seconds
下一位 Agent：hermes
操作：跳过 / 重试 / 手动完成 / 暂停
```

价值：

```text
1. 用户能看懂系统卡在哪里。
2. 联调 OpenClaw 时能快速判断是 Bridge 卡住还是 Agent 没回写。
3. 降低调试门槛。
```

---

## B2. Callback 调试面板

建议增加一个“回写调试”面板。

显示：

```text
callback_url
turn_id
correlation_id
curl 示例
```

例如：

```bash
curl -X POST "http://127.0.0.1:7899/api/rooms/test_room/agents/openclaw/callback?token=xxx" \
  -H "Content-Type: application/json" \
  -d '{"turn_id":"...","correlation_id":"...","message":"测试回复"}'
```

价值：

```text
1. 当 OpenClaw 不自动回写时，用户可以手动验证 Bridge callback 是否通。
2. 可以快速定位问题在 Bridge 侧还是 Agent 侧。
```

---

## B3. Agent 连接健康状态增强

当前 UI 有连接状态，但可以进一步拆分：

```text
未配置
连接可达
已投递测试消息
等待 callback
callback 已回写
联调通过
联调失败
```

建议状态来源：

```text
1. Adapter capability。
2. /api/agent/test。
3. /api/agent/integration-test。
4. room current_turn。
5. events.jsonl。
```

---

## B4. 自动识别 OpenClaw token_jsonpath

当前 UI 可以显示 token path / token jsonpath，但普通用户仍然不应该判断：

```text
gateway.auth.password
gateway.auth.token
```

建议后端 discovery 阶段自动检测 `~/.openclaw/openclaw.json` 中实际存在的字段，并返回：

```json
{
  "auth": {
    "type": "bearer",
    "token_path": "~/.openclaw/openclaw.json",
    "token_jsonpath": "gateway.auth.password"
  },
  "auth_detected": true
}
```

UI 只显示：

```text
认证：已自动识别
```

开发者配置里才展示实际 jsonpath。

---

## B5. OpenClaw 侧执行能力提示增强

即使 Agent Bridge 把 callback_url 发给 OpenClaw，OpenClaw 是否真的会执行 HTTP POST，还取决于 OpenClaw 的工具权限和执行策略。

建议在 OpenClaw message 中更明确：

```text
如果你可以执行 HTTP 请求，请 POST 到 callback_url。
如果你不能执行 HTTP 请求，请明确说明无法回写，并提示用户需要开启 exec/tool 权限。
不要只在当前会话中回答。
```

同时可在 UI 中提示：

```text
如果 30 秒内没有 callback，可能是 OpenClaw 没有执行 HTTP 请求权限。
```

---

## B6. Runtime timeout 策略 UI 化

当前 Runtime 支持 timeout，但 UI 里还可以让用户配置：

```text
等待回复超时：180 秒
超时后：跳过 / 重试 / 暂停 / 标记错误
最大重试次数：1
```

普通用户默认：

```text
180 秒后跳过
```

开发者模式可细调。

---

## B7. Room Policy 产品化

当前 room policy 仍偏工程化。

未来可以支持：

```text
轮流发言
主持人模式
只回复被点名消息
允许主动插话
广播模式
```

底层仍映射为：

```yaml
policy:
  type: round_robin
  allow_interrupt: false
  on_timeout: skip
```

---

# C. 低优先级：后续增强

## C1. 流式消息支持

未来可以支持 Agent 流式输出：

```text
agent.response.delta
agent.response.completed
```

对应 UI 可显示“正在输入”。

适用场景：

```text
OpenAI-compatible Agent
Claude / Codex
自定义 WebSocket Agent
```

---

## C2. WebSocket / SSE 前端实时刷新

当前 UI 仍主要靠轮询刷新。

未来可以增加：

```text
/api/events/stream
WebSocket
SSE
```

用于实时推送：

```text
新消息
当前 turn 变化
Agent 状态变化
callback 收到
runtime error
```

---

## C3. file_outbox watcher 完整化

当前 file_mailbox adapter 已存在，但需要确认：

```text
1. 是否有稳定 watcher。
2. 是否支持 outbox cursor。
3. 是否能去重。
4. 是否能携带 turn_id / correlation_id。
5. 是否能统一走 receive_agent_response。
```

Hermes 类文件型 Agent 后续会依赖这块。

---

## C4. Agent 主动发言策略

未来需要进一步支持 Agent 主动发言：

```text
normal
interrupt
whisper
system
```

并让 room policy 决定如何处理：

```text
append only
interrupt current turn
route to moderator
broadcast
```

---

## C5. 多模型 / 多 Agent 能力声明

Adapter capability 可进一步丰富：

```text
supports_callback
supports_mcp
supports_file_outbox
supports_streaming
supports_active_push
supports_tools
supports_images
```

UI 可显示 Agent 能力标签。

---

## C6. 日志脱敏与 Debug 开关

之前版本曾加入 response_body 日志。后续应统一处理：

```text
1. 默认不记录完整 response_body。
2. token/password/api_key 自动脱敏。
3. debug.log_adapter_response_body 显式开启。
4. UI 提供导出诊断包时自动脱敏。
```

---

## C7. 诊断包导出

建议添加：

```text
导出诊断包
```

内容包括：

```text
bridge.yaml 脱敏版
room state
events.jsonl
runtime.log
最近 active.jsonl
adapter capability
OpenClaw discovery 结果
```

用于用户反馈问题时一键导出。

---

## C8. Room / Agent 模板市场

未来可以做预设模板：

```text
OpenClaw + Hermes 双人讨论
OpenClaw 单 Agent 测试房间
Claude Code + OpenClaw 协作房间
Webhook Agent 模板
Manual Agent 模板
```

降低新用户上手成本。

---

## 6. 当前风险点

### 6.1 OpenClaw 是否真的能 callback 仍需实测

代码已经把 callback_url 放进 OpenClaw message，但 OpenClaw 是否能执行 HTTP POST 取决于：

```text
1. OpenClaw 当前 session 是否允许工具执行。
2. 是否需要用户批准 exec/http 请求。
3. OpenClaw 是否能访问 127.0.0.1:7899。
4. OpenClaw 的运行环境是否与 Agent Bridge 同机。
5. callback token 是否正确。
```

这是当前最大实机风险。

---

### 6.2 MCP Server 兼容性风险

当前 MCP Server 是自实现协议，仍需真实客户端验证。

风险包括：

```text
1. initialize 协议细节不完整。
2. tools/list schema 不完全兼容。
3. tools/call 返回格式不完全兼容。
4. stdio flush / newline / error code 处理不完整。
```

---

### 6.3 UI 与后端配置字段仍需持续保持一致

现在 UI 会生成：

```yaml
adapter:
  type: openclaw_sessions
  config:
    url: ...
    sessions_key: ...
  auth: ...
  response:
    mode: callback
```

后续如果 adapter schema 变化，必须同步更新：

```text
1. UI collectAdapterData。
2. server handle_update_config_full。
3. adapters normalize_adapter。
4. tests。
```

---

### 6.4 自动测试房间会写入 bridge.yaml

虽然现在有测试房间标识和清理入口，但测试房间仍然是正式配置的一部分。

风险：

```text
1. 用户不清理会积累测试 room。
2. 多次测试同一个 Agent 会生成多个 test_xxx。
3. 如果用户误把测试房间当正式房间，可能困惑。
```

可通过后续“复用同一 Agent 的测试房间”进一步优化。

---

## 7. 建议的后续开发优先级

### P0：合并前必须验证

```text
1. OpenClaw 真机 callback 链路。
2. 发送测试消息完整联调。
3. Callback 成功后 Runtime V2 推进。
4. 旧 PollManager 不再双驱动。
5. 安装后 UI 确实为新版。
```

### P1：合并前强烈建议修正

```text
1. 后端 rooms/delete 支持 force_if_temporary。
2. UI 当前 turn 状态可视化。
3. Callback 调试 curl 示例。
4. OpenClaw token_jsonpath 自动识别。
5. MCP 真实客户端验证。
```

### P2：近期增强

```text
1. Room policy UI 产品化。
2. Runtime timeout 策略 UI 化。
3. file_outbox watcher 完整实测。
4. 诊断包导出。
5. 测试 room 复用策略。
```

### P3：未来能力

```text
1. 流式输出。
2. WebSocket/SSE 实时刷新。
3. Agent 主动插话策略。
4. Agent 模板市场。
5. 多模型能力标签。
```

---

## 8. 当前版本建议验收清单

### 8.1 基础启动验收

```text
bridge start 能启动服务。
浏览器打开 http://127.0.0.1:7899 正常。
UI 为新版 Agent 配置界面。
/api/status 正常。
/api/config 正常。
```

### 8.2 Agent 配置验收

```text
扫描能发现 OpenClaw。
添加 OpenClaw 后显示简洁卡片。
唯一标识默认或可填写。
Agent 类型为 OpenClaw。
OpenClaw 会话默认为 agent:main:main。
保存后 bridge.yaml 写入 adapter.type=openclaw_sessions。
```

### 8.3 测试连接验收

```text
点击测试连接。
如果 OpenClaw Gateway 可达，显示连接可达。
提示“这不代表 callback 已完成回写”。
```

### 8.4 发送测试消息验收

```text
点击发送测试消息。
如果 Agent 未加入 room，提示自动创建测试聊天室。
确认后创建测试房间。
Runtime V2 投递测试消息。
返回 turn_id/correlation_id。
OpenClaw 收到包含 callback_url 的消息。
```

### 8.5 Callback 验收

```text
OpenClaw callback 回写。
active.jsonl 出现 Agent 回复。
消息包含 reply_to/correlation_id。
events.jsonl 出现 agent.response.received。
state.json current_turn 完成或推进。
UI 显示回复。
```

### 8.6 测试房间验收

```text
自动创建的测试房间显示“测试房间”。
按钮显示“清理”。
点击清理能暂停并删除测试房间。
刷新后测试房间不再出现。
```

---

## 9. 建议给开发 Agent 的下一步任务

```text
当前 dev 分支已经完成主要架构改造和 UI 产品化，下一步不要继续大范围重构，重点进入真实联调和收尾稳定阶段。

请优先完成以下任务：

1. 用真实 OpenClaw Gateway 跑通完整链路：发送测试消息 → OpenClaw 被唤醒 → callback/MCP reply_turn 回写 → Runtime V2 推进。
2. 如果 OpenClaw 没有自动 callback，请定位是 OpenClaw 工具权限问题、callback token 问题、还是 Agent Bridge 回写接口问题。
3. 给 /api/rooms/delete 增加 force_if_temporary=true，后端原子清理测试房间。
4. 在 room 对话页显示 current_turn 状态，包括 waiting agent、turn_id、correlation_id、等待时间、timeout。
5. 增加 callback 调试面板，提供一键复制 curl callback 示例。
6. 验证 MCP Server 能被真实 MCP Client 调用，至少跑通 agent_bridge.reply_turn。
7. 补充端到端测试，尤其是 OpenClaw discovery、integration-test 自动创建测试房间、callback 完成 turn、测试房间清理。
8. 检查安装脚本和 README，确保普通用户安装后看到的是新版 UI。
```

---

## 10. 最终评价

当前 `dev` 分支已经从最初的：

```text
文件轮询 + 单向唤醒 + waiting_response 死等
```

升级为：

```text
Adapter V2 + Runtime V2 + EventBus + Scheduler + Callback/MCP 回写 + OpenClaw 专用 adapter + 产品化 UI 联调
```

整体方向正确，架构核心已经成型，UI 也已经从工程化配置面板明显进化为普通用户可理解的操作流程。

当前最重要的工作不再是继续堆新抽象，而是：

```text
真实联调
稳定性验证
MCP 兼容性确认
UI 状态可视化增强
安装发布流程完善
```

如果 OpenClaw 真机 callback 链路跑通，并且测试全部通过，则当前 dev 分支可以考虑进入合并 main 前的候选状态。

