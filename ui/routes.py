#!/usr/bin/env python3
"""
Agent Bridge — API 路由处理函数

每个函数对应 BridgeHandler 的一个方法，以 handler 实例作为第一个参数。
"""
import json
import logging
import re
import sys
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # ui/ itself for intra-package imports
from lock import file_lock
from poll import do_archive, parse_jsonl, wakeup_agent
from rooms import (
    append_room_message,
    append_room_log,
    ensure_room,
    normalize_room,
    read_room_logs,
    read_room_messages,
    read_room_state,
    set_room_status,
    write_room_state,
    validate_room_id,
)
from send import validate_agent_id
from adapters import adapter_capability, adapter_to_wakeup, normalize_adapter, wakeup_to_adapter
from protocol import (
    gen_message_id, gen_turn_id, gen_correlation_id,
    make_message, migrate_room_state,
    EVT_ROOM_STARTED, EVT_ROOM_PAUSED, EVT_MESSAGE_CREATED,
    ROOM_RUNNING,
)
from events import emit_event, read_events
from scheduler import get_scheduler
from runtime import run_room_step, receive_agent_response
from security import (
    verify_callback_token, extract_token_from_request,
    agent_in_room, sanitize_message,
)

from config import (
    BRIDGE_FILENAME,
    DEFAULT_POLL_INTERVAL,
    is_relative_to,
    open_in_file_manager,
    resolve_chat_file,
    read_bridge,
    write_bridge,
    generate_room_id,
    sync_filter_from,
    room_runtime_status,
    room_label,
    append_room_log_safely,
    running_rooms_using_agents,
    remove_agents_from_rooms,
    rename_agent_in_rooms,
    rename_cursor,
    VALID_ID_RE,
)
from discovery import (
    discover_local_agents,
    _classify_conn_error,
    _probe_http_reachable,
)


# ═══════════════════════════════════════════════════════════
#  Static file serving
# ═══════════════════════════════════════════════════════════

CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)


def serve_static(handler, filename):
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    if filename in ("favicon.ico", "pageIcon.ico"):
        static_root = project_dir / "icon"
        filepath = (static_root / "pageIcon.ico").resolve()
    elif filename.startswith("icon/"):
        static_root = project_dir / "icon"
        filepath = (project_dir / filename).resolve()
    else:
        static_root = script_dir
        filepath = (script_dir / filename).resolve()
    # 防止路径遍历：确保解析后的路径仍在允许的静态目录下
    if not is_relative_to(filepath, static_root.resolve()):
        handler.send_error(403)
        return
    if not filepath.exists():
        handler.send_error(404)
        return
    ext = filepath.suffix.lower()
    ctype = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    handler.send_header("Content-Security-Policy", CSP_HEADER)
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    with open(filepath, "rb") as f:
        handler.wfile.write(f.read())


# ═══════════════════════════════════════════════════════════
#  GET handlers
# ═══════════════════════════════════════════════════════════

def handle_get_config(handler):
    shared = Path(handler.shared_dir)
    cfg, _ = read_bridge(shared)
    agents_list = []
    for key, a in cfg.get("agents", {}).items():
        agents_list.append({
            "id": a.get("id", key),
            "display_name": a.get("display_name", key.capitalize()),
            "color": a.get("color", "#8888a0"),
            "cursor": a.get("cursor", "line"),
            "filter_from": a.get("filter_from", ""),
            "sample": bool(a.get("sample")),
            "wakeup": a.get("wakeup", {}),
            "adapter": normalize_adapter(a),
            "capability": adapter_capability(a),
        })
    rooms_list = []
    for key, r in cfg.get("rooms", {}).items():
        room = normalize_room({**r, "id": r.get("id", key)})
        state = read_room_state(shared, room["id"], room)
        meta = room.get("meta") or {}
        rooms_list.append({
            "id": room["id"],
            "name": room.get("name", ""),
            "agents": room.get("agents", []),
            "order": room.get("order", []),
            "policy": room.get("policy", "round_robin"),
            "status": state.get("status", room.get("status", "paused")),
            "state": state,
            "max_turns": room.get("max_turns", 50),
            "created_at": room.get("created_at", ""),
            "temporary": bool(meta.get("temporary")),
        })
    _send_json(handler, {
        "ok": True,
        "shared_dir": str(shared),
        "agent_id": cfg.get("agent_id", ""),
        "agents": agents_list,
        "rooms": rooms_list,
        "active_exists": (shared / "active.jsonl").exists(),
    })


def handle_discover_agents(handler):
    shared = Path(handler.shared_dir)
    cfg, _ = read_bridge(shared)
    configured = {a.get("id", key) for key, a in cfg.get("agents", {}).items()}
    discovered = discover_local_agents(shared)
    for item in discovered:
        item["configured"] = item["id"] in configured
    _send_json(handler, {
        "ok": True,
        "agents": discovered,
        "count": len(discovered),
    })


def handle_get_rooms(handler):
    shared = Path(handler.shared_dir)
    cfg, _ = read_bridge(shared)
    rooms_list = []
    for key, r in cfg.get("rooms", {}).items():
        room = normalize_room({**r, "id": r.get("id", key)})
        state = read_room_state(shared, room["id"], room)
        meta = room.get("meta") or {}
        rooms_list.append({
            "id": room["id"],
            "name": room.get("name", ""),
            "agents": room.get("agents", []),
            "order": room.get("order", []),
            "policy": room.get("policy", "round_robin"),
            "status": state.get("status", room.get("status", "paused")),
            "state": state,
            "max_turns": room.get("max_turns", 50),
            "created_at": room.get("created_at", ""),
            "temporary": bool(meta.get("temporary")),
        })
    agents_brief = [
        {"id": a["id"], "display_name": a.get("display_name", a["id"]),
         "color": a.get("color", "#8888a0"), "capability": adapter_capability(a)}
        for a in cfg.get("agents", {}).values()
    ]
    _send_json(handler, {"ok": True, "rooms": rooms_list, "agents": agents_brief})


