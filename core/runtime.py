#!/usr/bin/env python3
"""Room Runtime — state machine for turn-based Agent scheduling.

This module implements the core ``run_room_step`` function that drives
the v2 turn state machine for a single room.  It replaces the old
monolithic ``tick_room`` from ``rooms.py``.

Key differences from the old tick_room:
  * Uses ``current_turn`` in state.json with explicit turn states.
  * Generates turn_id / correlation_id for every delivery.
  * Injects callback_url into adapter context.
  * Emits events via EventBus.
  * Supports timeout / retry / skip / manual_required.
  * Does NOT directly call old ``deliver_to_adapter`` — uses adapter layer.
"""
import json
import os
import sys
import time as _time
from datetime import datetime
from pathlib import Path

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from protocol import (                                    # noqa: E402
    ROOM_RUNNING, ROOM_PAUSED, ROOM_ERROR,
    TURN_IDLE, TURN_WAITING_RESPONSE, TURN_COMPLETED,
    TURN_TIMEOUT, TURN_FAILED, TURN_MANUAL_REQUIRED, TURN_SKIPPED,
    RESPONSE_SYNC, RESPONSE_CALLBACK, RESPONSE_FILE_OUTBOX,
    RESPONSE_MCP_TOOL, RESPONSE_NONE, RESPONSE_MANUAL,
    EVT_ROOM_STARTED, EVT_ROOM_PAUSED, EVT_MESSAGE_CREATED,
    EVT_TURN_SELECTED, EVT_AGENT_WAKEUP_REQUESTED,
    EVT_AGENT_WAKEUP_SUCCEEDED, EVT_AGENT_WAKEUP_FAILED,
    EVT_AGENT_RESPONSE_RECEIVED, EVT_TURN_COMPLETED,
    EVT_TURN_TIMEOUT, EVT_TURN_SKIPPED, EVT_ROOM_ERROR,
    make_turn, make_delivery_request, make_delivery_ticket,
    migrate_room_state,
)
from events import emit_event                             # noqa: E402
from rooms import (                                       # noqa: E402
    room_dir, room_active_file, room_log_file,
    normalize_room, ensure_room,
    read_room_state, write_room_state,
    read_room_cursor, write_room_cursor,
    append_room_message,
    _messages_with_lines, _pending_for_agent, _format_delivery,
    _line_no, _extract_reply, _log_tick,
)
from adapters import (                                    # noqa: E402
    adapter_capability, normalize_adapter,
    deliver_via_registry,
)
from poll import parse_jsonl                              # noqa: E402


# ── Helpers ─────────────────────────────────────────────

def _shared_dir(config):
    return Path(os.path.expandvars(os.path.expanduser(
        str(config.get("shared_dir", "~/.agent-bridge")))))


def _callback_base_url(config):
    """Build the base URL for agent callbacks."""
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "127.0.0.1")
    port = server_cfg.get("port", 7899)
    return f"http://{host}:{port}"


def _callback_url(config, room_id, agent_id):
    return f"{_callback_base_url(config)}/api/rooms/{room_id}/agents/{agent_id}/callback"


def _now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(ts_str):
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


# ── Core step function ──────────────────────────────────

