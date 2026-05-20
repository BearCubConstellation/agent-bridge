#!/usr/bin/env python3
"""Room-scoped message storage and round-robin runtime."""
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from adapters import adapter_capability, deliver_to_adapter
from lock import file_lock
from poll import ARCHIVE_IDLE_MINUTES, ARCHIVE_MSG_LIMIT, parse_jsonl


VALID_ROOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_room_id(room_id):
    return bool(room_id and VALID_ROOM_ID_RE.match(str(room_id)))


def rooms_root(shared_dir):
    return Path(shared_dir) / "rooms"


def room_dir(shared_dir, room_id):
    if not validate_room_id(room_id):
        raise ValueError(f"invalid room id: {room_id}")
    return rooms_root(shared_dir) / room_id


def room_active_file(shared_dir, room_id):
    return room_dir(shared_dir, room_id) / "active.jsonl"


def normalize_room(room_cfg):
    room = dict(room_cfg or {})
    agents = [str(a) for a in room.get("agents", []) if str(a)]
    seen = set()
    agents = [a for a in agents if not (a in seen or seen.add(a))]

    order = [str(a) for a in room.get("order", []) if str(a)]
    order = [a for a in order if a in agents]
    for aid in agents:
        if aid not in order:
            order.append(aid)

    room["agents"] = agents
    room["order"] = order
    room["policy"] = room.get("policy", "round_robin")
    room["status"] = room.get("status", "paused")
    try:
        room["max_turns"] = int(room.get("max_turns", 50))
    except (TypeError, ValueError):
        room["max_turns"] = 50
    return room


def default_state(room_cfg):
    room = normalize_room(room_cfg)
    return {
        "status": room.get("status", "paused"),
        "turn_index": 0,
        "round": 0,
        "turn_count": 0,
        "order": room.get("order", []),
        "max_turns": room.get("max_turns", 50),
        "last_message_id": "",
        "last_error": "",
        "waiting_for": "",
        "waiting_line": 0,
    }


def ensure_room(shared_dir, room_cfg):
    room = normalize_room(room_cfg)
    rid = room.get("id")
    if not validate_room_id(rid):
        raise ValueError(f"invalid room id: {rid}")
    rdir = room_dir(shared_dir, rid)
    (rdir / "history").mkdir(parents=True, exist_ok=True)
    (rdir / "cursors").mkdir(parents=True, exist_ok=True)
    (rdir / "active.jsonl").touch(exist_ok=True)
    state_path = rdir / "state.json"
    if not state_path.exists():
        write_room_state(shared_dir, rid, default_state(room))
    room_yaml = rdir / "room.json"
    # 向后兼容：如果旧名 room.yaml 已存在则不重新创建
    if not room_yaml.exists() and not (rdir / "room.yaml").exists():
        room_yaml.write_text(json.dumps(room, ensure_ascii=False, indent=2), encoding="utf-8")
    return rdir


def read_room_state(shared_dir, room_id, room_cfg=None):
    rdir = room_dir(shared_dir, room_id)
    state_path = rdir / "state.json"
    if not state_path.exists():
        state = default_state(room_cfg or {"id": room_id})
        return state
    try:
        state = json.loads(state_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        state = {}
    base = default_state(room_cfg or {"id": room_id})
    base.update(state)
    return base


def write_room_state(shared_dir, room_id, state):
    rdir = room_dir(shared_dir, room_id)
    rdir.mkdir(parents=True, exist_ok=True)
    state_path = rdir / "state.json"
    with file_lock(rdir / ".state.lock"):
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def set_room_status(shared_dir, room_cfg, status):
    room = normalize_room(room_cfg)
    ensure_room(shared_dir, room)
    state = read_room_state(shared_dir, room["id"], room)
    state["status"] = status
    if status != "error":
        state["last_error"] = ""
    write_room_state(shared_dir, room["id"], state)
    return state


def _message_id():
    return "msg_" + datetime.now().strftime("%Y%m%d%H%M%S%f")


def append_room_message(shared_dir, room_id, from_agent, text, to_agent="", kind="agent", meta=None):
    active = room_active_file(shared_dir, room_id)
    active.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": _message_id(),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "room": room_id,
        "from": from_agent,
        "msg": text,
    }
    if to_agent:
        record["to"] = to_agent
    if kind:
        record["kind"] = kind
    if meta:
        record["meta"] = meta
    with file_lock(active.parent / ".active.lock"):
        with open(active, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_room_messages(shared_dir, room_id, include_history=False, limit=500):
    active = room_active_file(shared_dir, room_id)
    messages = []
    if include_history:
        hdir = active.parent / "history"
        if hdir.exists():
            for hf in sorted(hdir.iterdir()):
                for msg in parse_jsonl(hf):
                    msg["_source"] = hf.name
                    messages.append(msg)
    for msg in parse_jsonl(active):
        msg["_source"] = "active"
        messages.append(msg)
    messages.sort(key=lambda m: m.get("ts", ""))
    if limit and len(messages) > limit:
        messages = messages[-int(limit):]
    return messages


def _cursor_file(shared_dir, room_id, agent_id):
    return room_dir(shared_dir, room_id) / "cursors" / f"{agent_id}.cursor"


def read_room_cursor(shared_dir, room_id, agent_id):
    path = _cursor_file(shared_dir, room_id, agent_id)
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return 0


def write_room_cursor(shared_dir, room_id, agent_id, line_no):
    path = _cursor_file(shared_dir, room_id, agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(line_no)), encoding="utf-8")