def handle_messages(handler, query):
    params = urllib.parse.parse_qs(query)
    archive = params.get("archive", [None])[0]
    search = params.get("q", [None])[0]
    room_id = params.get("room", [None])[0]
    try:
        limit = int(params.get("limit", [500])[0])
    except (ValueError, TypeError):
        limit = 500
    shared = Path(handler.shared_dir)
    all_msgs = []

    for m in parse_jsonl(shared / "active.jsonl"):
        m["_source"] = "active"
        all_msgs.append(m)

    if archive:
        archive_path = (shared / "history" / archive).resolve()
        if not is_relative_to(archive_path, (shared / "history").resolve()):
            _send_json(handler, {"ok": False, "error": "invalid archive name"})
            return
        for m in parse_jsonl(archive_path):
            m["_source"] = archive
            all_msgs.append(m)
    else:
        hdir = shared / "history"
        if hdir.exists():
            for hf in sorted(hdir.iterdir(), reverse=True)[:3]:
                for m in parse_jsonl(hf):
                    m["_source"] = hf.name
                    all_msgs.append(m)

    if room_id:
        cfg, _ = read_bridge(shared)
        room = cfg.get("rooms", {}).get(room_id)
        if room:
            room_msgs = read_room_messages(shared, room_id, include_history=bool(archive), limit=limit)
            legacy_agents = set(room.get("agents", []))
            legacy_msgs = [m for m in all_msgs if m.get("from") in legacy_agents]
            all_msgs = room_msgs + legacy_msgs

    if search:
        q = search.lower()
        all_msgs = [m for m in all_msgs if q in m.get("msg", "").lower()]
    all_msgs.sort(key=lambda m: m.get("ts", ""))
    if limit and len(all_msgs) > limit:
        all_msgs = all_msgs[-limit:]

    _send_json(handler, {"ok": True, "count": len(all_msgs), "messages": all_msgs})


def handle_status(handler):
    shared = Path(handler.shared_dir)
    active = shared / "active.jsonl"
    hdir = shared / "history"

    active_msgs = parse_jsonl(active)
    history_files = []
    if hdir.exists():
        for hf in sorted(hdir.iterdir(), reverse=True):
            msgs = parse_jsonl(hf)
            history_files.append({
                "name": hf.name,
                "size": hf.stat().st_size,
                "count": len(msgs),
                "modified": datetime.fromtimestamp(hf.stat().st_mtime)
                    .strftime("%Y-%m-%d %H:%M:%S"),
            })

    _send_json(handler, {
        "ok": True,
        "active": {
            "size": active.stat().st_size if active.exists() else 0,
            "count": len(active_msgs),
            "path": str(active),
        },
        "history": history_files,
        "history_count": len(history_files),
    })


def handle_history(handler, path):
    filename = path.replace("/api/history/", "")
    if not filename.endswith(".jsonl"):
        handler.send_error(400, "Only .jsonl files")
        return
    shared = Path(handler.shared_dir)
    filepath = (shared / "history" / filename).resolve()
    if not is_relative_to(filepath, (shared / "history").resolve()):
        handler.send_error(403)
        return
    if not filepath.exists():
        handler.send_error(404)
        return
    msgs = parse_jsonl(filepath)
    _send_json(handler, {"ok": True, "name": filename, "count": len(msgs), "messages": msgs})


def handle_bridge_yaml(handler):
    shared = Path(handler.shared_dir)
    config_path = shared / BRIDGE_FILENAME
    if not config_path.exists():
        _send_json(handler, {"ok": False, "error": "bridge.yaml not found"})
        return
    text = config_path.read_text(encoding="utf-8")
    _send_json(handler, {"ok": True, "yaml": text})


# ═══════════════════════════════════════════════════════════
#  POST / PUT handlers
# ═══════════════════════════════════════════════════════════

def handle_update_config(handler):
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    shared = Path(handler.shared_dir)
    cfg, config_path = read_bridge(shared)
    new_agents_input = body.get("agents", [])
    changes = []

    # Validate IDs
    errors = [f"invalid ID: '{a.get('id', '')}'"
              for a in new_agents_input
              if not validate_agent_id(a.get("id", "").strip())]
    if errors:
        _send_json(handler, {"ok": False, "error": "; ".join(errors)})
        return
    new_ids = [a.get("id", "").strip() for a in new_agents_input]
    if len(set(new_ids)) != len(new_ids):
        _send_json(handler, {"ok": False, "error": "duplicate agent id"})
        return

    old_lookup = {}
    for key, a in cfg.get("agents", {}).items():
        old_lookup[a["id"]] = (key, a)

    for item in new_agents_input:
        new_id = item.get("id", "").strip()
        old_id = item.get("old_id", "")
        display_name = item.get("display_name", "").strip()
        color = item.get("color", "").strip()

        if old_id and old_id != new_id and old_id in old_lookup:
            blockers = running_rooms_using_agents(shared, cfg, {old_id})
            if blockers:
                rooms = ", ".join(room_label(room) for room in blockers)
                _send_json(handler, {
                    "ok": False,
                    "error": f"agent '{old_id}' is used by running room(s): {rooms}",
                })
                return
            old_key, old_agent = old_lookup[old_id]
            if old_key in cfg["agents"]:
                del cfg["agents"][old_key]
            old_agent["id"] = new_id
            cfg["agents"][new_id] = old_agent
            cursor_moved = rename_cursor(shared, old_id, new_id)
            rename_agent_in_rooms(shared, cfg, old_id, new_id)
            if cfg.get("agent_id") == old_id:
                cfg["agent_id"] = new_id
            changes.append(f"renamed: {old_id} → {new_id}" + (" (cursor)" if cursor_moved else ""))

        target_key = None
        if old_id and old_id != new_id and old_id in old_lookup:
            target_key = new_id
        elif new_id in old_lookup:
            target_key = old_lookup[new_id][0]
        elif new_id in cfg.get("agents", {}):
            target_key = new_id

        if target_key and target_key in cfg["agents"]:
            agent = cfg["agents"][target_key]
            if display_name:
                agent["display_name"] = display_name
            if color and re.match(r'^#[0-9a-fA-F]{6}$', color):
                agent["color"] = color

    write_bridge(config_path, cfg)
    agents_list = [{"id": a["id"], "display_name": a.get("display_name", a["id"]),
                    "color": a.get("color", "#8888a0")}
                   for a in cfg["agents"].values()]
    _send_json(handler, {"ok": True, "agents": agents_list, "changes": changes})


