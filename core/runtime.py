#!/usr/bin/env python3
"""V2 room runtime.

The runtime deliberately separates a short, locked state transition from the
potentially slow adapter call.  A turn is persisted as ``delivering`` before
an adapter is called; callback handlers can therefore safely arrive before the
adapter request returns without being overwritten by stale state.
"""
from __future__ import annotations

import os
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

_parent = str(Path(__file__).resolve().parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from adapters import adapter_capability, deliver_via_registry, normalize_adapter
from events import emit_event
from protocol import (
    EVT_AGENT_RESPONSE_RECEIVED,
    EVT_AGENT_WAKEUP_FAILED,
    EVT_AGENT_WAKEUP_REQUESTED,
    EVT_AGENT_WAKEUP_SUCCEEDED,
    EVT_ROOM_ERROR,
    EVT_ROOM_PAUSED,
    EVT_TURN_COMPLETED,
    EVT_TURN_SELECTED,
    EVT_TURN_SKIPPED,
    EVT_TURN_TIMEOUT,
    RESPONSE_CALLBACK,
    RESPONSE_MCP_TOOL,
    RESPONSE_SYNC,
    ROOM_ERROR,
    ROOM_PAUSED,
    ROOM_RUNNING,
    TURN_DELIVERING,
    TURN_FAILED,
    TURN_MANUAL_REQUIRED,
    TURN_SKIPPED,
    TURN_WAITING_RESPONSE,
    gen_delivery_id,
    make_turn,
)
from room_state import mutate_room_state, read_room_state_consistent
from rooms import (
    _extract_reply,
    _format_delivery,
    _line_no,
    _log_tick,
    _messages_with_lines,
    _pending_for_agent,
    append_room_message,
    ensure_room,
    normalize_room,
    read_room_cursor,
    room_active_file,
    room_dir,
    write_room_cursor,
)


def _shared_dir(config):
    return Path(os.path.expandvars(os.path.expanduser(str(config.get("shared_dir", "~/.agent-bridge")))))


def _callback_base_url(config):
    server_cfg = config.get("server", {}) or {}
    return "http://{}:{}".format(server_cfg.get("host", "127.0.0.1"), server_cfg.get("port", 8825))


def _callback_url(config, room_id, agent_id):
    return "{}/api/rooms/{}/agents/{}/callback".format(_callback_base_url(config), room_id, agent_id)


def _now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _schedule(room_id, timeout_at=None):
    """Best-effort scheduler notification; a missing scheduler is harmless."""
    try:
        from scheduler import get_scheduler
        scheduler = get_scheduler()
        scheduler.schedule_room(room_id)
        if timeout_at:
            deadline = _parse_ts(timeout_at)
            if deadline:
                scheduler.schedule_room_at(room_id, deadline.timestamp())
    except Exception:
        pass


def _advance(state, turn, order):
    """Advance from the selected turn index, not merely the old cursor."""
    if not order:
        state["current_turn"] = None
        state["waiting_for"] = ""
        state["waiting_line"] = 0
        return 0
    try:
        selected = int(turn.get("turn_index", state.get("turn_index", 0))) % len(order)
    except (TypeError, ValueError):
        selected = 0
    next_index = (selected + 1) % len(order)
    state["turn_index"] = next_index
    state["round"] = int(state.get("round", 0)) + (1 if next_index == 0 else 0)
    state["current_turn"] = None
    state["waiting_for"] = ""
    state["waiting_line"] = 0
    state["last_error"] = ""
    return next_index


def _timeout_at(seconds):
    return (datetime.now() + timedelta(seconds=max(1, int(seconds)))).strftime("%Y-%m-%d %H:%M:%S")


def _addressed_to(message, agent_id):
    target = message.get("to", "")
    if not target:
        return False
    if isinstance(target, list):
        return agent_id in target
    return str(target) == agent_id


def _select_pending(shared_dir, room_id, room_cfg, state, messages):
    """Pick a recipient while giving explicit ``to`` messages priority.

    A directed message must not be blocked simply because the round-robin
    cursor currently points at a different agent.
    """
    order = list(room_cfg.get("order", []))
    if not order:
        return "", 0, []
    try:
        start = int(state.get("turn_index", 0)) % len(order)
    except (TypeError, ValueError):
        start = 0

    candidates = []
    for offset in range(len(order)):
        index = (start + offset) % len(order)
        agent_id = order[index]
        cursor = read_room_cursor(shared_dir, room_id, agent_id)
        pending = _pending_for_agent(messages, agent_id, cursor)
        if pending:
            first_direct = next((m for m in pending if _addressed_to(m, agent_id)), None)
            candidates.append((agent_id, index, pending, first_direct))

    if not candidates:
        return "", start, []

    directed = [item for item in candidates if item[3] is not None]
    if directed:
        # Earlier message wins; order is a deterministic tie-breaker.
        directed.sort(key=lambda item: (_line_no(item[3]), (item[1] - start) % len(order)))
        return directed[0][0], directed[0][1], directed[0][2]

    # Normal round-robin: the first ready agent from the current cursor wins.
    return candidates[0][0], candidates[0][1], candidates[0][2]


def _finish_completed_turn(shared_dir, room_id, room_cfg, turn, result):
    order = room_cfg.get("order", [])

    def complete(state):
        current = state.get("current_turn") or {}
        if current.get("turn_id") != turn.get("turn_id"):
            return {"stale": True}
        next_index = _advance(state, current, order)
        return {"next_index": next_index}

    _state, outcome = mutate_room_state(shared_dir, room_id, room_cfg, complete)
    if outcome.get("stale"):
        return {**result, "action": "stale"}
    emit_event(shared_dir, room_id, EVT_TURN_COMPLETED, actor=turn.get("agent_id", ""),
               turn_id=turn.get("turn_id", ""), correlation_id=turn.get("correlation_id", ""),
               meta={"next_turn_index": outcome["next_index"]})
    _log_tick(shared_dir, room_id, "response_seen", "已确认 Agent 回复并推进下一轮",
              agent_id=turn.get("agent_id", ""), meta={"message_id": turn.get("response_message_id", "")})
    _schedule(room_id)
    return {**result, "action": "response_received", "to_agent": turn.get("agent_id", "")}


def _pause_or_error(shared_dir, room_id, room_cfg, turn, action, result):
    status = ROOM_PAUSED if action == "pause" else ROOM_ERROR
    event = EVT_ROOM_PAUSED if action == "pause" else EVT_ROOM_ERROR

    def apply(state):
        current = state.get("current_turn") or {}
        if current.get("turn_id") != turn.get("turn_id"):
            return {"stale": True}
        state["status"] = status
        state["current_turn"] = None
        state["waiting_for"] = ""
        state["waiting_line"] = 0
        state["last_error"] = "turn timeout: {}".format(turn.get("agent_id", ""))
        return {"stale": False}

    _state, outcome = mutate_room_state(shared_dir, room_id, room_cfg, apply)
    if outcome.get("stale"):
        return {**result, "action": "stale"}
    emit_event(shared_dir, room_id, event, actor=turn.get("agent_id", ""),
               turn_id=turn.get("turn_id", ""), meta={"reason": "timeout"})
    return {**result, "action": "paused" if action == "pause" else "error",
            "error": "timeout: {}".format(turn.get("agent_id", ""))}


def _skip_turn(shared_dir, room_id, room_cfg, turn, result):
    order = room_cfg.get("order", [])

    def skip(state):
        current = state.get("current_turn") or {}
        if current.get("turn_id") != turn.get("turn_id"):
            return {"stale": True}
        current["state"] = TURN_SKIPPED
        next_index = _advance(state, current, order)
        return {"stale": False, "next_index": next_index}

    _state, outcome = mutate_room_state(shared_dir, room_id, room_cfg, skip)
    if outcome.get("stale"):
        return {**result, "action": "stale"}
    emit_event(shared_dir, room_id, EVT_TURN_SKIPPED, actor=turn.get("agent_id", ""),
               turn_id=turn.get("turn_id", ""), meta={"next_turn_index": outcome["next_index"]})
    _log_tick(shared_dir, room_id, "turn_skipped", "已跳过 {}".format(turn.get("agent_id", "")),
              agent_id=turn.get("agent_id", ""))
    _schedule(room_id)
    return {**result, "action": "skipped", "to_agent": turn.get("agent_id", "")}


def _deliver_turn(config, shared_dir, room_id, room_cfg, agent_cfg, turn, result):
    """Call an adapter after the ``delivering`` state is durable."""
    payload = dict(turn.get("delivery_payload") or {})
    agent_id = turn.get("agent_id", "")
    text = payload.get("message", "")
    from_agents = payload.get("from", "")
    active = room_active_file(shared_dir, room_id)
    context = {
        "message": text,
        "from": from_agents,
        "to": agent_id,
        "room": room_id,
        "room_path": str(room_dir(shared_dir, room_id)),
        "active_file": str(active),
        "turn_id": turn.get("turn_id", ""),
        "correlation_id": turn.get("correlation_id", ""),
        "callback_url": _callback_url(config, room_id, agent_id),
    }

    emit_event(shared_dir, room_id, EVT_AGENT_WAKEUP_REQUESTED, actor=agent_id,
               turn_id=turn.get("turn_id", ""), correlation_id=turn.get("correlation_id", ""),
               meta={"attempt": turn.get("attempts", 1)})
    _log_tick(shared_dir, room_id, "delivery_attempt", "准备投递给 {}".format(agent_id),
              agent_id=agent_id, meta={"attempt": turn.get("attempts", 1), "messages": len(turn.get("input_message_ids", []))})

    started = _time.monotonic()
    ticket = deliver_via_registry(agent_cfg, text, from_agents, context)
    elapsed = round(_time.monotonic() - started, 2)
    delivered = bool(ticket.get("ok"))
    response_mode = ticket.get("response_mode", RESPONSE_CALLBACK)
    detail = ticket.get("detail") or ticket.get("error", "")
    sync_text = _extract_reply(ticket.get("sync_response", "")) if response_mode == RESPONSE_SYNC else None

    def finish(state):
        current = state.get("current_turn") or {}
        if current.get("turn_id") != turn.get("turn_id"):
            return {"stale": True}
        if not delivered:
            current["state"] = TURN_FAILED
            current["last_error"] = detail
            state["current_turn"] = current
            state["status"] = ROOM_ERROR
            state["last_error"] = detail
            return {"stale": False, "failed": True}

        # Mark the input cursor once per turn, even when retrying delivery.
        if not current.get("delivery_acknowledged"):
            input_line_max = int(current.get("input_line_max", 0) or 0)
            if input_line_max:
                write_room_cursor(shared_dir, room_id, agent_id, input_line_max)
            current["delivery_acknowledged"] = True
            state["turn_count"] = int(state.get("turn_count", 0)) + 1

        # A callback may have completed while the adapter request was in flight.
        if current.get("response_message_id"):
            current["state"] = TURN_WAITING_RESPONSE
            state["current_turn"] = current
            return {"stale": False, "callback_won": True}

        if sync_text:
            msg = append_room_message(
                shared_dir, room_id, agent_id, sync_text, kind="agent",
                reply_to=current.get("turn_id", ""), correlation_id=current.get("correlation_id", ""),
                meta={"source": "sync_response"},
            )
            current["response_message_id"] = msg.get("id", "")
            state["last_message_id"] = msg.get("id", "")
            current["sync_response"] = True

        current["state"] = TURN_WAITING_RESPONSE
        current["last_error"] = ""
        state["current_turn"] = current
        state["waiting_for"] = agent_id
        state["waiting_line"] = int(current.get("input_line_max", 0) or 0)
        state["last_error"] = ""
        return {"stale": False, "sync": bool(sync_text), "response_message_id": current.get("response_message_id", "")}

    saved_state, outcome = mutate_room_state(shared_dir, room_id, room_cfg, finish)
    if outcome.get("stale"):
        return {**result, "action": "stale", "to_agent": agent_id}
    if outcome.get("failed"):
        emit_event(shared_dir, room_id, EVT_AGENT_WAKEUP_FAILED, actor=agent_id,
                   turn_id=turn.get("turn_id", ""), meta={"error": detail, "elapsed": elapsed})
        _log_tick(shared_dir, room_id, "delivery_failed", "调用 {} 失败：{}".format(agent_id, detail),
                  level="error", agent_id=agent_id)
        return {**result, "ok": False, "action": "delivery_failed", "to_agent": agent_id, "error": detail}

    emit_event(shared_dir, room_id, EVT_AGENT_WAKEUP_SUCCEEDED, actor=agent_id,
               turn_id=turn.get("turn_id", ""), correlation_id=turn.get("correlation_id", ""),
               meta={"detail": detail, "elapsed": elapsed, "response_mode": response_mode})
    fresh_turn = saved_state.get("current_turn") or {}
    _log_tick(shared_dir, room_id, "delivery_succeeded", "已成功投递给 {}，等待回复".format(agent_id),
              agent_id=agent_id, meta={"elapsed": elapsed, "response_mode": response_mode})
    _schedule(room_id, fresh_turn.get("timeout_at", ""))
    return {**result, "action": "sync_response" if outcome.get("sync") else "waiting",
            "to_agent": agent_id, "delivered": True, "waiting_for": agent_id,
            "response_auto_written": bool(outcome.get("sync"))}


def _handle_timeout(config, shared_dir, room_id, room_cfg, agent_cfg, turn, result):
    policy = room_cfg.get("policy", {})
    on_timeout = policy.get("on_timeout", "skip") if isinstance(policy, dict) else "skip"
    agent_id = turn.get("agent_id", "")
    emit_event(shared_dir, room_id, EVT_TURN_TIMEOUT, actor=agent_id,
               turn_id=turn.get("turn_id", ""), meta={"on_timeout": on_timeout})
    _log_tick(shared_dir, room_id, "turn_timeout", "{} 超时未回复，执行 {}".format(agent_id, on_timeout),
              level="warn", agent_id=agent_id)

    if on_timeout == "retry":
        attempts = int(turn.get("attempts", 1) or 1)
        max_attempts = int(turn.get("max_attempts", 2) or 2)
        if attempts < max_attempts:
            retry_turn = {}

            def retry(state):
                current = state.get("current_turn") or {}
                if current.get("turn_id") != turn.get("turn_id") or current.get("response_message_id"):
                    return {"stale": True}
                current["attempts"] = attempts + 1
                current["delivery_id"] = gen_delivery_id()
                current["state"] = TURN_DELIVERING
                current["timeout_at"] = _timeout_at(current.get("timeout_seconds", 180))
                state["current_turn"] = current
                state["waiting_for"] = agent_id
                retry_turn.update(current)
                return {"stale": False}

            _state, outcome = mutate_room_state(shared_dir, room_id, room_cfg, retry)
            if not outcome.get("stale"):
                _log_tick(shared_dir, room_id, "turn_retry", "重试 {}（第 {} 次）".format(agent_id, attempts + 1), agent_id=agent_id)
                return _deliver_turn(config, shared_dir, room_id, room_cfg, agent_cfg, retry_turn, result)
        return _skip_turn(shared_dir, room_id, room_cfg, turn, result)
    if on_timeout == "pause":
        return _pause_or_error(shared_dir, room_id, room_cfg, turn, "pause", result)
    if on_timeout == "error":
        return _pause_or_error(shared_dir, room_id, room_cfg, turn, "error", result)
    if on_timeout == "manual":
        def manual(state):
            current = state.get("current_turn") or {}
            if current.get("turn_id") != turn.get("turn_id"):
                return {"stale": True}
            current["state"] = TURN_MANUAL_REQUIRED
            current["last_error"] = "timeout, manual intervention required"
            state["current_turn"] = current
            return {"stale": False}
        _state, outcome = mutate_room_state(shared_dir, room_id, room_cfg, manual)
        return {**result, "action": "manual_required" if not outcome.get("stale") else "stale", "to_agent": agent_id}
    return _skip_turn(shared_dir, room_id, room_cfg, turn, result)


def run_room_step(config, room_id):
    """Execute one safe V2 state-machine step for a room."""
    shared_dir = _shared_dir(config)
    rooms_cfg = config.get("rooms", {}) or {}
    result = {"ok": True, "room_id": room_id, "action": "noop"}
    if room_id not in rooms_cfg:
        return {**result, "ok": False, "error": "room not found"}

    room_cfg = normalize_room({**rooms_cfg[room_id], "id": room_id})
    ensure_room(shared_dir, room_cfg)
    state = read_room_state_consistent(shared_dir, room_id, room_cfg)
    if state.get("status") != ROOM_RUNNING:
        return {**result, "error": "room not running"}

    if int(state.get("turn_count", 0)) >= int(room_cfg.get("max_turns", 50)):
        def pause_for_limit(current):
            current["status"] = ROOM_PAUSED
            current["last_error"] = "max_turns reached"
            return True
        mutate_room_state(shared_dir, room_id, room_cfg, pause_for_limit)
        emit_event(shared_dir, room_id, EVT_ROOM_PAUSED, meta={"reason": "max_turns"})
        return {**result, "action": "paused", "error": "max_turns reached"}

    current_turn = state.get("current_turn") or {}
    current_state = current_turn.get("state")
    if current_turn:
        if current_state == TURN_DELIVERING:
            return {**result, "action": "delivering", "waiting_for": current_turn.get("agent_id", "")}
        if current_state == TURN_WAITING_RESPONSE:
            if current_turn.get("response_message_id"):
                return _finish_completed_turn(shared_dir, room_id, room_cfg, current_turn, result)
            deadline = _parse_ts(current_turn.get("timeout_at", ""))
            if deadline and datetime.now() >= deadline:
                agent_cfg = (config.get("agents", {}) or {}).get(current_turn.get("agent_id", ""), {})
                return _handle_timeout(config, shared_dir, room_id, room_cfg, agent_cfg, current_turn, result)
            _schedule(room_id, current_turn.get("timeout_at", ""))
            return {**result, "action": "waiting", "waiting_for": current_turn.get("agent_id", "")}
        if current_state in (TURN_MANUAL_REQUIRED, TURN_FAILED):
            return {**result, "action": current_state, "waiting_for": current_turn.get("agent_id", "")}

    order = room_cfg.get("order", [])
    if not order:
        def no_agents(current):
            current["status"] = ROOM_ERROR
            current["last_error"] = "room has no agents"
            return True
        mutate_room_state(shared_dir, room_id, room_cfg, no_agents)
        emit_event(shared_dir, room_id, EVT_ROOM_ERROR, meta={"error": "no agents"})
        return {**result, "ok": False, "action": "error", "error": "no agents"}

    messages = _messages_with_lines(room_active_file(shared_dir, room_id))
    agent_id, selected_index, pending = _select_pending(shared_dir, room_id, room_cfg, state, messages)
    if not pending:
        return {**result, "action": "no_pending"}

    agent_cfg = (config.get("agents", {}) or {}).get(agent_id)
    if not agent_cfg:
        return {**result, "ok": False, "action": "error", "error": "unknown agent: {}".format(agent_id)}

    cap = adapter_capability(agent_cfg)
    if not cap.get("automatic"):
        def require_manual(current):
            if current.get("current_turn"):
                return {"busy": True}
            current["current_turn"] = {
                "turn_id": "", "agent_id": agent_id, "state": TURN_MANUAL_REQUIRED,
                "started_at": _now_ts(), "timeout_at": "", "timeout_seconds": 0,
                "input_message_ids": [item.get("id", "") for item in pending],
                "input_line_max": max(_line_no(item) for item in pending),
                "response_message_id": "", "attempts": 1, "max_attempts": 1,
                "last_error": "manual agent, cannot auto-trigger", "turn_index": selected_index,
            }
            return {"busy": False}
        _state, outcome = mutate_room_state(shared_dir, room_id, room_cfg, require_manual)
        return {**result, "action": "manual_required" if not outcome.get("busy") else "busy", "to_agent": agent_id}

    response_cfg = (agent_cfg.get("adapter", {}) or {}).get("response", {}) or agent_cfg.get("wakeup", {}) or {}
    timeout_seconds = int(response_cfg.get("timeout_seconds", 180))
    turn = make_turn(room_id, agent_id, pending, timeout_seconds)
    turn["state"] = TURN_DELIVERING
    turn["turn_index"] = selected_index
    turn["input_line_max"] = max(_line_no(item) for item in pending)
    turn["delivery_payload"] = {
        "message": _format_delivery(pending),
        "from": ",".join(sorted({item.get("from", "") for item in pending if item.get("from")})),
    }
    turn["adapter_type"] = cap.get("type", "")

    def begin_delivery(current):
        if current.get("current_turn"):
            return {"busy": True}
        current["current_turn"] = turn
        current["waiting_for"] = agent_id
        current["waiting_line"] = turn["input_line_max"]
        current["last_error"] = ""
        return {"busy": False}

    _state, outcome = mutate_room_state(shared_dir, room_id, room_cfg, begin_delivery)
    if outcome.get("busy"):
        return {**result, "action": "busy"}

    emit_event(shared_dir, room_id, EVT_TURN_SELECTED, actor=agent_id,
               turn_id=turn["turn_id"], correlation_id=turn["correlation_id"],
               meta={"turn_index": selected_index, "pending": len(pending)})
    return _deliver_turn(config, shared_dir, room_id, room_cfg, agent_cfg, turn, result)


def receive_agent_response(shared_dir, room_id, agent_id, message_text, turn_id="", correlation_id="", source="callback", meta=None):
    """Accept one callback with strict turn validation and idempotency."""
    accepted = {}

    def receive(state):
        current = state.get("current_turn") or {}
        if not current:
            if turn_id or correlation_id:
                return {"ok": False, "error": "stale callback: no active turn", "persist": False}
            msg = append_room_message(shared_dir, room_id, agent_id, message_text, kind="agent", meta={"source": source, **(meta or {})})
            accepted.update({"message_id": msg.get("id", ""), "free": True})
            return {"ok": True, "free": True}
        if current.get("agent_id") != agent_id:
            return {"ok": False, "error": "current turn belongs to {}, not {}".format(current.get("agent_id", ""), agent_id), "persist": False}
        if turn_id and current.get("turn_id") != turn_id:
            return {"ok": False, "error": "turn_id mismatch", "persist": False}
        if correlation_id and current.get("correlation_id") != correlation_id:
            return {"ok": False, "error": "correlation_id mismatch", "persist": False}
        if current.get("response_message_id"):
            return {"ok": True, "duplicate": True, "message_id": current.get("response_message_id", "")}
        msg = append_room_message(
            shared_dir, room_id, agent_id, message_text, kind="agent",
            reply_to=current.get("turn_id", ""), correlation_id=current.get("correlation_id", ""),
            meta={"source": source, **(meta or {})},
        )
        current["response_message_id"] = msg.get("id", "")
        current["state"] = TURN_WAITING_RESPONSE
        state["current_turn"] = current
        state["last_message_id"] = msg.get("id", "")
        accepted.update({"message_id": msg.get("id", ""), "turn": dict(current)})
        return {"ok": True, "duplicate": False}

    state, outcome = mutate_room_state(shared_dir, room_id, None, receive)
    if outcome.get("persist") is False:
        # mutate_room_state persisted only its revision by default; restore no-op semantics on invalid callback.
        # The response is still rejected, and no message was appended.
        return {"ok": False, "error": outcome.get("error", "callback rejected")}
    if not outcome.get("ok"):
        return {"ok": False, "error": outcome.get("error", "callback rejected")}
    if outcome.get("duplicate"):
        return {"ok": True, "duplicate": True, "message_id": outcome.get("message_id", ""), "scheduled": False}

    if not outcome.get("free"):
        current = accepted.get("turn") or {}
        emit_event(shared_dir, room_id, EVT_AGENT_RESPONSE_RECEIVED, actor=agent_id,
                   turn_id=current.get("turn_id", ""), correlation_id=current.get("correlation_id", ""),
                   message_id=accepted.get("message_id", ""), meta={"source": source})
    _schedule(room_id)
    return {"ok": True, "message_id": accepted.get("message_id", ""), "scheduled": True,
            "note": "no active turn" if outcome.get("free") else ""}