def _line_no(message):
    try:
        return int(message.get("_line", 0))
    except (TypeError, ValueError):
        return 0


def _messages_with_lines(active_file):
    messages = parse_jsonl(active_file)
    for i, msg in enumerate(messages):
        msg["_line"] = i + 1
    return messages


def _addressed_to(message, agent_id):
    target = message.get("to", "")
    if not target:
        return True
    if isinstance(target, list):
        return agent_id in target
    return str(target) == agent_id


def _pending_for_agent(messages, agent_id, cursor):
    pending = []
    for msg in messages:
        if _line_no(msg) <= cursor:
            continue
        if msg.get("from") == agent_id:
            continue
        if not _addressed_to(msg, agent_id):
            continue
        pending.append(msg)
    return pending


def _format_delivery(messages):
    lines = []
    for msg in messages:
        sender = msg.get("from", "")
        text = msg.get("msg", "")
        lines.append(f"[{sender}] {text}" if sender else text)
    return "\n".join(lines)


def _response_seen(messages, agent_id, waiting_line):
    for msg in messages:
        if _line_no(msg) > int(waiting_line or 0) and msg.get("from") == agent_id:
            return msg
    return None


def should_archive_room(active_file):
    messages = parse_jsonl(active_file)
    if not messages:
        return False
    if len(messages) >= ARCHIVE_MSG_LIMIT:
        return True
    last_ts = messages[-1].get("ts", "")
    try:
        last_dt = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (datetime.now() - last_dt).total_seconds() > ARCHIVE_IDLE_MINUTES * 60


def archive_room(shared_dir, room_id):
    active = room_active_file(shared_dir, room_id)
    if not active.exists() or not parse_jsonl(active):
        return None
    rdir = active.parent
    hdir = rdir / "history"
    hdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = hdir / f"{stamp}.jsonl"
    with file_lock(rdir / ".active.lock"):
        shutil.move(str(active), str(dest))
        active.write_text("", encoding="utf-8")
    for cursor in (rdir / "cursors").glob("*.cursor"):
        cursor.write_text("0", encoding="utf-8")
    state = read_room_state(shared_dir, room_id)
    state["turn_index"] = 0
    state["waiting_for"] = ""
    state["waiting_line"] = 0
    write_room_state(shared_dir, room_id, state)
    return dest.name