def handle_update_config_full(handler):
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    shared = Path(handler.shared_dir)
    cfg, config_path = read_bridge(shared)

    # Shared dir
    if body.get("shared_dir"):
        cfg["shared_dir"] = body["shared_dir"]
    if body.get("agent_id"):
        cfg["agent_id"] = body["agent_id"]

    saved_agents = []
    # Agents — 支持传入空列表来清空
    if "agents" in body:
        new_agents_list = body["agents"]
        if new_agents_list:
            errors = [f"invalid ID: '{a.get('id', '')}'"
                      for a in new_agents_list
                      if not validate_agent_id(a.get("id", "").strip())]
            if errors:
                _send_json(handler, {"ok": False, "error": "; ".join(errors)})
                return
            new_ids = [a.get("id", "").strip() for a in new_agents_list]
            if len(set(new_ids)) != len(new_ids):
                _send_json(handler, {"ok": False, "error": "duplicate agent id"})
                return

            rename_map = {}
            for a in new_agents_list:
                aid = a.get("id", "").strip()
                old_id = a.get("old_id", "").strip()
                if old_id and old_id != aid and old_id in cfg.get("agents", {}):
                    rename_map[old_id] = aid
            blockers = []
            for old_id in rename_map:
                blockers.extend(running_rooms_using_agents(shared, cfg, {old_id}))
            if blockers:
                rooms = ", ".join(room_label(room) for room in blockers)
                _send_json(handler, {
                    "ok": False,
                    "error": f"agent rename blocked by running room(s): {rooms}",
                })
                return
            for old_id, aid in rename_map.items():
                rename_agent_in_rooms(shared, cfg, old_id, aid)
                if cfg.get("agent_id") == old_id:
                    cfg["agent_id"] = aid

            agents_dict = {}
            for a in new_agents_list:
                aid = a["id"].strip()
                entry = {
                    "id": aid,
                    "display_name": a.get("display_name", aid).strip() or aid,
                    "color": a.get("color", "#8888a0").strip(),
                    "cursor": a.get("cursor", "line"),
                    "filter_from": a.get("filter_from", ""),
                }
                # Wakeup
                wu = a.get("wakeup", {})
                wakeup = {
                    "url": wu.get("url", ""),
                    "method": wu.get("method", "POST"),
                    "headers": wu.get("headers", {"Content-Type": "application/json"}),
                    "body_template": wu.get("body_template", {"message": "{{message}}"}),
                }
                # Auth (optional)
                auth = wu.get("auth")
                if auth and auth.get("type") == "bearer":
                    if auth.get("token_path"):
                        wakeup["auth"] = {
                            "type": "bearer",
                            "token_path": auth["token_path"],
                            "token_jsonpath": auth.get("token_jsonpath", ""),
                        }
                    elif auth.get("token_env"):
                        wakeup["auth"] = {
                            "type": "bearer",
                            "token_env": auth["token_env"],
                        }
                entry["wakeup"] = wakeup
                entry["adapter"] = a.get("adapter") or wakeup_to_adapter(wakeup)
                agents_dict[aid] = entry

            old_ids = set(cfg.get("agents", {}).keys())
            removed_ids = old_ids - set(rename_map.keys()) - set(agents_dict.keys())
            blockers = running_rooms_using_agents(shared, cfg, removed_ids)
            if blockers:
                rooms = ", ".join(room_label(room) for room in blockers)
                _send_json(handler, {
                    "ok": False,
                    "error": f"agent delete blocked by running room(s): {rooms}",
                })
                return
            remove_agents_from_rooms(shared, cfg, removed_ids)
            cfg["agents"] = agents_dict
            saved_agents = list(agents_dict.keys())
        else:
            # 传入空列表：清空 agents
            removed_ids = set(cfg.get("agents", {}).keys())
            blockers = running_rooms_using_agents(shared, cfg, removed_ids)
            if blockers:
                rooms = ", ".join(room_label(room) for room in blockers)
                _send_json(handler, {
                    "ok": False,
                    "error": f"agent delete blocked by running room(s): {rooms}",
                })
                return
            remove_agents_from_rooms(shared, cfg, removed_ids)
            cfg["agents"] = {}
            saved_agents = []

    sync_filter_from(cfg)
    if cfg.get("agent_id") not in cfg.get("agents", {}):
        cfg["agent_id"] = next(iter(cfg.get("agents", {}).keys()), "")
    write_bridge(config_path, cfg)
    saved_agents_out = saved_agents if "agents" in body else []
    _send_json(handler, {"ok": True,
                        "saved_agents": saved_agents_out,
                        "message": "config saved"})


