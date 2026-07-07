# Agent Bridge

> 本地多 Agent 消息总线、Channel Hub 与调试工具。

Agent Bridge 可同时保留旧版轮询/HTTP Adapter 兼容能力，并提供新的 WebSocket Channel Hub：OpenClaw、Hermes 等 Agent 作为普通通道客户端收发消息，不再把 callback URL、turnId 或鉴权信息塞进模型提示词。

## 快速开始

```bash
python -m pip install -r requirements.txt
python ui/server.py --open
```

默认地址：

```text
UI / HTTP API: http://127.0.0.1:8825
Channel Hub:    ws://127.0.0.1:8826
```

Channel 状态：`GET /api/channel/status`。

## Channel 模式

```text
OpenClaw Channel Client ─┐
                         ├─ AgentBridge Channel Hub ─ 房间、路由、持久化、ACK、重连补发
Hermes Channel Client ───┘
```

- 正常聊天消息进入/离开各自 Agent 会话。
- AgentBridge 只负责消息路由、去重、投递确认和审计，不强制“必须回复”。
- 断线未 ACK 的消息在重连后补发。
- 同一 outbound message ID 可重复发送而不重复入房间。

接入 OpenClaw / Hermes 的完整协议、Sidecar 和配置模板见 [docs/CHANNEL.md](docs/CHANNEL.md)。

## 运行边界

适合：本地 Agent 协作、通道联调、角色对话、人工介入与消息回放。

不适合：高并发生产消息系统、跨机器网络盘强一致、需要完整多租户权限隔离或企业级死信队列的场景。

## 测试

```bash
python -m unittest discover -s tests
```

## 许可证

MIT