def tick_room(config, room_id, force=False):
    shared_dir = Path(os.path.expandvars(os.path.expanduser(str(config.get("shared_dir", "~/.agent-bridge")))))
    rooms = config.get("rooms", {})
    if room_id not in rooms:
        return {"ok": False, "room_id": room_id, "error": "room not found", "delivered": False}
    room = normalize_room(rooms[room_id])
    room.setdefault("id", room_id)
    ensure_room(shared_dir, room)
    state = read_room_state(shared_dir, room_id, room)
    state["order"] = room["order"]
    state["max_turns"] = room["max_turns"]

    result = {
        "ok": True,
        "room_id": room_id,
        "delivered": False,
        "waiting_for": state.get("waiting_for", ""),
        "to_agent": "",
        "new_msgs": 0,
        "error": "",
    }
    if state.get("status") != "running" and not force:
        result["ok"] = True
        result["error"] = "room is not running"
        write_room_state(shared_dir, room_id, state)
        return result

    order = room.get("order", [])
    if not order:
        state["last_error"] = "room has no agents"
        write_room_state(shared_dir, room_id, state)
        return {**result, "ok": False, "error": state["last_error"]}

    active = room_active_file(shared_dir, room_id)
    messages = _messages_with_lines(active)

    waiting_for = state.get("waiting_for", "")
    if waiting_for:
        response = _response_seen(messages, waiting_for, state.get("waiting_line", 0))
        if not response:
            result["waiting_for"] = waiting_for
            write_room_state(shared_dir, room_id, state)
            return result
        current_index = order.index(waiting_for) if waiting_for in order else int(state.get("turn_index", 0))
        next_index = (current_index + 1) % len(order)
        state["turn_index"] = next_index
        state["round"] = int(state.get("round", 0)) + (1 if next_index == 0 else 0)
        state["waiting_for"] = ""
        state["waiting_line"] = 0
        state["last_message_id"] = response.get("id", "")
        result["response_seen"] = True
        write_room_state(shared_dir, room_id, state)
        return result

    if int(state.get("turn_count", 0)) >= int(state.get("max_turns", 50)):
        state["status"] = "paused"
        state["last_error"] = "max_turns reached"
        write_room_state(shared_dir, room_id, state)
        return {**result, "ok": True, "error": state["last_error"]}

    turn_index = int(state.get("turn_index", 0)) % len(order)
    agent_id = order[turn_index]
    agents = config.get("agents", {})
    agent_cfg = agents.get(agent_id)
    if not agent_cfg:
        state["status"] = "error"
        state["last_error"] = f"unknown agent: {agent_id}"
        write_room_state(shared_dir, room_id, state)
        return {**result, "ok": False, "to_agent": agent_id, "error": state["last_error"]}

    cursor = read_room_cursor(shared_dir, room_id, agent_id)
    pending = _pending_for_agent(messages, agent_id, cursor)
    result["to_agent"] = agent_id
    result["new_msgs"] = len(pending)
    if not pending:
        write_room_state(shared_dir, room_id, state)
        if should_archive_room(active):
            result["archived"] = archive_room(shared_dir, room_id)
        return result

    text = _format_delivery(pending)
    context = {
        "message": text,
        "from": ",".join(sorted({m.get("from", "") for m in pending if m.get("from")})),
        "to": agent_id,
        "room": room_id,
        "room_path": str(room_dir(shared_dir, room_id)),
        "active_file": str(active),
    }
    cap = adapter_capability(agent_cfg)
    if not cap.get("automatic"):
        state["status"] = "error"
        state["last_error"] = f"agent '{agent_id}' is not auto-triggerable ({cap.get('type')})"
        write_room_state(shared_dir, room_id, state)
        return {**result, "ok": False, "error": state["last_error"]}

    delivered, detail = deliver_to_adapter(agent_cfg, text, context["from"], context)
    if not delivered:
        state["status"] = "error"
        state["last_error"] = detail
        write_room_state(shared_dir, room_id, state)
        return {**result, "ok": False, "error": detail}

    latest_line = max(_line_no(m) for m in pending)
    write_room_cursor(shared_dir, room_id, agent_id, latest_line)
    state["waiting_for"] = agent_id
    # waiting_line 存的是当前消息总数，用于判断后续新消息（行号 > 此值）
    state["waiting_line"] = len(messages)
    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    state["last_error"] = ""
    write_room_state(shared_dir, room_id, state)
    result["ok"] = True
    result["delivered"] = True
    result["waiting_for"] = agent_id
    return result


def tick_running_rooms(config):
    results = []
    shared_dir = Path(os.path.expandvars(os.path.expanduser(str(config.get("shared_dir", "~/.agent-bridge")))))
    for room_id, room_cfg in config.get("rooms", {}).items():
        if not validate_room_id(room_id):
            continue
        room = normalize_room({**room_cfg, "id": room_id})
        ensure_room(shared_dir, room)
        state = read_room_state(shared_dir, room_id, room)
        if state.get("status") == "running":
            results.append(tick_room(config, room_id))
    return results