def run_room_step(config, room_id):
    """Execute one state-machine step for *room_id*.

    This is the main entry point called by the Scheduler.  It reads
    the current room state, decides what action to take, mutates
    state, and persists changes.

    Returns a result dict with at least ``ok``, ``room_id``, and
    ``action`` keys.
    """
    shared_dir = _shared_dir(config)
    rooms_cfg = config.get("rooms", {})

    if room_id not in rooms_cfg:
        return {"ok": False, "room_id": room_id, "action": "noop", "error": "room not found"}

    room_cfg = normalize_room({**rooms_cfg[room_id], "id": room_id})
    ensure_room(shared_dir, room_cfg)
    state = read_room_state(shared_dir, room_id, room_cfg)
    state = migrate_room_state(state, room_cfg)
    state["order"] = room_cfg.get("order", [])
    state["max_turns"] = room_cfg.get("max_turns", 50)

    result = {"ok": True, "room_id": room_id, "action": "noop"}

    # ── Check room status ──
    if state.get("status") != ROOM_RUNNING:
        return {**result, "action": "noop", "error": "room not running"}

    # ── Check max_turns ──
    if int(state.get("turn_count", 0)) >= int(state.get("max_turns", 50)):
        state["status"] = ROOM_PAUSED
        state["last_error"] = "max_turns reached"
        write_room_state(shared_dir, room_id, state)
        emit_event(shared_dir, room_id, EVT_ROOM_PAUSED, meta={"reason": "max_turns"})
        _log_tick(shared_dir, room_id, "max_turns_reached", "已达到最大轮次，聊天室自动暂停", level="warn")
        return {**result, "action": "paused", "error": "max_turns reached"}

    # ── Check current_turn waiting_response ──
    current_turn = state.get("current_turn")
    if current_turn and current_turn.get("state") == TURN_WAITING_RESPONSE:
        # Check if response received
        resp_msg_id = current_turn.get("response_message_id", "")
        if resp_msg_id:
            return _complete_turn_and_advance(shared_dir, config, room_id, room_cfg, state, current_turn, result)

        # Check timeout
        timeout_at_str = current_turn.get("timeout_at", "")
        if timeout_at_str:
            timeout_at = _parse_ts(timeout_at_str)
            if timeout_at and datetime.now() > timeout_at:
                return _handle_timeout(shared_dir, config, room_id, room_cfg, state, current_turn, result)

        # Still waiting
        return {**result, "action": "waiting", "waiting_for": current_turn.get("agent_id", "")}

    # ── Select next agent ──
    order = room_cfg.get("order", [])
    if not order:
        state["last_error"] = "room has no agents"
        state["status"] = ROOM_ERROR
        write_room_state(shared_dir, room_id, state)
        emit_event(shared_dir, room_id, EVT_ROOM_ERROR, meta={"error": "no agents"})
        return {**result, "action": "error", "error": "no agents"}

    turn_index = int(state.get("turn_index", 0)) % len(order)
    agent_id = order[turn_index]

    # ── Collect pending messages ──
    active = room_active_file(shared_dir, room_id)
    messages = _messages_with_lines(active)
    cursor = read_room_cursor(shared_dir, room_id, agent_id)
    pending = _pending_for_agent(messages, agent_id, cursor)

    if not pending:
        # No pending messages — skip this agent, keep turn
        _log_tick(shared_dir, room_id, "wakeup_skipped", f"未唤醒 {agent_id}：没有待处理的新消息", agent_id=agent_id)
        # Check archive
        from rooms import should_archive_room, archive_room
        if should_archive_room(active):
            archive_room(shared_dir, room_id)
        write_room_state(shared_dir, room_id, state)
        return {**result, "action": "no_pending", "to_agent": agent_id}

    # ── Get agent config and check capability ──
    agents_cfg = config.get("agents", {})
    agent_cfg = agents_cfg.get(agent_id)
    if not agent_cfg:
        state["status"] = ROOM_ERROR
        state["last_error"] = f"unknown agent: {agent_id}"
        write_room_state(shared_dir, room_id, state)
        emit_event(shared_dir, room_id, EVT_ROOM_ERROR, actor=agent_id, meta={"error": "unknown agent"})
        return {**result, "action": "error", "error": f"unknown agent: {agent_id}"}

    cap = adapter_capability(agent_cfg)
    if not cap.get("automatic"):
        # Manual agent — enter manual_required
        state["current_turn"] = {
            "turn_id": "",
            "agent_id": agent_id,
            "state": TURN_MANUAL_REQUIRED,
            "started_at": _now_ts(),
            "timeout_at": "",
            "timeout_seconds": 0,
            "input_message_ids": [m.get("id", "") for m in pending],
            "input_line_max": max(_line_no(m) for m in pending) if pending else 0,
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 1,
            "last_error": "manual agent, cannot auto-trigger",
        }
        write_room_state(shared_dir, room_id, state)
        _log_tick(shared_dir, room_id, "manual_required", f"{agent_id} 需要手动介入", level="warn", agent_id=agent_id)
        return {**result, "action": "manual_required", "to_agent": agent_id}

    # ── Create turn ──
    # Determine timeout from adapter config
    adapter = normalize_adapter(agent_cfg)
    response_cfg = agent_cfg.get("adapter", {}).get("response", {})
    if not response_cfg:
        # Try top-level wakeup config for timeout
        response_cfg = agent_cfg.get("wakeup", {})
    timeout_seconds = int(response_cfg.get("timeout_seconds", 180))

    turn = make_turn(room_id, agent_id, pending, timeout_seconds)
    turn["state"] = TURN_WAITING_RESPONSE
    turn["input_line_max"] = max(_line_no(m) for m in pending) if pending else 0

    state["current_turn"] = turn
    state["waiting_for"] = agent_id
    state["waiting_line"] = len(messages)
    write_room_state(shared_dir, room_id, state)

    emit_event(shared_dir, room_id, EVT_TURN_SELECTED, actor=agent_id,
               turn_id=turn["turn_id"], correlation_id=turn["correlation_id"],
               meta={"turn_index": turn_index, "pending": len(pending)})

    # ── Build delivery context ──
    text = _format_delivery(pending)
    callback_url = _callback_url(config, room_id, agent_id)
    from_agents = ",".join(sorted({m.get("from", "") for m in pending if m.get("from")}))

    context = {
        "message": text,
        "from": from_agents,
        "to": agent_id,
        "room": room_id,
        "room_path": str(room_dir(shared_dir, room_id)),
        "active_file": str(active),
        "turn_id": turn["turn_id"],
        "correlation_id": turn["correlation_id"],
        "callback_url": callback_url,
    }

    # ── Deliver to adapter ──
    emit_event(shared_dir, room_id, EVT_AGENT_WAKEUP_REQUESTED, actor=agent_id,
               turn_id=turn["turn_id"], correlation_id=turn["correlation_id"])

    _log_tick(shared_dir, room_id, "delivery_attempt", f"准备唤醒/调用 {agent_id}，待投递消息 {len(pending)} 条",
              agent_id=agent_id, meta={"adapter": cap.get("type"), "from": from_agents, "new_msgs": len(pending)})

    t0 = _time.monotonic()
    ticket = deliver_via_registry(agent_cfg, text, from_agents, context)
    elapsed = round(_time.monotonic() - t0, 2)

    delivered = ticket.get("ok", False)
    detail = ticket.get("detail", ticket.get("error", ""))
    response_body = ticket.get("sync_response", "")
    response_mode = ticket.get("response_mode", RESPONSE_CALLBACK)

    if not delivered:
        # Delivery failed
        state["status"] = ROOM_ERROR
        state["last_error"] = detail
        turn["state"] = TURN_FAILED
        turn["last_error"] = detail
        state["current_turn"] = turn
        write_room_state(shared_dir, room_id, state)
        emit_event(shared_dir, room_id, EVT_AGENT_WAKEUP_FAILED, actor=agent_id,
                   turn_id=turn["turn_id"], meta={"error": detail, "elapsed": elapsed})
        _log_tick(shared_dir, room_id, "delivery_failed", f"调用 {agent_id} 失败（{elapsed}s）：{detail}",
                  level="error", agent_id=agent_id)
        return {**result, "action": "delivery_failed", "to_agent": agent_id, "error": detail}

    # ── Delivery succeeded ──
    emit_event(shared_dir, room_id, EVT_AGENT_WAKEUP_SUCCEEDED, actor=agent_id,
               turn_id=turn["turn_id"], meta={"detail": detail, "elapsed": elapsed})

    # Update cursor
    latest_line = max(_line_no(m) for m in pending) if pending else 0
    write_room_cursor(shared_dir, room_id, agent_id, latest_line)
    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    state["last_error"] = ""

    # ── Handle response based on response_mode ──
    if response_mode == RESPONSE_SYNC:
        reply_text = _extract_reply(response_body)
        if reply_text:
            # Sync response — write message and complete turn
            msg = append_room_message(shared_dir, room_id, agent_id, reply_text,
                                       kind="agent", meta={"source": "sync_response"})
            turn["state"] = TURN_COMPLETED
            turn["response_message_id"] = msg.get("id", "")
            state["current_turn"] = turn
            state["last_message_id"] = msg.get("id", "")

            emit_event(shared_dir, room_id, EVT_AGENT_RESPONSE_RECEIVED, actor=agent_id,
                       turn_id=turn["turn_id"], correlation_id=turn["correlation_id"],
                       message_id=msg.get("id", ""), meta={"source": "sync", "elapsed": elapsed})

            # Advance turn
            next_index = (turn_index + 1) % len(order)
            state["turn_index"] = next_index
            state["round"] = int(state.get("round", 0)) + (1 if next_index == 0 else 0)
            state["waiting_for"] = ""
            state["waiting_line"] = 0
            state["current_turn"] = None
            write_room_state(shared_dir, room_id, state)

            emit_event(shared_dir, room_id, EVT_TURN_COMPLETED, actor=agent_id,
                       turn_id=turn["turn_id"], meta={"next_turn_index": next_index})

            _log_tick(shared_dir, room_id, "delivery_succeeded",
                      f"已成功唤醒 {agent_id}（{elapsed}s）并收到同步回复",
                      agent_id=agent_id, meta={"cursor": latest_line, "elapsed": elapsed})

            # Schedule next step
            try:
                from scheduler import get_scheduler
                get_scheduler().schedule_room(room_id)
            except Exception:
                pass

            return {**result, "action": "sync_response", "to_agent": agent_id,
                    "delivered": True, "response_auto_written": True}

    elif response_mode == RESPONSE_MCP_TOOL:
        # MCP tool response: instructions rendered, but not a real agent reply.
        # Enter waiting_response — external system will invoke callback.
        turn["state"] = TURN_WAITING_RESPONSE
        state["current_turn"] = turn
        state["waiting_for"] = agent_id
        state["waiting_line"] = len(messages)
        write_room_state(shared_dir, room_id, state)

        _log_tick(shared_dir, room_id, "delivery_succeeded",
                  f"已成功投递 MCP 工具指令到 {agent_id}（{elapsed}s），等待外部回调",
                  agent_id=agent_id, meta={"cursor": latest_line, "elapsed": elapsed,
                                           "response_mode": response_mode})

        return {**result, "action": "waiting", "to_agent": agent_id,
                "delivered": True, "waiting_for": agent_id}

    else:
        # callback, file_outbox, pull_session, manual, none:
        # No sync response — enter waiting_response
        turn["state"] = TURN_WAITING_RESPONSE
        state["current_turn"] = turn
        state["waiting_for"] = agent_id
        state["waiting_line"] = len(messages)
        write_room_state(shared_dir, room_id, state)

        _log_tick(shared_dir, room_id, "delivery_succeeded",
                  f"已成功唤醒 {agent_id}（{elapsed}s），等待异步回复",
                  agent_id=agent_id, meta={"cursor": latest_line, "elapsed": elapsed,
                                           "response_mode": response_mode})

        return {**result, "action": "waiting", "to_agent": agent_id,
                "delivered": True, "waiting_for": agent_id}