def handle_update_single_agent(handler, agent_id):
    """PUT /api/agents/{agent_id} — update a single agent in-place.

    Supports renaming (old_id in body), display_name, color, cursor,
    filter_from, wakeup, adapter. Avoids the "full replace" pattern
    where the frontend has to POST all agents just to edit one.

    Blocked if agent is used by running rooms (rename/delete only).
    """
    if not validate_agent_id(agent_id):
        _send_json(handler, {"ok": False, "error": "invalid agent_id"})
        return
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    shared = Path(handler.shared_dir)
    cfg, config_path = read_bridge(shared)
    agents = cfg.get("agents", {})

    old_id = (body.get("old_id") or "").strip()
    new_id = (body.get("id") or agent_id).strip()
    if not validate_agent_id(new_id):
        _send_json(handler, {"ok": False, "error": "invalid id"})
        return

    # Rename path
    target_key = agent_id
    if old_id and old_id != new_id and old_id in agents:
        blockers = running_rooms_using_agents(shared, cfg, {old_id})
        if blockers:
            rooms = ", ".join(room_label(room) for room in blockers)
            _send_json(handler, {
                "ok": False,
                "error": f"agent rename blocked by running room(s): {rooms}",
            })
            return
        target_key = old_id

    if target_key not in agents:
        _send_json(handler, {"ok": False, "error": f"agent not found: '{agent_id}'"})
        return

    # Apply field updates
    agent = agents[target_key]
    rename_performed = False
    if new_id != target_key:
        # rename
        del agents[target_key]
        agent["id"] = new_id
        agents[new_id] = agent
        rename_cursor(shared, target_key, new_id)
        rename_agent_in_rooms(shared, cfg, target_key, new_id)
        if cfg.get("agent_id") == target_key:
            cfg["agent_id"] = new_id
        rename_performed = True

    if "display_name" in body:
        agent["display_name"] = (body.get("display_name") or agent["id"]).strip() or agent["id"]
    if "color" in body:
        color = (body.get("color") or "").strip()
        if re.match(r'^#[0-9a-fA-F]{6}$', color):
            agent["color"] = color
    if "cursor" in body:
        agent["cursor"] = body.get("cursor") or "line"
    if "filter_from" in body:
        agent["filter_from"] = body.get("filter_from") or ""

    # Wakeup / adapter
    if "wakeup" in body:
        wu = body.get("wakeup") or {}
        wakeup = {
            "url": wu.get("url", ""),
            "method": wu.get("method", "POST"),
            "headers": wu.get("headers", {"Content-Type": "application/json"}),
            "body_template": wu.get("body_template", {"message": "{{message}}"}),
        }
        auth = wu.get("auth")
        if auth and auth.get("type") == "bearer":
            if auth.get("token_path"):
                wakeup["auth"] = {
                    "type": "bearer",
                    "token_path": auth["token_path"],
                    "token_jsonpath": auth.get("token_jsonpath", ""),
                }
            elif auth.get("token_env"):
                wakeup["auth"] = {"type": "bearer", "token_env": auth["token_env"]}
        agent["wakeup"] = wakeup
        agent["adapter"] = body.get("adapter") or wakeup_to_adapter(wakeup)
    elif "adapter" in body:
        adapter = body.get("adapter") or {}
        agent["adapter"] = adapter
        # keep wakeup in sync for backward compat
        agent["wakeup"] = adapter_to_wakeup(adapter)

    sync_filter_from(cfg)
    if cfg.get("agent_id") not in cfg.get("agents", {}):
        cfg["agent_id"] = next(iter(cfg.get("agents", {}).keys()), "")
    write_bridge(config_path, cfg)

    _send_json(handler, {
        "ok": True,
        "agent": {
            "id": agent["id"],
            "display_name": agent.get("display_name", agent["id"]),
            "color": agent.get("color", "#8888a0"),
        },
        "renamed": rename_performed,
        "message": "agent saved",
    })


def handle_save_room(handler):
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    shared = Path(handler.shared_dir)
    cfg, config_path = read_bridge(shared)

    room_id = (body.get("id") or "").strip() or generate_room_id()
    room_name = (body.get("name") or "").strip()
    room_agents = body.get("agents") or []
    room_order = body.get("order") or room_agents
    room_policy = body.get("policy") or "round_robin"
    room_status = body.get("status") or "paused"
    try:
        max_turns = int(body.get("max_turns", 50))
    except (TypeError, ValueError):
        max_turns = 50

    if not validate_room_id(room_id):
        _send_json(handler, {"ok": False, "error": "invalid room id"})
        return

    for aid in room_agents:
        if aid not in cfg.get("agents", {}):
            _send_json(handler, {"ok": False, "error": f"unknown agent: '{aid}'"})
            return

    if len(set(room_agents)) != len(room_agents):
        _send_json(handler, {"ok": False, "error": "duplicate agent in room"})
        return
    if len(set(room_order)) != len(room_order):
        _send_json(handler, {"ok": False, "error": "duplicate agent in order"})
        return
    for aid in room_order:
        if aid not in room_agents:
            _send_json(handler, {"ok": False, "error": f"order contains non-member agent: '{aid}'"})
            return
    if room_policy not in ("round_robin", "broadcast"):
        _send_json(handler, {"ok": False, "error": "unsupported room policy"})
        return

    existing_rooms = cfg.get("rooms", {})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = existing_rooms.get(room_id, {})
    if existing:
        existing_room = normalize_room({**existing, "id": room_id})
        if room_runtime_status(shared, existing_room) == "running":
            existing_order = existing_room.get("order", [])
            if existing_room.get("agents", []) != room_agents or existing_order != room_order:
                _send_json(handler, {
                    "ok": False,
                    "error": "running room members cannot be changed",
                })
                return
    room = normalize_room({
        "id": room_id,
        "name": room_name or existing.get("name", room_id),
        "agents": room_agents,
        "order": room_order,
        "policy": room_policy,
        "status": room_status,
        "max_turns": max_turns,
        "created_at": existing.get("created_at", now),
        "meta": existing.get("meta", {}),
    })
    cfg.setdefault("rooms", {})[room_id] = room

    sync_filter_from(cfg)
    ensure_room(shared, room)
    state = read_room_state(shared, room_id, room)
    state["order"] = room["order"]
    state["max_turns"] = room["max_turns"]
    state["status"] = room_status
    write_room_state(shared, room_id, state)
    write_bridge(config_path, cfg)
    append_room_log_safely(
        shared,
        room_id,
        "room_saved" if existing else "room_created",
        "聊天室配置已保存" if existing else "聊天室已创建",
        meta={"agents": room["agents"], "order": room["order"], "status": room_status},
    )
    _send_json(handler, {"ok": True, "room_id": room_id})


def handle_delete_room(handler):
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    room_id = (body.get("id") or "").strip()
    if not room_id:
        _send_json(handler, {"ok": False, "error": "room id required"})
        return

    shared = Path(handler.shared_dir)
    cfg, config_path = read_bridge(shared)

    if room_id not in cfg.get("rooms", {}):
        _send_json(handler, {"ok": False, "error": "room not found"})
        return

    room = normalize_room({**cfg["rooms"][room_id], "id": room_id})
    if room_runtime_status(shared, room) == "running":
        _send_json(handler, {"ok": False, "error": "运行中的聊天室不能删除"})
        return

    del cfg["rooms"][room_id]
    sync_filter_from(cfg)
    write_bridge(config_path, cfg)
    _send_json(handler, {"ok": True, "deleted": room_id})


def handle_archive(handler):
    shared = Path(handler.shared_dir)
    active = shared / "active.jsonl"
    if not active.exists():
        _send_json(handler, {"ok": False, "error": "no active file"})
        return
    msgs = parse_jsonl(active)
    if not msgs:
        _send_json(handler, {"ok": False, "error": "active file is empty"})
        return
    name = do_archive(shared)
    if name:
        _send_json(handler, {"ok": True, "archived_to": name, "message_count": len(msgs)})
    else:
        _send_json(handler, {"ok": False, "error": "archive failed"})


