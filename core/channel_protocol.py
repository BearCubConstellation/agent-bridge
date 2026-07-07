#!/usr/bin/env python3
"""Versioned wire protocol for Agent Bridge channel clients.

Channel clients transport normal chat messages; model prompts never carry
callback URLs, turn identifiers or transport tokens.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

PROTOCOL_VERSION = 1
VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_TEXT_LENGTH = 50000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return "{}_{}".format(prefix, uuid.uuid4().hex)


def ensure_id(value: Any, field: str) -> str:
    value = str(value or "").strip()
    if not VALID_ID_RE.match(value):
        raise ValueError("invalid {}".format(field))
    return value


def normalize_recipients(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Iterable):
        raise ValueError("to must be a recipient id or list of recipient ids")
    recipients = []
    seen = set()
    for item in value:
        agent_id = ensure_id(item, "recipient")
        if agent_id not in seen:
            recipients.append(agent_id)
            seen.add(agent_id)
    return recipients


def normalize_message(payload: Dict[str, Any], sender: str) -> Dict[str, Any]:
    """Validate a client message and create a canonical channel envelope."""
    if not isinstance(payload, dict):
        raise ValueError("message payload must be an object")
    room_id = ensure_id(payload.get("room_id") or payload.get("roomId"), "room_id")
    text = payload.get("text")
    if text is None and isinstance(payload.get("content"), dict):
        text = payload["content"].get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("message text is required")
    text = text.strip().replace("\x00", "")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError("message exceeds max length ({})".format(MAX_TEXT_LENGTH))
    message_id = str(payload.get("id") or payload.get("message_id") or new_id("chmsg"))
    if len(message_id) > 180:
        raise ValueError("message id is too long")
    reply_to = str(payload.get("reply_to") or payload.get("replyTo") or "").strip()
    trace_id = str(payload.get("trace_id") or payload.get("traceId") or new_id("trace")).strip()
    return {
        "type": "message",
        "protocol": PROTOCOL_VERSION,
        "id": message_id,
        "room_id": room_id,
        "from": ensure_id(sender, "sender"),
        "to": normalize_recipients(payload.get("to")),
        "text": text,
        "reply_to": reply_to,
        "trace_id": trace_id,
        "created_at": utc_now(),
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }


def registration_response(agent_id: str, status: str = "online") -> Dict[str, Any]:
    return {
        "type": "registered",
        "protocol": PROTOCOL_VERSION,
        "agent_id": agent_id,
        "status": status,
        "server_time": utc_now(),
    }


def error_event(code: str, message: str) -> Dict[str, Any]:
    return {"type": "error", "protocol": PROTOCOL_VERSION, "code": code, "message": message}