# ── Turn completion ─────────────────────────────────────

def _complete_turn_and_advance(shared_dir, config, room_id, room_cfg, state, turn, result):
    """Complete a turn that has received a response and advance to next agent."""
    order = room_cfg.get("order", [])
    agent_id = turn.get("agent_id", "")

    turn["state"] = TURN_COMPLETED
    turn_index = int(state.get("turn_index", 0)) % len(order) if order else 0

    next_index = (turn_index + 1) % len(order) if order else 0
    state["turn_index"] = next_index
    state["round"] = int(state.get("round", 0)) + (1 if next_index == 0 else 0)
    state["waiting_for"] = ""
    state["waiting_line"] = 0
    state["current_turn"] = None
    state["last_error"] = ""

    emit_event(shared_dir, room_id, EVT_TURN_COMPLETED, actor=agent_id,
               turn_id=turn.get("turn_id", ""), meta={"next_turn_index": next_index})

    write_room_state(shared_dir, room_id, state)

    _log_tick(shared_dir, room_id, "response_seen",
              f"已检测到 {agent_id} 的回复，下一轮将进入后续 Agent",
              agent_id=agent_id, meta={"message_id": turn.get("response_message_id", "")})

    # Schedule next step
    try:
        from scheduler import get_scheduler
        get_scheduler().schedule_room(room_id)
    except Exception:
        pass

    return {**result, "action": "response_received", "to_agent": agent_id}