def handle_open_current_folder(handler):
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    archive_name = body.get("archive", "__active__")
    shared = Path(handler.shared_dir)
    try:
        chat_file = resolve_chat_file(shared, archive_name)
    except FileNotFoundError:
        _send_json(handler, {"ok": False, "error": "archive file not found"})
        return
    except ValueError as exc:
        _send_json(handler, {"ok": False, "error": str(exc)})
        return

    ok, error = open_in_file_manager(chat_file, select=True)
    if not ok:
        _send_json(handler, {"ok": False, "error": error or "failed to open folder"})
        return

    _send_json(handler, {"ok": True, "path": str(chat_file)})


def handle_open_dir(handler):
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return
    dir_path = body.get("path", "")
    if not dir_path:
        _send_json(handler, {"ok": False, "error": "path is required"})
        return
    ok, error = open_in_file_manager(dir_path)
    if not ok:
        _send_json(handler, {"ok": False, "error": error or "failed to open directory"})
        return
    _send_json(handler, {"ok": True, "path": str(dir_path)})


def handle_send_message(handler):
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    agent_id = body.get("agent_id", "")
    text = body.get("text", "")
    if not agent_id or not text:
        _send_json(handler, {"ok": False, "error": "agent_id and text required"})
        return

    # 验证 agent_id 是否在配置中定义
    shared = Path(handler.shared_dir)
    cfg, _ = read_bridge(shared)
    known_agents = cfg.get("agents", {})
    if agent_id not in known_agents:
        _send_json(handler, {"ok": False, "error": f"unknown agent_id: '{agent_id}'"})
        return

    active = shared / "active.jsonl"
    active.parent.mkdir(parents=True, exist_ok=True)
    msg = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "from": agent_id,
        "msg": text,
    }
    with file_lock(shared / ".active.lock"):
        with open(active, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    _send_json(handler, {"ok": True, "agent_id": agent_id, "chars": len(text)})


def handle_test_agent(handler):
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    adapter = body.get("adapter")
    if adapter:
        adapter_type = adapter.get("type", "")
        cfg = adapter.get("config") or {}
        url = (cfg.get("url") or "").strip()
        if not url:
            _send_json(handler, {"ok": False, "error": "Webhook URL not configured"})
            return
        if adapter_type in ("openclaw_sessions", "native_http"):
            success, detail = _probe_http_reachable(url)
            if success:
                _send_json(handler, {"ok": True, "status": detail})
            else:
                _send_json(handler, {"ok": False, "error": _classify_conn_error(detail, url), "raw": detail})
            return
        wakeup = adapter_to_wakeup(adapter)
    else:
        wakeup = body.get("wakeup", {})
    url = wakeup.get("url", "").strip()
    if not url:
        _send_json(handler, {"ok": False, "error": "Webhook URL not configured"})
        return

    success, detail, _body = wakeup_agent(wakeup, "[Agent Bridge] connectivity test", "agent-bridge")
    if success:
        _send_json(handler, {"ok": True, "status": detail})
    else:
        msg = _classify_conn_error(detail, url)
        _send_json(handler, {"ok": False, "error": msg, "raw": detail})


def _create_integration_test_room(handler, cfg, config_path, shared, agent_id):
    agent = cfg.get("agents", {}).get(agent_id, {})
    display_name = (agent.get("display_name") or agent_id).strip()
    room_id = f"test_{agent_id}_{uuid.uuid4().hex[:6]}"
    room = normalize_room({
        "id": room_id,
        "name": f"{display_name} 临时测试聊天室",
        "agents": [agent_id],
        "order": [agent_id],
        "policy": "round_robin",
        "status": ROOM_RUNNING,
        "max_turns": 50,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "meta": {"temporary": True},
    })
    cfg.setdefault("rooms", {})[room_id] = room
    sync_filter_from(cfg)
    ensure_room(shared, room)
    state = read_room_state(shared, room_id, room)
    state["status"] = ROOM_RUNNING
    state["order"] = room["order"]
    state["max_turns"] = room["max_turns"]
    state["current_turn"] = None
    state["waiting_for"] = ""
    state["waiting_line"] = 0
    state["last_error"] = ""
    write_room_state(shared, room_id, state)
    write_bridge(config_path, cfg)
    append_room_log_safely(
        shared,
        room_id,
        "room_created",
        "联调用临时聊天室已创建",
        meta={"agents": room["agents"], "order": room["order"], "temporary": True},
    )
    return room_id, room


def handle_agent_integration_test(handler):
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    agent_id = (body.get("agent_id") or "").strip()
    if not validate_agent_id(agent_id):
        _send_json(handler, {"ok": False, "error": "invalid agent_id"})
        return

    shared = Path(handler.shared_dir)
    cfg, config_path = read_bridge(shared)
    if agent_id not in cfg.get("agents", {}):
        _send_json(handler, {"ok": False, "error": "请先保存 Agent 配置"})
        return

    rooms = cfg.get("rooms", {})
    room_id = ""
    room = None
    for rid, item in rooms.items():
        if agent_id in (item.get("agents") or []):
            room_id = rid
            room = item
            break
    if not room_id or not room:
        if not body.get("auto_create_room"):
            _send_json(handler, {
                "ok": False,
                "error": "请先把该 Agent 加入一个聊天室",
                "needs_room": True,
            })
            return
        room_id, room = _create_integration_test_room(handler, cfg, config_path, shared, agent_id)

    room_cfg = normalize_room({**room, "id": room_id})
    state = read_room_state(shared, room_id, room_cfg)
    state = migrate_room_state(state, room_cfg)
    current_turn = state.get("current_turn") or {}
    if current_turn and not current_turn.get("response_message_id"):
        _send_json(handler, {"ok": False, "error": f"聊天室正在等待 {current_turn.get('agent_id', '')} 回复，请稍后再试"})
        return

    agents = room_cfg.get("agents") or []
    order = room_cfg.get("order") or agents
    if agent_id not in order:
        order = [agent_id] + [aid for aid in order if aid != agent_id]
        room_cfg["order"] = order
        cfg["rooms"][room_id]["order"] = order

    ensure_room(shared, room_cfg)
    state["status"] = ROOM_RUNNING
    state["turn_index"] = order.index(agent_id)
    state["current_turn"] = None
    state["waiting_for"] = ""
    state["last_error"] = ""
    write_room_state(shared, room_id, state)
    cfg["rooms"][room_id]["status"] = ROOM_RUNNING
    write_bridge(config_path, cfg)

    text = body.get("text") or "Agent Bridge 联调测试：请通过 callback 回写一句简短确认。"
    msg = append_room_message(
        shared, room_id, "user", text,
        to_agent=agent_id, kind="user",
        meta={"source": "integration_test", "target": agent_id},
    )
    emit_event(shared, room_id, EVT_MESSAGE_CREATED, actor="user",
               message_id=msg.get("id", ""), meta={"source": "integration_test", "target": agent_id})

    result = run_room_step(cfg, room_id)
    next_state = read_room_state(shared, room_id, room_cfg)
    turn = (next_state.get("current_turn") or {})
    if not result.get("ok", True):
        _send_json(handler, {"ok": False, "error": result.get("error", "Runtime V2 联调失败"), "result": result})
        return
    action = result.get("action")
    if action == "sync_response":
        _send_json(handler, {
            "ok": True,
            "room_id": room_id,
            "agent_id": agent_id,
            "message_id": msg.get("id", ""),
            "room_created": bool(body.get("auto_create_room")),
            "response_received": True,
            "result": result,
        })
        return
    if action != "waiting":
        _send_json(handler, {"ok": False, "error": result.get("error", f"Runtime V2 未进入等待回写状态: {action}"), "result": result})
        return
    _send_json(handler, {
        "ok": True,
        "room_id": room_id,
        "agent_id": agent_id,
        "message_id": msg.get("id", ""),
        "room_created": bool(body.get("auto_create_room")),
        "turn_id": turn.get("turn_id", ""),
        "correlation_id": turn.get("correlation_id", ""),
        "result": result,
    })


# ═══════════════════════════════════════════════════════════
#  Poll API handlers
# ═══════════════════════════════════════════════════════════

def handle_poll_status(handler):
    status = handler.poll_manager.get_status() if handler.poll_manager else {
        "running": False, "interval": 0, "last_run": None, "last_result": {}
    }
    _send_json(handler, {"ok": True, **status})


def handle_poll_now(handler):
    if not handler.poll_manager:
        _send_json(handler, {"ok": False, "error": "poll manager not initialized"})
        return
    result = handler.poll_manager.poll_now()
    _send_json(handler, {"ok": True, "result": result})


def handle_poll_start(handler):
    if not handler.poll_manager:
        _send_json(handler, {"ok": False, "error": "poll manager not initialized"})
        return
    handler.poll_manager.start()
    _send_json(handler, {"ok": True, "running": True})


def handle_poll_stop(handler):
    if not handler.poll_manager:
        _send_json(handler, {"ok": False, "error": "poll manager not initialized"})
        return
    handler.poll_manager.stop()
    _send_json(handler, {"ok": True, "running": False})


def handle_poll_history(handler):
    if not handler.poll_manager:
        _send_json(handler, {"ok": False, "error": "poll manager not initialized"})
        return
    body, _ = _read_json_body(handler)
    limit = (body or {}).get("limit", 50)
    history = handler.poll_manager.get_history(limit)
    _send_json(handler, {"ok": True, "history": history})


# ═══════════════════════════════════════════════════════════
#  Room sub-endpoints
# ═══════════════════════════════════════════════════════════

def _parse_room_api_path(path):
    """Parse /api/rooms/{room_id}/{action} or /api/rooms/{room_id}/agents/{agent_id}/{action}."""
    parts = [p for p in path.split("/") if p]
    # Standard: /api/rooms/{room_id}/{action}
    if len(parts) == 4 and parts[0] == "api" and parts[1] == "rooms":
        room_id = parts[2]
        action = parts[3]
        if not validate_room_id(room_id):
            return None, None, None
        return room_id, action, None
    # Nested: /api/rooms/{room_id}/agents/{agent_id}/{action}
    if len(parts) == 6 and parts[0] == "api" and parts[1] == "rooms" and parts[3] == "agents":
        room_id = parts[2]
        agent_id = parts[4]
        action = parts[5]
        if not validate_room_id(room_id) or not validate_agent_id(agent_id):
            return None, None, None
        return room_id, action, agent_id
    return None, None, None


def handle_room_messages(handler, path):
    room_id, action, _agent_id = _parse_room_api_path(path)
    if action != "messages":
        handler.send_error(404)
        return
    shared = Path(handler.shared_dir)
    cfg, _ = read_bridge(shared)
    if room_id not in cfg.get("rooms", {}):
        _send_json(handler, {"ok": False, "error": "room not found"})
        return
    messages = read_room_messages(shared, room_id, include_history=False, limit=500)
    _send_json(handler, {"ok": True, "room_id": room_id, "count": len(messages), "messages": messages})


def handle_room_logs(handler, path):
    room_id, action, _agent_id = _parse_room_api_path(path)
    if action != "logs":
        handler.send_error(404)
        return
    shared = Path(handler.shared_dir)
    cfg, _ = read_bridge(shared)
    if room_id not in cfg.get("rooms", {}):
        _send_json(handler, {"ok": False, "error": "room not found"})
        return
    logs = read_room_logs(shared, room_id, limit=500)
    _send_json(handler, {"ok": True, "room_id": room_id, "count": len(logs), "logs": logs})


def handle_room_events(handler, path):
    """GET /api/rooms/{room_id}/events"""
    room_id, action, _agent_id = _parse_room_api_path(path)
    if not room_id:
        handler.send_error(404)
        return
    shared = Path(handler.shared_dir)
    limit = 500
    parsed = urllib.parse.urlparse(handler.path)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    if "limit" in params:
        try:
            limit = min(int(params["limit"]), 1000)
        except ValueError:
            pass
    events = read_events(shared, room_id, limit=limit)
    _send_json(handler, {"ok": True, "room_id": room_id, "count": len(events), "events": events})


def handle_room_current_turn(handler, path):
    """GET /api/rooms/{room_id}/turn"""
    room_id, action, _agent_id = _parse_room_api_path(path)
    if not room_id:
        handler.send_error(404)
        return
    shared = Path(handler.shared_dir)
    cfg, _ = read_bridge(shared)
    if room_id not in cfg.get("rooms", {}):
        _send_json(handler, {"ok": False, "error": "room not found"})
        return
    room_cfg = normalize_room({**cfg["rooms"][room_id], "id": room_id})
    state = read_room_state(shared, room_id, room_cfg)
    state = migrate_room_state(state, room_cfg)
    current_turn = state.get("current_turn")
    _send_json(handler, {"ok": True, "room_id": room_id, "current_turn": current_turn,
                        "status": state.get("status"), "turn_index": state.get("turn_index")})


def handle_room_action(handler, path):
    room_id, action, agent_id = _parse_room_api_path(path)
    if not room_id:
        handler.send_error(404)
        return

    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    shared = Path(handler.shared_dir)
    cfg, config_path = read_bridge(shared)
    if room_id not in cfg.get("rooms", {}):
        _send_json(handler, {"ok": False, "error": "room not found"})
        return

    room = normalize_room({**cfg["rooms"][room_id], "id": room_id})

    # ── Agent callback: /api/rooms/{room_id}/agents/{agent_id}/callback ──
    if action == "callback" and agent_id:
        _handle_agent_callback(handler, cfg, shared, room_id, agent_id, body)
        return

    # ── Agent active message: /api/rooms/{room_id}/agents/{agent_id}/message ──
    if action == "message" and agent_id:
        _handle_agent_message(handler, cfg, shared, room_id, agent_id, body)
        return

    if action == "send":
        agent_id = (body.get("agent_id") or body.get("from") or "user").strip()
        text = body.get("text", "")
        to_agent = (body.get("to") or "").strip()
        kind = body.get("kind") or ("user" if agent_id == "user" else "agent")
        if not validate_agent_id(agent_id):
            _send_json(handler, {"ok": False, "error": "invalid agent_id"})
            return
        if agent_id != "user" and agent_id not in cfg.get("agents", {}):
            _send_json(handler, {"ok": False, "error": f"unknown agent_id: '{agent_id}'"})
            return
        if agent_id != "user" and agent_id not in room.get("agents", []):
            _send_json(handler, {"ok": False, "error": f"agent is not in room: '{agent_id}'"})
            return
        if to_agent and to_agent not in room.get("agents", []):
            _send_json(handler, {"ok": False, "error": f"target agent is not in room: '{to_agent}'"})
            return
        if not text:
            _send_json(handler, {"ok": False, "error": "text required"})
            return
        msg = append_room_message(shared, room_id, agent_id, text, to_agent=to_agent, kind=kind)
        # v2: emit event + schedule room
        emit_event(shared, room_id, EVT_MESSAGE_CREATED, actor=agent_id,
                   message_id=msg.get("id", ""))
        _schedule_room(cfg, room_id)
        _send_json(handler, {"ok": True, "room_id": room_id, "message": msg})
        return

    if action == "start":
        if not room.get("agents"):
            _send_json(handler, {"ok": False, "error": "room has no agents"})
            return
        missing = [aid for aid in room.get("agents", []) if aid not in cfg.get("agents", {})]
        if missing:
            _send_json(handler, {"ok": False, "error": "room has unknown agent: " + ", ".join(missing)})
            return
        state = set_room_status(shared, room, "running")
        cfg["rooms"][room_id]["status"] = "running"
        write_bridge(config_path, cfg)
        append_room_log_safely(shared, room_id, "room_started", "聊天室已开始运行")
        # v2: emit event + schedule room
        emit_event(shared, room_id, EVT_ROOM_STARTED)
        _schedule_room(cfg, room_id)
        _send_json(handler, {"ok": True, "room_id": room_id, "state": state})
        return

    if action == "resume":
        # Explicit error → running recovery path. Also accepts paused → running.
        cur_status = (room.get("status") or read_room_state(shared, room_id, room).get("status") or "paused")
        if cur_status not in ("error", "paused"):
            _send_json(handler, {"ok": False, "error": f"cannot resume from status: {cur_status}"})
            return
        if not room.get("agents"):
            _send_json(handler, {"ok": False, "error": "room has no agents"})
            return
        # Clear last_error, reset turn to idle if it was failed
        state = set_room_status(shared, room, "running")
        cfg["rooms"][room_id]["status"] = "running"
        write_bridge(config_path, cfg)
        append_room_log_safely(shared, room_id, "room_resumed", f"聊天室从 {cur_status} 恢复运行")
        emit_event(shared, room_id, EVT_ROOM_STARTED, meta={"recovered_from": cur_status})
        _schedule_room(cfg, room_id)
        _send_json(handler, {"ok": True, "room_id": room_id, "state": state, "recovered_from": cur_status})
        return

    if action == "pause":
        state = set_room_status(shared, room, "paused")
        cfg["rooms"][room_id]["status"] = "paused"
        write_bridge(config_path, cfg)
        append_room_log_safely(shared, room_id, "room_paused", "聊天室已暂停")
        emit_event(shared, room_id, EVT_ROOM_PAUSED)
        _send_json(handler, {"ok": True, "room_id": room_id, "state": state})
        return

    if action == "tick":
        # v2: always use runtime state machine (no V1 fallback)
        result = run_room_step(cfg, room_id)
        _send_json(handler, {"ok": bool(result.get("ok", True)), "result": result})
        return

    if action == "schedule":
        # Manual schedule trigger
        _schedule_room(cfg, room_id)
        _send_json(handler, {"ok": True, "room_id": room_id, "scheduled": True})
        return

    handler.send_error(404)


def _handle_agent_callback(handler, cfg, shared, room_id, agent_id, body):
    """Handle POST /api/rooms/{room_id}/agents/{agent_id}/callback"""
    # Token auth
    parsed = urllib.parse.urlparse(handler.path)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    token = extract_token_from_request(dict(handler.headers), params)
    ok, err = verify_callback_token(cfg, agent_id, token)
    if not ok:
        _send_json(handler, {"ok": False, "error": f"auth failed: {err}"}, status=403)
        return

    # Validate membership
    if not agent_in_room(cfg, room_id, agent_id):
        _send_json(handler, {"ok": False, "error": f"agent {agent_id} not in room {room_id}"}, status=403)
        return

    # Extract message
    message = body.get("message", "")
    turn_id = body.get("turn_id", "")
    correlation_id = body.get("correlation_id", "")
    meta = body.get("meta") or {}

    try:
        message = sanitize_message(message)
    except ValueError as e:
        _send_json(handler, {"ok": False, "error": str(e)})
        return

    result = receive_agent_response(
        shared, room_id, agent_id, message,
        turn_id=turn_id, correlation_id=correlation_id,
        source="callback", meta=meta,
    )

    status = 200 if result.get("ok") else 400
    _send_json(handler, result, status=status)


def _handle_agent_message(handler, cfg, shared, room_id, agent_id, body):
    """Handle POST /api/rooms/{room_id}/agents/{agent_id}/message"""
    # Token auth
    parsed = urllib.parse.urlparse(handler.path)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    token = extract_token_from_request(dict(handler.headers), params)
    ok, err = verify_callback_token(cfg, agent_id, token)
    if not ok:
        _send_json(handler, {"ok": False, "error": f"auth failed: {err}"}, status=403)
        return

    if not agent_in_room(cfg, room_id, agent_id):
        _send_json(handler, {"ok": False, "error": f"agent {agent_id} not in room {room_id}"}, status=403)
        return

    message = body.get("message", "")
    mode = body.get("mode", "normal")
    to_agent = body.get("to", "")
    meta = body.get("meta") or {}

    try:
        message = sanitize_message(message)
    except ValueError as e:
        _send_json(handler, {"ok": False, "error": str(e)})
        return

    msg = append_room_message(shared, room_id, agent_id, message,
                               to_agent=to_agent, kind="agent",
                               meta={"source": "active_push", "mode": mode, **meta})
    emit_event(shared, room_id, EVT_MESSAGE_CREATED, actor=agent_id,
               message_id=msg.get("id", ""), meta={"source": "active_push", "mode": mode})
    _schedule_room(cfg, room_id)
    _send_json(handler, {"ok": True, "room_id": room_id, "agent_id": agent_id,
                        "message_id": msg.get("id", ""), "message": msg})


def _schedule_room(cfg, room_id):
    """Helper: schedule room via v2 scheduler if available."""
    try:
        sched = get_scheduler()
        if sched:
            sched.set_config(cfg)
            sched.schedule_room(room_id)
    except Exception as exc:
        logging.debug("_schedule_room failed for %s: %s", room_id, exc)


# ═══════════════════════════════════════════════════════════
#  Settings API handlers
# ═══════════════════════════════════════════════════════════

DEFAULT_SETTINGS = {
    "poll_interval": DEFAULT_POLL_INTERVAL,
    "auto_start_poll": True,
    "max_log_entries": 1000,
}


def _read_settings(shared_dir):
    """Read settings from bridge.yaml, falling back to defaults."""
    shared = Path(shared_dir)
    cfg, _ = read_bridge(shared)
    stored = cfg.get("settings") or {}
    return {
        "poll_interval": stored.get("poll_interval", DEFAULT_SETTINGS["poll_interval"]),
        "auto_start_poll": stored.get("auto_start_poll", DEFAULT_SETTINGS["auto_start_poll"]),
        "max_log_entries": stored.get("max_log_entries", DEFAULT_SETTINGS["max_log_entries"]),
    }


def handle_get_settings(handler):
    """GET /api/settings — return all settings (port/shared_dir from server runtime)."""
    shared_dir = handler.shared_dir
    settings = _read_settings(shared_dir)

    # Determine port from the server socket
    port = 8825
    try:
        port = handler.server.server_address[1]
    except Exception:
        pass

    _send_json(handler, {
        "ok": True,
        "port": port,
        "shared_dir": str(shared_dir),
        "poll_interval": settings["poll_interval"],
        "auto_start_poll": settings["auto_start_poll"],
        "max_log_entries": settings["max_log_entries"],
        "version": "0.1.0",
        "project_url": "https://github.com/nousresearch/agent-bridge",
    })


def handle_update_settings(handler):
    """PUT /api/settings — save editable settings to bridge.yaml."""
    body, err = _read_json_body(handler)
    if err:
        _send_json(handler, err)
        return

    shared = Path(handler.shared_dir)
    cfg, config_path = read_bridge(shared)
    current = cfg.get("settings") or {}

    # Validate and apply poll_interval
    if "poll_interval" in body:
        try:
            val = int(body["poll_interval"])
            if val < 5:
                _send_json(handler, {"ok": False, "error": "poll_interval must be >= 5 seconds"})
                return
            if val > 3600:
                _send_json(handler, {"ok": False, "error": "poll_interval must be <= 3600 seconds"})
                return
            current["poll_interval"] = val
        except (ValueError, TypeError):
            _send_json(handler, {"ok": False, "error": "poll_interval must be an integer"})
            return

    # Validate and apply auto_start_poll
    if "auto_start_poll" in body:
        current["auto_start_poll"] = bool(body["auto_start_poll"])

    # Validate and apply max_log_entries
    if "max_log_entries" in body:
        try:
            val = int(body["max_log_entries"])
            if val < 10:
                _send_json(handler, {"ok": False, "error": "max_log_entries must be >= 10"})
                return
            if val > 100000:
                _send_json(handler, {"ok": False, "error": "max_log_entries must be <= 100000"})
                return
            current["max_log_entries"] = val
        except (ValueError, TypeError):
            _send_json(handler, {"ok": False, "error": "max_log_entries must be an integer"})
            return

    cfg["settings"] = current
    write_bridge(config_path, cfg)

    _send_json(handler, {
        "ok": True,
        "saved": list(current.keys()),
        "message": "设置已保存",
    })


# ═══════════════════════════════════════════════════════════
#  HTTP helpers
# ═══════════════════════════════════════════════════════════

MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB


def _read_json_body(handler):
    """Read and parse JSON body with size limit. Returns (body, error_response)."""
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return None, {"ok": False, "error": "empty body"}
    if length > MAX_BODY_SIZE:
        return None, {"ok": False, "error": "request body too large"}
    try:
        return json.loads(handler.rfile.read(length)), None
    except json.JSONDecodeError:
        return None, {"ok": False, "error": "invalid JSON"}


def _send_json(handler, data, status=200):
    text = json.dumps(data, ensure_ascii=False)
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    handler.send_header("Content-Security-Policy", CSP_HEADER)
    handler.send_header("X-Content-Type-Options", "nosniff")
    # 动态回显请求 Origin，仅允许可信的 localhost 来源
    origin = handler.headers.get("Origin", "")
    if origin and re.match(r'^http://127\.0\.0\.1:\d+$', origin):
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()
