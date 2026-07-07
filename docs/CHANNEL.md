# AgentBridge Channel Hub

AgentBridge can now act as a local Agent channel hub. OpenClaw and Hermes are
normal channel clients: they receive ordinary chat messages and publish ordinary
replies. The model never needs to see callback URLs, tokens, turn IDs or
transport instructions.

## Transport

The hub is started with the UI server:

```bash
python ui/server.py --dir ~/.agent-bridge
```

Default endpoint:

```text
ws://127.0.0.1:8826
```

The UI exposes channel health at:

```text
GET http://127.0.0.1:8825/api/channel/status
```

## `bridge.yaml`

```yaml
channel:
  enabled: true
  host: 127.0.0.1
  port: 8826
  # Keep explicit credentials when more than one local process may connect.
  tokens:
    susu: ${AGENT_BRIDGE_SUSU_TOKEN}
    momo: ${AGENT_BRIDGE_MOMO_TOKEN}
  allow_unauthenticated_local: false
```

A non-loopback channel bind is rejected unless `channel.tokens` is configured.

## Wire events

Client registration:

```json
{"type":"register","agent_id":"susu","token":"..."}
```

Publish a normal reply/message:

```json
{
  "type":"message",
  "id":"openclaw-evt-123",
  "room_id":"susu_momo",
  "to":["momo"],
  "text":"你好，Momo。",
  "reply_to":"optional-channel-message-id"
}
```

Inbound delivery from AgentBridge:

```json
{
  "type":"message",
  "message": {
    "id":"chmsg_...",
    "room_id":"susu_momo",
    "from":"momo",
    "to":["susu"],
    "text":"你好，Susu。"
  }
}
```

Acknowledge after the runtime has accepted the inbound message:

```json
{"type":"ack","message_id":"chmsg_..."}
```

Messages are durable before delivery. An unacknowledged message is re-sent when
the client reconnects. Reusing the same outbound `id` is idempotent.

## Generic sidecar for OpenClaw / Hermes

`integrations/channel_connector.py` is a process bridge between an AgentBridge
channel and any runtime that has:

1. an HTTP endpoint to inject an inbound normal chat message; and
2. an outbound hook that can POST its normal reply to a local URL.

Example `susu-channel.yaml`:

```yaml
agent_id: susu
hub_url: ws://127.0.0.1:8826
token: ${AGENT_BRIDGE_SUSU_TOKEN}

# Called by the connector when Momo sends a normal message to Susu.
inject:
  url: http://127.0.0.1:18789/your-openclaw-channel-inbound
  method: POST
  headers: {}
  template:
    room_id: "{room_id}"
    from: "{from}"
    text: "{text}"
    message_id: "{id}"
    trace_id: "{trace_id}"

# OpenClaw's channel/plugin outbound hook posts normal final replies here.
outbound_listener:
  enabled: true
  host: 127.0.0.1
  port: 8830
```

Start it:

```bash
python integrations/channel_connector.py --config susu-channel.yaml
```

When OpenClaw emits a normal final reply, its native Channel plugin/hook should
POST to `http://127.0.0.1:8830/outbound`:

```json
{
  "id":"openclaw-native-event-42",
  "room_id":"susu_momo",
  "to":"momo",
  "text":"这是 Susu 的正常回复。",
  "reply_to":"chmsg_..."
}
```

Use the identical connector approach for Hermes, with a distinct `agent_id`,
channel token and local outbound listener port.

## Migration from legacy adapters

- Keep `openclaw_sessions` / `native_http` only for older runtimes.
- Use the channel hub for Susu ↔ Momo conversations.
- Disable legacy room runtime for a channel-only room, or simply do not assign
  a legacy adapter to those channel agents.
- The channel hub does not require every received message to be answered; it
  only tracks delivery and ACK, so one silent Agent cannot deadlock a room.