# ── Timeout handling ────────────────────────────────────

def _handle_timeout(shared_dir, config, room_id, room_cfg, state, turn, result):
    """Handle a timed-out turn based on room policy."""
    agent_id = turn.get("agent_id", "")
    order = room_cfg.get("order", [])
    turn_index = int(state.get("turn_index", 0)) % len(order) if order else 0

    # Get timeout policy from room config
    policy = room_cfg.get("policy", {})
    if isinstance(policy, dict):
        on_timeout = policy.get("on_timeout", "skip")
    else:
        on_timeout = "skip"

    emit_event(shared_dir, room_id, EVT_TURN_TIMEOUT, actor=agent_id,
               turn_id=turn.get("turn_id", ""), meta={"on_timeout": on_timeout})

    _log_tick(shared_dir, room_id, "turn_timeout",
              f"{agent_id} 超时未回复，执行策略：{on_timeout}",
              level="warn", agent_id=agent_id)

    if on_timeout == "skip":
        return _skip_turn(shared_dir, room_id, order, state, turn, turn_index, result)
    elif on_timeout == "retry":
        # Check retry limit
        attempts = int(turn.get("attempts", 1))
        max_attempts = int(turn.get("max_attempts", 2))
        if attempts < max_attempts:
            turn["attempts"] = attempts + 1
            turn["state"] = TURN_WAITING_RESPONSE
            # Reset timeout
            timeout_seconds = int(turn.get("timeout_seconds", 180))
            from datetime import timedelta
            turn["timeout_at"] = (datetime.now() + timedelta(seconds=timeout_seconds)).strftime("%Y-%m-%d %H:%M:%S")
            state["current_turn"] = turn
            write_room_state(shared_dir, room_id, state)
            _log_tick(shared_dir, room_id, "turn_retry", f"重试 {agent_id}（第 {attempts + 1} 次）", agent_id=agent_id)
            # Re-deliver
            return run_room_step(config, room_id)
        else:
            return _skip_turn(shared_dir, room_id, order, state, turn, turn_index, result)
    elif on_timeout == "pause":
        state["status"] = ROOM_PAUSED
        state["current_turn"] = None
        state["waiting_for"] = ""
        state["waiting_line"] = 0
        state["last_error"] = f"turn timeout: {agent_id}"
        write_room_state(shared_dir, room_id, state)
        emit_event(shared_dir, room_id, EVT_ROOM_PAUSED, meta={"reason": "timeout"})
        return {**result, "action": "paused", "error": f"timeout: {agent_id}"}
    elif on_timeout == "error":
        state["status"] = ROOM_ERROR
        state["current_turn"] = None
        state["last_error"] = f"turn timeout: {agent_id}"
        write_room_state(shared_dir, room_id, state)
        emit_event(shared_dir, room_id, EVT_ROOM_ERROR, meta={"error": "timeout"})
        return {**result, "action": "error", "error": f"timeout: {agent_id}"}
    elif on_timeout == "manual":
        turn["state"] = TURN_MANUAL_REQUIRED
        turn["last_error"] = "timeout, manual intervention required"
        state["current_turn"] = turn
        write_room_state(shared_dir, room_id, state)
        return {**result, "action": "manual_required", "to_agent": agent_id}
    else:
        return _skip_turn(shared_dir, room_id, order, state, turn, turn_index, result)


