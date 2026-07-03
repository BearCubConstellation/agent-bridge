#!/usr/bin/env python3
"""
Agent Bridge — 配置管理模块

导出: read_bridge(), write_bridge(), normalize_config(),
      find_shared_dir(), generate_room_id(), sync_filter_from(),
      以及相关的房间/Agent 工具函数。
"""
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from lock import file_lock
from poll import parse_jsonl
from rooms import (
    ensure_room,
    normalize_room,
    read_room_state,
    write_room_state,
    validate_room_id,
)
from send import validate_agent_id

BRIDGE_FILENAME = "bridge.yaml"
VALID_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')
DEFAULT_POLL_INTERVAL = 180  # 秒


def is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def open_in_file_manager(path, select=False):
    target = Path(path)
    target_exists = target.exists()
    try:
        if os.name == "nt" and hasattr(os, "startfile"):
            if select and target_exists and target.is_file():
                subprocess.Popen(
                    ["explorer.exe", "/select,", str(target)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.startfile(str(target if target_exists else target.parent))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            if select and target_exists and target.is_file():
                cmd = ["open", "-R", str(target)]
            else:
                cmd = ["open", str(target if target_exists else target.parent)]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            target = target if target_exists and not target.is_file() else target.parent
            subprocess.Popen(
                ["xdg-open", str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True, None
    except Exception as exc:
        return False, str(exc)


def resolve_chat_file(shared_dir, archive_name):
    shared = Path(shared_dir)
    if not archive_name or archive_name == "__active__":
        return shared / "active.jsonl"

    filename = Path(str(archive_name)).name
    if not filename.endswith(".jsonl"):
        raise ValueError("Only .jsonl archive files are allowed")

    history_root = (shared / "history").resolve()
    archive_path = (shared / "history" / filename).resolve()
    if not is_relative_to(archive_path, history_root):
        raise ValueError("invalid archive name")
    if not archive_path.exists():
        raise FileNotFoundError(filename)
    return archive_path


# ─── 配置 ─────────────────────────────────────────────

def find_shared_dir():
    for d in [Path.home() / ".agent-bridge", Path.home() / ".shared-chat"]:
        active = d / "active.jsonl"
        if d.exists() and (active.exists() or (d / "history").exists()):
            return d
    return Path.home() / ".agent-bridge"


def _read_yaml_file(path):
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _agent_source(path):
    try:
        return str(path.expanduser())
    except Exception:
        return str(path)


def _agent_source_dir(path):
    try:
        p = path.expanduser()
        if p.exists() and p.is_file():
            return str(p.parent)
        return str(p)
    except Exception:
        return str(path)


def _env_ref_name(value):
    if not isinstance(value, str):
        return ""
    m = re.match(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$', value.strip())
    return m.group(1) if m else ""


def _apply_bearer_secret_ref(wakeup, value):
    env_name = _env_ref_name(value)
    if env_name:
        wakeup["auth"] = {"type": "bearer", "token_env": env_name}
        return "env"
    return "literal" if isinstance(value, str) and value.strip() else ""


def generate_room_id():
    return "room_" + uuid.uuid4().hex[:8]


def sync_filter_from(cfg):
    """Auto-derive filter_from for each agent based on room membership."""
    for a in cfg.get("agents", {}).values():
        a["filter_from"] = ""
    for room in cfg.get("rooms", {}).values():
        agents = room.get("agents", [])
        if len(agents) == 2:
            a0, a1 = agents[0], agents[1]
            if a0 in cfg.get("agents", {}) and a1 in cfg.get("agents", {}):
                cfg["agents"][a0]["filter_from"] = a1
                cfg["agents"][a1]["filter_from"] = a0


def room_runtime_status(shared_dir, room):
    try:
        state = read_room_state(shared_dir, room["id"], room)
        return state.get("status", room.get("status", "paused"))
    except Exception:
        return room.get("status", "paused")


def room_label(room):
    name = room.get("name") or room.get("id", "")
    rid = room.get("id", "")
    return f"{name}({rid})" if name and name != rid else rid


def append_room_log_safely(shared_dir, room_id, event, message="", level="info", agent_id="", meta=None):
    try:
        from rooms import append_room_log
        append_room_log(shared_dir, room_id, event, message, level=level, agent_id=agent_id, meta=meta)
    except Exception:
        pass


def running_rooms_using_agents(shared_dir, cfg, agent_ids):
    agent_ids = set(agent_ids)
    rooms = []
    for key, r in cfg.get("rooms", {}).items():
        room = normalize_room({**r, "id": r.get("id", key)})
        if room_runtime_status(shared_dir, room) != "running":
            continue
        if agent_ids.intersection(room.get("agents", [])):
            rooms.append(room)
    return rooms


def update_room_state_refs(shared_dir, room, removed_ids=None, rename_map=None):
    removed_ids = set(removed_ids or [])
    rename_map = rename_map or {}
    try:
        state = read_room_state(shared_dir, room["id"], room)
    except Exception:
        return
    state["order"] = [
        rename_map.get(aid, aid)
        for aid in state.get("order", [])
        if aid not in removed_ids
    ]
    waiting_for = state.get("waiting_for", "")
    if waiting_for in removed_ids:
        state["waiting_for"] = ""
        state["waiting_line"] = 0
    elif waiting_for in rename_map:
        state["waiting_for"] = rename_map[waiting_for]
    write_room_state(shared_dir, room["id"], state)


def remove_agents_from_rooms(shared_dir, cfg, removed_ids):
    removed_ids = set(removed_ids)
    if not removed_ids:
        return
    updated = {}
    for key, r in cfg.get("rooms", {}).items():
        room = normalize_room({**r, "id": r.get("id", key)})
        if not removed_ids.intersection(room.get("agents", [])):
            updated[room["id"]] = room
            continue
        room["agents"] = [aid for aid in room.get("agents", []) if aid not in removed_ids]
        room["order"] = [aid for aid in room.get("order", []) if aid not in removed_ids]
        room = normalize_room(room)
        updated[room["id"]] = room
        update_room_state_refs(shared_dir, room, removed_ids=removed_ids)
    cfg["rooms"] = updated


def rename_agent_in_rooms(shared_dir, cfg, old_id, new_id):
    if old_id == new_id:
        return
    updated = {}
    for key, r in cfg.get("rooms", {}).items():
        room = normalize_room({**r, "id": r.get("id", key)})
        if old_id not in room.get("agents", []):
            updated[room["id"]] = room
            continue
        room["agents"] = [new_id if aid == old_id else aid for aid in room.get("agents", [])]
        room["order"] = [new_id if aid == old_id else aid for aid in room.get("order", [])]
        room = normalize_room(room)
        updated[room["id"]] = room
        update_room_state_refs(shared_dir, room, rename_map={old_id: new_id})
    cfg["rooms"] = updated


def default_agents(shared_dir):
    """Return the initial Agent set for a new bridge config."""
    return {}


def rename_cursor(shared_dir, old_id, new_id):
    renamed = False
    shared = Path(shared_dir)
    for ext in ["_cursor", "_ts_cursor"]:
        old_path = shared / f".{old_id}{ext}"
        new_path = shared / f".{new_id}{ext}"
        if old_path.exists() and not new_path.exists():
            old_path.rename(new_path)
            renamed = True
    return renamed


def normalize_config(cfg, shared_dir):
    """Apply defaults and normalization to a raw bridge config dict.
    
    This is called by read_bridge() after loading from disk. It can also be
    called standalone to normalize a config in memory before writing.
    """
    shared_path = Path(shared_dir)
    cfg.setdefault("shared_dir", str(shared_path))
    agents = cfg.get("agents") or default_agents(shared_path)
    if not isinstance(agents, dict):
        agents = {}
    cfg["agents"] = {
        key: a for key, a in agents.items()
        if isinstance(a, dict) and not a.get("sample")
    }
    cfg.setdefault("rooms", {})
    cfg.setdefault("agent_id", "")
    if cfg["agent_id"] not in cfg["agents"]:
        cfg["agent_id"] = next(iter(cfg["agents"].keys()), "")
    first_agent_key = next(iter(cfg["agents"].keys()), "")
    for key, a in cfg["agents"].items():
        a.setdefault("display_name", a.get("id", key).capitalize())
        a.setdefault("color", "#ff6b6b" if first_agent_key == key else "#4ecdc4")
        a.setdefault("id", key)
        a.setdefault("cursor", "line")
        had_wakeup = "wakeup" in a
        from adapters import wakeup_to_adapter, adapter_to_wakeup
        if "adapter" not in a:
            a["adapter"] = wakeup_to_adapter(a.get("wakeup", {}))
        if not had_wakeup and a.get("adapter", {}).get("type") == "native_http":
            a["wakeup"] = adapter_to_wakeup(a["adapter"])
        else:
            a.setdefault("wakeup", {"url": "", "method": "POST", "body_template": {"message": "{{message}}"}})
            a["wakeup"]["body_template"] = a["wakeup"].get("body_template", {"message": "{{message}}"})
    normalized_rooms = {}
    for key, r in cfg.get("rooms", {}).items():
        room = normalize_room({**r, "id": r.get("id", key)})
        normalized_rooms[room["id"]] = room
        try:
            ensure_room(shared_path, room)
        except Exception:
            pass
    cfg["rooms"] = normalized_rooms
    return cfg


def read_bridge(shared_dir):
    config_path = Path(shared_dir) / BRIDGE_FILENAME
    cfg = None
    if config_path.exists():
        # 先尝试 yaml，失败再尝试 json
        try:
            import yaml
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except ImportError:
            pass
        except Exception as exc:
            logging.warning("Failed to parse %s as YAML: %s", config_path, exc)
        # json fallback (仅在 yaml 不可用或失败时)
        if cfg is None:
            try:
                with open(config_path, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception as exc:
                logging.warning("Failed to parse %s as JSON: %s", config_path, exc)
                cfg = {}
    if cfg is None:
        cfg = {}

    cfg = normalize_config(cfg, shared_dir)
    return cfg, config_path


def write_bridge(config_path, config):
    try:
        import yaml
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
    except ImportError:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
