#!/usr/bin/env python3
"""Protocol data structures for Agent Bridge v2.

Defines Message, Event, DeliveryRequest, DeliveryTicket, TurnInfo,
and related constants.  Every module in core/ imports from here
instead of inventing ad-hoc dicts.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

# ── ID generation ──────────────────────────────────────

def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _uid(prefix):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


def gen_message_id():
    return _uid("msg")


def gen_event_id():
    return _uid("evt")


def gen_turn_id():
    return _uid("turn")


def gen_delivery_id():
    return _uid("deliv")


def gen_correlation_id():
    return _uid("corr")


# ── Validations ────────────────────────────────────────

VALID_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_id(value):
    return bool(value and VALID_ID_RE.match(str(value)))


# ── Message kinds ──────────────────────────────────────

MSG_KIND_USER = "user"
MSG_KIND_AGENT = "agent"
MSG_KIND_SYSTEM = "system"
MSG_KIND_EVENT = "event"

MSG_KINDS = {MSG_KIND_USER, MSG_KIND_AGENT, MSG_KIND_SYSTEM, MSG_KIND_EVENT}


# ── Room status ────────────────────────────────────────

ROOM_PAUSED = "paused"
ROOM_RUNNING = "running"
ROOM_ERROR = "error"
ROOM_ARCHIVED = "archived"

ROOM_STATUSES = {ROOM_PAUSED, ROOM_RUNNING, ROOM_ERROR, ROOM_ARCHIVED}


# ── Turn states ────────────────────────────────────────

TURN_IDLE = "idle"
TURN_SELECTING_AGENT = "selecting_agent"
TURN_COLLECTING_PENDING = "collecting_pending"
TURN_DELIVERING = "delivering"
TURN_WAITING_RESPONSE = "waiting_response"
TURN_COMPLETED = "completed"
TURN_TIMEOUT = "timeout"
TURN_FAILED = "failed"
TURN_MANUAL_REQUIRED = "manual_required"
TURN_SKIPPED = "skipped"

TURN_STATES = {
    TURN_IDLE, TURN_SELECTING_AGENT, TURN_COLLECTING_PENDING,
    TURN_DELIVERING, TURN_WAITING_RESPONSE, TURN_COMPLETED,
    TURN_TIMEOUT, TURN_FAILED, TURN_MANUAL_REQUIRED, TURN_SKIPPED,
}


# ── Response modes ────────────────────────────────────

RESPONSE_SYNC = "sync"
RESPONSE_CALLBACK = "callback"
RESPONSE_FILE_OUTBOX = "file_outbox"
RESPONSE_PULL_SESSION = "pull_session"
RESPONSE_MCP_TOOL = "mcp_tool"
RESPONSE_MANUAL = "manual"
RESPONSE_NONE = "none"

RESPONSE_MODES = {
    RESPONSE_SYNC, RESPONSE_CALLBACK, RESPONSE_FILE_OUTBOX,
    RESPONSE_PULL_SESSION, RESPONSE_MCP_TOOL, RESPONSE_MANUAL,
    RESPONSE_NONE,
}


# ── Event types ────────────────────────────────────────

EVT_ROOM_STARTED = "room.started"
EVT_ROOM_PAUSED = "room.paused"
EVT_MESSAGE_CREATED = "message.created"
EVT_TURN_SELECTED = "turn.selected"
EVT_AGENT_WAKEUP_REQUESTED = "agent.wakeup.requested"
EVT_AGENT_WAKEUP_SUCCEEDED = "agent.wakeup.succeeded"
EVT_AGENT_WAKEUP_FAILED = "agent.wakeup.failed"
EVT_AGENT_RESPONSE_RECEIVED = "agent.response.received"
EVT_TURN_COMPLETED = "turn.completed"
EVT_TURN_TIMEOUT = "turn.timeout"
EVT_TURN_SKIPPED = "turn.skipped"
EVT_ROOM_ERROR = "room.error"
EVT_ARCHIVE_CREATED = "archive.created"


# ── Adapter types ──────────────────────────────────────

ADAPTER_NATIVE_HTTP = "native_http"
ADAPTER_OPENCLAW_SESSIONS = "openclaw_sessions"
ADAPTER_CLI = "cli"
ADAPTER_FILE_MAILBOX = "file_mailbox"
ADAPTER_MCP_TOOL = "mcp_tool"
ADAPTER_MANUAL = "manual"

ADAPTER_TYPES = {
    ADAPTER_NATIVE_HTTP, ADAPTER_OPENCLAW_SESSIONS, ADAPTER_CLI,
    ADAPTER_FILE_MAILBOX, ADAPTER_MCP_TOOL, ADAPTER_MANUAL,
}


# ── Policy timeout actions ─────────────────────────────

ON_TIMEOUT_SKIP = "skip"
ON_TIMEOUT_RETRY = "retry"
ON_TIMEOUT_PAUSE = "pause"
ON_TIMEOUT_ERROR = "error"
ON_TIMEOUT_MANUAL = "manual"

ON_TIMEOUT_ACTIONS = {ON_TIMEOUT_SKIP, ON_TIMEOUT_RETRY, ON_TIMEOUT_PAUSE, ON_TIMEOUT_ERROR, ON_TIMEOUT_MANUAL}


# ── Data structures ────────────────────────────────────

def make_message(room, from_agent, msg, to_agent="", kind=MSG_KIND_AGENT,
                 reply_to="", correlation_id="", meta=None):
    """Create a new message dict."""
    record = {
        "id": gen_message_id(),
        "ts": _now_str(),
        "room": room,
        "from": from_agent,
        "kind": kind,
        "msg": msg,
    }
    if to_agent:
        record["to"] = to_agent
    if reply_to:
        record["reply_to"] = reply_to
    if correlation_id:
        record["correlation_id"] = correlation_id
    if meta:
        record["meta"] = meta
    return record


def normalize_message(raw):
    """Normalize a raw message dict (legacy compatibility).

    Old messages have only ts/from/msg.  We add defaults for new fields
    but do NOT modify the original dict.
    """
    if not isinstance(raw, dict):
        return raw
    m = dict(raw)
    m.setdefault("id", "")
    m.setdefault("ts", "")
    m.setdefault("room", "")
    m.setdefault("from", "")
    m.setdefault("to", "")
    m.setdefault("kind", MSG_KIND_AGENT)
    m.setdefault("msg", "")
    m.setdefault("reply_to", "")
    m.setdefault("correlation_id", "")
    m.setdefault("meta", {})
    return m


def make_event(room, event_type, actor="", turn_id="", correlation_id="",
               message_id="", meta=None):
    """Create a new event dict."""
    return {
        "id": gen_event_id(),
        "ts": _now_str(),
        "room": room,
        "type": event_type,
        "actor": actor,
        "turn_id": turn_id,
        "correlation_id": correlation_id,
        "message_id": message_id,
        "meta": meta or {},
    }


def make_turn(room_id, agent_id, input_messages, timeout_seconds=180):
    """Create a new turn info dict."""
    now = _now_str()
    turn_id = gen_turn_id()
    corr_id = gen_correlation_id()
    delivery_id = gen_delivery_id()
    from datetime import datetime as _dt
    timeout_at_dt = _dt.now()
    from datetime import timedelta as _td
    timeout_at_dt = timeout_at_dt + _td(seconds=timeout_seconds)
    return {
        "turn_id": turn_id,
        "agent_id": agent_id,
        "state": TURN_IDLE,
        "delivery_id": delivery_id,
        "correlation_id": corr_id,
        "started_at": now,
        "timeout_at": timeout_at_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "timeout_seconds": timeout_seconds,
        "input_message_ids": [m.get("id", "") for m in input_messages],
        "input_line_max": 0,
        "response_message_id": "",
        "attempts": 1,
        "max_attempts": 2,
        "last_error": "",
    }


def make_delivery_request(room_id, agent_id, turn, message_text, from_agents,
                          callback_url, room_path, active_file, input_messages=None):
    """Create a DeliveryRequest dict."""
    return {
        "room_id": room_id,
        "agent_id": agent_id,
        "turn_id": turn["turn_id"],
        "correlation_id": turn["correlation_id"],
        "message": message_text,
        "from": from_agents,
        "callback_url": callback_url,
        "room_path": room_path,
        "active_file": active_file,
        "input_messages": input_messages or [],
    }


def make_delivery_ticket(ok, delivery_request=None, adapter_type="",
                         response_mode=RESPONSE_CALLBACK, detail="",
                         sync_response="", raw_response="", error=""):
    """Create a DeliveryTicket dict."""
    dr = delivery_request or {}
    return {
        "ok": ok,
        "delivery_id": dr.get("turn_id", "") and gen_delivery_id() or gen_delivery_id(),
        "turn_id": dr.get("turn_id", ""),
        "agent_id": dr.get("agent_id", ""),
        "adapter_type": adapter_type,
        "response_mode": response_mode,
        "correlation_id": dr.get("correlation_id", ""),
        "detail": detail,
        "sync_response": sync_response,
        "raw_response": raw_response,
        "error": error,
    }


def make_capability(adapter_type, configured=True, automatic=True,
                    wake_modes=None, response_modes=None,
                    supports_active_push=False, supports_streaming=False,
                    requires_callback_url=False, health="configured"):
    """Create an adapter capability dict."""
    return {
        "type": adapter_type,
        "configured": configured,
        "automatic": automatic,
        "wake_modes": wake_modes or [],
        "response_modes": response_modes or [],
        "supports_active_push": supports_active_push,
        "supports_streaming": supports_streaming,
        "requires_callback_url": requires_callback_url,
        "health": health,
    }


# ── Default room state ─────────────────────────────────

def default_room_state(room_cfg=None):
    """Create a default room state dict with V2 current_turn structure."""
    room = room_cfg or {}
    return {
        "status": room.get("status", ROOM_PAUSED),
        "policy": room.get("policy", "round_robin"),
        "turn_index": 0,
        "round": 0,
        "turn_count": 0,
        "max_turns": room.get("max_turns", 50),
        "order": room.get("order", []),
        "current_turn": None,
        "last_message_id": "",
        "last_error": "",
        # Legacy compat fields
        "waiting_for": "",
        "waiting_line": 0,
    }


def migrate_room_state(state, room_cfg=None):
    """Migrate a legacy state dict to V2 format.

    - Adds ``current_turn`` if missing.
    - Keeps ``waiting_for`` / ``waiting_line`` for backward compat.
    """
    base = default_room_state(room_cfg)
    merged = {**base, **state}
    # If current_turn is missing but waiting_for exists, build a stub
    if not merged.get("current_turn") and merged.get("waiting_for"):
        merged["current_turn"] = {
            "turn_id": "",
            "agent_id": merged["waiting_for"],
            "state": TURN_WAITING_RESPONSE,
            "started_at": "",
            "timeout_at": "",
            "timeout_seconds": 180,
            "input_message_ids": [],
            "input_line_max": merged.get("waiting_line", 0),
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 2,
            "last_error": "",
        }
    return merged