def _skip_turn(shared_dir, room_id, order, state, turn, turn_index, result):
    """Skip the current turn and advance to next agent."""
    agent_id = turn.get("agent_id", "")
    turn["state"] = TURN_SKIPPED

    next_index = (turn_index + 1) % len(order) if order else 0
    state["turn_index"] = next_index
    state["round"] = int(state.get("round", 0)) + (1 if next_index == 0 else 0)
    state["waiting_for"] = ""
    state["waiting_line"] = 0
    state["current_turn"] = None
    state["last_error"] = ""

    emit_event(shared_dir, room_id, EVT_TURN_SKIPPED, actor=agent_id,
               turn_id=turn.get("turn_id", ""), meta={"next_turn_index": next_index})

    write_room_state(shared_dir, room_id, state)

    _log_tick(shared_dir, room_id, "turn_skipped", f"已跳过 {agent_id}", agent_id=agent_id)

    # Schedule next step
    try:
        from scheduler import get_scheduler
        get_scheduler().schedule_room(room_id)
    except Exception:
        pass

    return {**result, "action": "skipped", "to_agent": agent_id}


# ── Receive agent response (called by callback API / MCP / file_outbox watcher) ──

def receive_agent_response(shared_dir, room_id, agent_id, message_text,
                           turn_id="", correlation_id="", source="callback", meta=None):
    """Process an incoming response from an Agent.

    This is the unified entry point for all response channels:
    HTTP callback, MCP reply_turn, file_outbox watcher.

    Returns a result dict with ok / message_id / scheduled.
    """
    rooms_cfg = {}  # Not needed here, we read state directly
    state = read_room_state(shared_dir, room_id)
    state = migrate_room_state(state)

    current_turn = state.get("current_turn")
    if not current_turn:
        # No active turn — write as a free agent message
        msg = append_room_message(shared_dir, room_id, agent_id, message_text,
                                   kind="agent", meta={"source": source, **(meta or {})})
        return {"ok": True, "message_id": msg.get("id", ""), "scheduled": False, "note": "no active turn"}

    # Validate turn
    if current_turn.get("agent_id") != agent_id:
        return {"ok": False, "error": f"current turn belongs to {current_turn.get('agent_id')}, not {agent_id}"}

    if turn_id and current_turn.get("turn_id") != turn_id:
        return {"ok": False, "error": "turn_id mismatch"}

    if correlation_id and current_turn.get("correlation_id") != correlation_id:
        return {"ok": False, "error": "correlation_id mismatch"}

    # Write message
    msg = append_room_message(shared_dir, room_id, agent_id, message_text,
                               kind="agent",
                               reply_to=current_turn.get("turn_id", ""),
                               correlation_id=current_turn.get("correlation_id", ""),
                               meta={"source": source, **(meta or {})})

    # Mark response received
    current_turn["response_message_id"] = msg.get("id", "")
    state["current_turn"] = current_turn
    state["last_message_id"] = msg.get("id", "")
    write_room_state(shared_dir, room_id, state)

    # Emit event
    emit_event(shared_dir, room_id, EVT_AGENT_RESPONSE_RECEIVED, actor=agent_id,
               turn_id=current_turn.get("turn_id", ""),
               correlation_id=current_turn.get("correlation_id", ""),
               message_id=msg.get("id", ""), meta={"source": source})

    # Schedule next step
    try:
        from scheduler import get_scheduler
        get_scheduler().schedule_room(room_id)
        scheduled = True
    except Exception:
        scheduled = False

    return {"ok": True, "message_id": msg.get("id", ""), "scheduled": scheduled}
