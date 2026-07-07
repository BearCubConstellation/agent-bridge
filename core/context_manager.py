#!/usr/bin/env python3
"""Persistent, room-scoped context for AgentBridge.

Room JSONL remains the source of truth. This module stores compact, editable
state alongside it so an Agent can resume after a restart or a model-context
reset without replaying an entire transcript.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable

from lock import file_lock
from rooms import read_room_messages, room_dir


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


def _write(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def ensure_context(shared_dir, room_id: str) -> Path:
    """Create the room context layout lazily without changing message storage."""
    root = room_dir(shared_dir, room_id)
    (root / "agent_memory").mkdir(parents=True, exist_ok=True)
    context_path = root / "context.json"
    sessions_path = root / "sessions.json"
    if not context_path.exists():
        _write(context_path, {
            "scene_id": "",
            "summary": "",
            "shared_facts": [],
            "game_state": {},
            "context_revision": 0,
            "updated_at": _now(),
        })
    if not sessions_path.exists():
        _write(sessions_path, {"sessions": {}, "updated_at": _now()})
    return root


def read_room_context(shared_dir, room_id: str) -> Dict[str, Any]:
    root = ensure_context(shared_dir, room_id)
    return _json(root / "context.json", {
        "scene_id": "", "summary": "", "shared_facts": [],
        "game_state": {}, "context_revision": 0, "updated_at": "",
    })


def update_room_context(shared_dir, room_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    root = ensure_context(shared_dir, room_id)
    path = root / "context.json"
    with file_lock(root / ".context.lock"):
        current = read_room_context(shared_dir, room_id)
        current.update({key: value for key, value in (patch or {}).items() if key != "context_revision"})
        current["context_revision"] = int(current.get("context_revision", 0)) + 1
        current["updated_at"] = _now()
        _write(path, current)
        return current


def read_agent_memory(shared_dir, room_id: str, agent_id: str) -> Dict[str, Any]:
    root = ensure_context(shared_dir, room_id)
    return _json(root / "agent_memory" / (str(agent_id) + ".json"), {
        "agent_id": str(agent_id), "role_memory": "", "private_facts": [],
        "last_summary": "", "updated_at": "",
    })


def update_agent_memory(shared_dir, room_id: str, agent_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    root = ensure_context(shared_dir, room_id)
    path = root / "agent_memory" / (str(agent_id) + ".json")
    with file_lock(root / ".context.lock"):
        current = read_agent_memory(shared_dir, room_id, agent_id)
        current.update(patch or {})
        current["agent_id"] = str(agent_id)
        current["updated_at"] = _now()
        _write(path, current)
        return current


def get_or_create_session(shared_dir, room_id: str, agent_id: str, adapter_type: str) -> Dict[str, Any]:
    """Return one native-session mapping per (room, agent, adapter).

    The adapter may later replace ``native_session_id`` after it creates or
    discovers a real runtime session.
    """
    root = ensure_context(shared_dir, room_id)
    path = root / "sessions.json"
    key = "{}:{}".format(adapter_type, agent_id)
    with file_lock(root / ".context.lock"):
        data = _json(path, {"sessions": {}, "updated_at": ""})
        sessions = data.setdefault("sessions", {})
        entry = sessions.get(key)
        if not isinstance(entry, dict):
            entry = {
                "agent_id": str(agent_id),
                "adapter": str(adapter_type),
                "native_session_id": "",
                "created_at": _now(),
            }
        entry["last_seen_at"] = _now()
        sessions[key] = entry
        data["updated_at"] = _now()
        _write(path, data)
        return dict(entry)


def save_session(shared_dir, room_id: str, agent_id: str, adapter_type: str, native_session_id: str) -> Dict[str, Any]:
    root = ensure_context(shared_dir, room_id)
    path = root / "sessions.json"
    key = "{}:{}".format(adapter_type, agent_id)
    with file_lock(root / ".context.lock"):
        data = _json(path, {"sessions": {}, "updated_at": ""})
        sessions = data.setdefault("sessions", {})
        entry = sessions.get(key) or {"agent_id": str(agent_id), "adapter": str(adapter_type), "created_at": _now()}
        entry["native_session_id"] = str(native_session_id or "")
        entry["last_seen_at"] = _now()
        sessions[key] = entry
        data["updated_at"] = _now()
        _write(path, data)
        return dict(entry)


def build_context_bundle(shared_dir, room_id: str, agent_id: str, recent_limit: int = 12) -> Dict[str, Any]:
    """Build the bounded context that adapters pass to a runtime.

    It intentionally keeps raw history on disk and sends only recent relevant
    messages plus explicit, editable summaries and state.
    """
    room_context = read_room_context(shared_dir, room_id)
    agent_memory = read_agent_memory(shared_dir, room_id, agent_id)
    messages = read_room_messages(shared_dir, room_id, include_history=False, limit=max(1, int(recent_limit)))
    recent = [
        {
            "id": item.get("id", ""),
            "from": item.get("from", ""),
            "to": item.get("to", ""),
            "text": item.get("msg", ""),
            "ts": item.get("ts", ""),
        }
        for item in messages
    ]
    return {
        "room_context": room_context,
        "agent_memory": agent_memory,
        "recent_messages": recent,
        "source": "room_jsonl",
    }
