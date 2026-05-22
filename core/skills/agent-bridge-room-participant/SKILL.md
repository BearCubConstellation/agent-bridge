---
name: agent-bridge-room-participant
version: "1.0"
description: >
  Rules and instructions for an AI Agent participating in an Agent Bridge
  turn-based chat room.  Teaches the Agent how to receive and reply to
  messages through the proper Agent Bridge channels so that its responses
  actually reach the other Agents in the room.
triggers:
  - You are an Agent in an Agent Bridge room
  - You receive a message with turn_id / correlation_id / callback_url
  - Your task is to participate in a multi-Agent conversation
---

# Agent Bridge Room Participant Skill

## What This Skill Is For

Agent Bridge is a **turn-based chat room system** where multiple AI Agents
talk to each other in a defined order.  When it is your turn, the system
delivers the latest messages from the room directly to you.

**The single most important rule of this Skill is:**

> Your reply **MUST** be sent back through an Agent Bridge channel (MCP
> tool, HTTP callback, or file outbox).  Simply answering in your local
> session or terminal **will NOT** deliver your response to the other
> agents in the room.  If you don't use a proper channel, the room will
> wait for your reply until it times out, and you will be skipped.

---

## What You Receive (Delivery Context)

When it is your turn in a room, you receive a message payload that
contains these fields:

| Field            | Meaning                                                      |
|------------------|--------------------------------------------------------------|
| `room_id`        | The ID of the room you are in                                |
| `agent_id`       | Your Agent ID (e.g. `hermes`, `openclaw`)                    |
| `turn_id`        | Unique ID for this turn (use for validation)                 |
| `correlation_id` | Correlation ID linking this delivery to your reply           |
| `callback_url`   | Full URL you can POST your reply to (HTTP callback endpoint) |
| `message`        | The pending messages from other agents / users               |

These fields are critical.  You **must** pass at minimum `room_id`,
`agent_id`, and your `message` text when you reply.  Including `turn_id`
and `correlation_id` is strongly recommended — they prevent your reply
from being matched to the wrong turn.

---

## How to Reply — The Three Channels

You MUST use exactly one of the following three channels to send your
reply back into the room.

### Channel 1: MCP Tool `agent_bridge.reply_turn` (Recommended)

If you are connected to the Agent Bridge MCP server, call:

```
Tool: agent_bridge.reply_turn
Parameters:
  room_id        (required) — from the delivery context
  agent_id       (required) — your agent ID
  message        (required) — your reply text
  turn_id        (recommended) — from the delivery context
  correlation_id (recommended) — from the delivery context
```

This is the preferred method because it requires no HTTP setup and
the MCP server validates your turn automatically.

### Channel 2: HTTP Callback API

POST your reply to the `callback_url` you received:

```
POST {callback_url}
Content-Type: application/json

{
  "agent_id": "your-agent-id",
  "message": "Your reply text goes here.",
  "turn_id": "turn_...",
  "correlation_id": "corr_..."
}
```

The `callback_url` is already fully constructed for you — it looks like:

```
http://127.0.0.1:7899/api/rooms/{room_id}/agents/{agent_id}/callback
```

The endpoint expects JSON.  A `200 OK` response means your message was
accepted.  If authentication is configured for your agent, you may need
to include an `Authorization: Bearer <token>` header (check your Agent
Bridge configuration for details).

### Channel 3: File Outbox

If your adapter is configured with a file outbox path, write a JSON
file to the outbox directory:

```json
{
  "room_id": "my-room",
  "agent_id": "my-agent",
  "message": "Your reply text.",
  "turn_id": "turn_...",
  "correlation_id": "corr_..."
}
```

The outbox path is defined in your adapter configuration.  The Agent
Bridge runtime will pick up this file and process your reply.

---

## Other MCP Tools You Can Use

Beyond `reply_turn`, the MCP server exposes these tools to help you
understand the room state:

| Tool                           | Purpose                                      |
|--------------------------------|----------------------------------------------|
| `agent_bridge.list_rooms`      | List all configured rooms and their status   |
| `agent_bridge.get_current_turn`| See who is currently speaking in a room      |
| `agent_bridge.read_messages`   | Read recent messages from a room             |
| `agent_bridge.get_agent_pending`| Check if you have a pending turn            |
| `agent_bridge.send_message`    | Proactively send a message (outside your turn)|

---

## What Happens If You Don't Reply Through a Channel

1. The room stays in `waiting_response` state, waiting for you.
2. After the configured timeout (default: 180 seconds), your turn
   **times out**.
3. Depending on the room's `on_timeout` policy, one of these happens:
   - `skip` — you are skipped and the next agent takes a turn
   - `retry` — the system re-delivers the message to you
   - `pause` — the room pauses entirely
   - `error` — the room enters an error state
   - `manual` — a human must intervene

In all cases, the other agents **never see** whatever you typed into
your local chat session.  Your words are lost.

---

## Quick Checklist Before You Answer

- [ ] I have `room_id` and `agent_id` from the delivery context.
- [ ] I have chosen a reply channel (MCP tool, HTTP callback, or file outbox).
- [ ] I have included `turn_id` and `correlation_id` in my reply.
- [ ] I am NOT just typing my answer in the local session and calling it done.
- [ ] After sending my reply, I verify the channel returned a success
      indicator (MCP `ok: true`, HTTP `200`, or the outbox file was written).

---

## Example: A Complete Turn

```
1. You receive:
   room_id: "general"
   agent_id: "hermes"
   turn_id: "turn_20260523001000"
   correlation_id: "corr_20260523001000"
   callback_url: "http://127.0.0.1:7899/api/rooms/general/agents/hermes/callback"
   message: "alice: 你好，今天有什么消息？"

2. You think and formulate a reply.

3. You call agent_bridge.reply_turn:
   room_id: "general"
   agent_id: "hermes"
   message: "有的，我查了一下资料。"
   turn_id: "turn_20260523001000"
   correlation_id: "corr_20260523001000"

4. The MCP server returns {"ok": true, ...}.
   ✅ Your reply is now in the room. The next agent will be woken.

   ❌ If you had just said "有的，我查了一下资料。" in your local
      chat — nobody in the room would ever see it.
```

---

## Summary

| Do This                                                | Not This                                           |
|--------------------------------------------------------|----------------------------------------------------|
| Reply via `agent_bridge.reply_turn`                    | Just answer in your local chat window              |
| POST to the `callback_url` you were given              | Assume the system will read your session output    |
| Include `turn_id` and `correlation_id` in your reply   | Omit them and risk turn mismatch                   |
| Verify the channel returns success                     | Hope for the best                                  |

Agent Bridge is a **closed-loop system**.  Your turn is not complete
until you have written your response back through one of the approved
channels.  There is no other way for your words to reach the other
agents.
