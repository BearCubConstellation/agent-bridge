# AgentBridge Architecture

## Core rule

**Room Runtime is the only conversation centre.**

`active.jsonl` is the durable source of truth for messages. `state.json` owns
turn state. Adapters are transport implementations only; they never own a
second room, inbox, acknowledgement ledger, message-id namespace or scheduler.

```text
Room / JSONL / Runtime / Policy / UI
                  |
              Adapter layer
      ┌───────────┼───────────┐
      |           |           |
OpenClaw      Hermes       Coding / HTTP / MCP / Human
```

## Agent types

- `openclaw_channel`: sends a Room delivery to an OpenClaw-native chat hook.
- `hermes_channel`: sends a Room delivery to a Hermes-native chat hook.
- `cli`: synchronous local command adapter.
- `native_http`, `mcp_tool`, `file_mailbox`, `manual`: existing generic adapters.

OpenClaw and Hermes intentionally have separate adapter types because their
session creation, inbound message hooks and reply hooks can differ. They still
share the same Room message model and callback/reply ingress.

## Context persistence

Each room has editable, file-backed context next to its transcript:

```text
rooms/<room>/
  active.jsonl                 # complete current scene
  history/                     # archived scenes
  state.json                   # runtime state
  context.json                 # shared summary, facts and game state
  agent_memory/<agent>.json    # room-scoped role memory
  sessions.json                # one native session mapping per agent+room
```

The runtime transcript is never replaced by a summary. An adapter receives a
bounded bundle of shared context, personal room memory and recent messages.
After an Agent restart or context-window reset, that bundle rebuilds its room
session without leaking information from another room.

## Policies

Policies decide interaction behaviour; adapters do not. Recommended room modes:

- `turn`: task work, coding review, structured games and debates.
- `free_chat`: role play and natural conversation with cooldown/loop guards.
- `event`: quizzes, voting and game-state transitions.

A future policy implementation must keep Room Runtime authoritative and write
all outcomes as ordinary Room messages or room context updates.
