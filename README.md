# Agent Bridge

> 基于共享文件的本地多 Agent 异步对话与调试工具。

Agent Bridge 用房间级 `active.jsonl` 保存消息、用 V2 状态机投递任务，并通过 HTTP 回调或 MCP 回写回复。它适合本机或可信局域网中的多 Agent 联调、角色对话和工作流实验；**不是生产级消息队列或多租户平台**。

## 适合与不适合

适合：本地 Agent 协作、回调链路调试、轮询式异步对话、人工介入与回放。

不适合：高并发生产消息系统、跨机器网络盘强一致、需要完整权限隔离/审计/死信队列的场景。

## 快速开始

```bash
python -m pip install -r requirements.txt
python ui/server.py --open
```

默认地址：`http://127.0.0.1:8825`。

常用命令：

```bash
bridge start
bridge status
bridge open
bridge stop
```

## V2 运行机制

每个房间有独立的 `state.json`：

```text
shared_dir/
  bridge.yaml
  rooms/<room-id>/
    active.jsonl
    state.json
    events.jsonl
    runtime.log
    cursors/
```

- 状态写入采用房间级锁、临时文件、`fsync` 与原子替换；损坏状态会被保留为 `state.json.corrupt-*`，不会静默覆盖。
- 投递前先落盘为 `delivering`，网络调用完成后再基于最新状态提交结果，避免“回调先到、旧状态后写”造成回复丢失。
- 相同 `turn_id` 的重复回调幂等处理；过期 `turn_id`/`correlation_id` 会被拒绝。
- `to: agent-id` 的定向消息优先于普通轮询，不会因当前回合指向其他 Agent 而卡死。
- 不同房间并发运行，同一房间始终串行；超时由 scheduler 定时唤醒，不依赖轮询间隔。

## 回调与 MCP 安全

默认仅建议绑定回环地址：

```bash
python ui/server.py --host 127.0.0.1 --port 8825
```

若绑定非本地地址，例如 `0.0.0.0`，必须在 `bridge.yaml` 配置回调与 MCP Token，否则服务拒绝启动：

```yaml
security:
  callback_token: ${AGENT_BRIDGE_CALLBACK_TOKEN}
  mcp_token: ${AGENT_BRIDGE_MCP_TOKEN}
```

调用回调和 HTTP MCP 时使用请求头，不要把 Token 放在 URL 中：

```text
Authorization: Bearer <token>
```

HTTP MCP 地址：`POST /api/mcp`；工具列表：`GET /api/mcp/tools`。原生 stdio MCP 由每个 MCP 客户端按其自身配置启动 `core/mcp_server.py`，UI 服务不会再启动一个无法被客户端附着的后台 stdio 子进程。

## OpenClaw

`openclaw_sessions` 首次遇到未知工具名时会探测可用的发消息工具；成功结果会在本进程缓存，后续投递不会重复先走错误工具名。

## 配置轮廓

```yaml
shared_dir: ~/.agent-bridge
server:
  host: 127.0.0.1
  port: 8825

rooms:
  example:
    agents: [alice, bob]
    order: [alice, bob]
    status: running
    policy:
      on_timeout: retry
```

显式收件人使用 `to`：

```json
{"from":"user","to":"bob","msg":"只交给 Bob 处理"}
```

## 测试

```bash
python -m unittest discover -s tests
```

新增 V2 回归覆盖：定向路由、投递期间早到回调、重复回调、真实重试、跨房间并发与非本地暴露安全校验。

## 许可证

MIT
