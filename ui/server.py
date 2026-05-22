#!/usr/bin/env python3
"""
Agent Bridge — 本地 UI 服务

集成了轮询 + 配置管理 + 聊天时间线。
只需要跑这一个进程。

用法:
    python3 server.py                          # 默认 7899 端口
    python3 server.py --open                   # 自动打开浏览器
    python3 server.py --poll-interval 60       # 每 60 秒轮询一次
"""
import argparse
import hashlib
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

# 从 core/ 导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from lock import file_lock
from poll import do_archive, run_poll, load_config as load_poll_config, wakeup_agent, parse_jsonl
from adapters import adapter_capability, adapter_to_wakeup, normalize_adapter, wakeup_to_adapter
from rooms import (
    append_room_message,
    append_room_log,
    ensure_room,
    normalize_room,
    read_room_logs,
    read_room_messages,
    read_room_state,
    set_room_status,
    tick_room,
    tick_running_rooms,
    validate_room_id,
    write_room_state,
    room_dir,
    room_active_file,
)
# v2 modules
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
    validate_room_id as security_validate_room_id,
    validate_agent_id as security_validate_agent_id,
    verify_callback_token, extract_token_from_request,
    agent_in_room, sanitize_message,
)


BRIDGE_FILENAME = "bridge.yaml"
VALID_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')
# validate_agent_id 复用 core/send.py 的实现
from send import validate_agent_id  # noqa: E402
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


# parse_jsonl 从 core/poll.py 导入（避免重复定义）
from poll import parse_jsonl  # noqa: E402


def _classify_conn_error(detail, url):
    """Convert low-level connection errors into short UI messages."""
    d = detail.lower()
    if "10061" in d or "connection refused" in d or "errno 111" in d:
        return "Connection refused: target service is not running or the port is wrong"
    if "timed out" in d or "10060" in d or "errno 110" in d:
        return "Connection timed out: check the target address and network"
    if "name or service not known" in d or "11001" in d or "getaddrinfo" in d:
        return "Host name could not be resolved"
    if detail.startswith("HTTP "):
        code = detail.split("HTTP ", 1)[1].split(":")[0].strip()
        return f"HTTP error {code}"
    return detail


def _default_wakeup():
    return {
        "url": "",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body_template": {"message": "{{message}}"},
    }


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


def _discovered_agent(agent_id, display_name, kind, source, details="", wakeup=None):
    item = {
        "id": agent_id,
        "display_name": display_name,
        "kind": kind,
        "source": _agent_source(source),
        "source_dir": _agent_source_dir(source),
        "details": details,
    }
    if wakeup:
        item["wakeup"] = wakeup
        item["adapter"] = wakeup_to_adapter(wakeup)
    else:
        item["adapter"] = {"type": "manual", "config": {}, "auth": {}, "template": {}}
    item["capability"] = adapter_capability(item)
    item["health"] = item["capability"]["health"]
    return item


def discover_local_agents(shared_dir, include_bridge_config=True):
    """Return likely local AI agents without modifying bridge.yaml.

    The scan is intentionally shallow: it checks known per-user config
    locations and bridge message history, avoiding broad filesystem walks.
    """
    shared = Path(shared_dir)
    home = Path.home()
    found = {}

    def add(item):
        if validate_agent_id(item["id"]):
            found.setdefault(item["id"], item)

    # Agents already participating in the current bridge conversation.
    for m in parse_jsonl(shared / "active.jsonl"):
        aid = str(m.get("from", "")).strip()
        if aid:
            add(_discovered_agent(
                aid,
                aid.capitalize(),
                "Bridge 消息",
                shared / "active.jsonl",
                "active.jsonl 中出现过的发送者",
            ))

    if include_bridge_config:
        cfg, cfg_path = read_bridge(shared)
        for key, agent in cfg.get("agents", {}).items():
            aid = agent.get("id", key)
            item = _discovered_agent(
                aid,
                agent.get("display_name", aid),
                "Bridge 配置",
                cfg_path,
                "当前 bridge.yaml 中已配置",
                agent.get("wakeup", {}),
            )
            if agent.get("sample"):
                item["sample"] = True
                item["details"] = "示例 Agent，尚未连接真实程序"
            add(item)

    # Hermes Agent.
    hermes_config = home / ".hermes" / "config.yaml"
    if hermes_config.exists():
        cfg = _read_yaml_file(hermes_config)
        webhook = ((cfg.get("platforms") or {}).get("webhook") or {})
        extra = webhook.get("extra") or {}
        host = extra.get("host", "127.0.0.1")
        port = extra.get("port", 8644)
        routes = extra.get("routes") or {}
        route = "agent-reply" if "agent-reply" in routes else (next(iter(routes), "agent-reply"))
        wakeup = {
            "url": f"http://{host}:{port}/webhooks/{route}",
            "method": "POST",
            "body_template": {"message": "{{message}}"},
        }
        secret_source = ""
        raw_secret = webhook.get("secret") or ""
        secret_source = _apply_bearer_secret_ref(wakeup, raw_secret)
        route_cfg = routes.get(route, {}) if isinstance(routes.get(route), dict) else {}
        if not secret_source:
            raw_secret = route_cfg.get("secret", "")
            secret_source = _apply_bearer_secret_ref(wakeup, raw_secret)
        details = "检测到 ~/.hermes/config.yaml"
        if secret_source == "literal":
            details += "；secret 为明文配置，未自动导入"
        add(_discovered_agent(
            "hermes",
            "Hermes Agent",
            "Hermes",
            hermes_config,
            details,
            wakeup,
        ))
    elif (home / ".hermes").exists():
        add(_discovered_agent("hermes", "Hermes Agent", "Hermes", home / ".hermes", "检测到 ~/.hermes 目录"))

    # OpenClaw.
    openclaw_config = home / ".openclaw" / "openclaw.json"
    if openclaw_config.exists() or (home / ".openclaw").exists():
        auth_cfg = {"type": "bearer", "token_path": str(home / ".openclaw" / "openclaw.json")}
        # 根据实际认证模式推断 JSONPath
        if openclaw_config.exists():
            try:
                oc_data = json.loads(openclaw_config.read_text(encoding="utf-8"))
                oc_auth = (oc_data.get("gateway") or {}).get("auth") or {}
                mode = oc_auth.get("mode", "")
                if mode in ("token", "password"):
                    auth_cfg["token_jsonpath"] = f"gateway.auth.{mode}"
            except Exception:
                pass
        if "token_jsonpath" not in auth_cfg:
            auth_cfg["token_jsonpath"] = "gateway.auth.token"

        add(_discovered_agent(
            "openclaw",
            "OpenClaw",
            "OpenClaw",
            openclaw_config if openclaw_config.exists() else home / ".openclaw",
            "检测到 OpenClaw 本地配置",
            {
                "url": "http://127.0.0.1:18789/tools/invoke",
                "method": "POST",
                "auth": auth_cfg,
                "body_template": {
                    "tool": "sessions_send",
                    "args": {"sessionKey": "agent:main:main", "message": "{{message}}"},
                },
            },
        ))

    known_dirs = [
        ("claude-code", "Claude Code", "Claude", home / ".claude"),
        ("codex", "Codex", "Codex", home / ".codex"),
        ("gemini", "Gemini CLI", "Gemini", home / ".gemini"),
        ("qwen", "Qwen Code", "Qwen", home / ".qwen"),
    ]
    for aid, name, kind, path in known_dirs:
        if path.exists():
            add(_discovered_agent(aid, name, kind, path, f"检测到 {path.name} 目录"))

    return sorted(found.values(), key=lambda x: (x["kind"].lower(), x["id"].lower()))


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
        except Exception:
            pass
        # json fallback (仅在 yaml 不可用或失败时)
        if cfg is None:
            try:
                with open(config_path, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
    if cfg is None:
        cfg = {}

    cfg.setdefault("shared_dir", str(shared_dir))
    agents = cfg.get("agents") or default_agents(shared_dir)
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
            ensure_room(shared_dir, room)
        except Exception:
            pass
    cfg["rooms"] = normalized_rooms
    return cfg, config_path


def write_bridge(config_path, config):
    try:
        import yaml
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
    except ImportError:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)


def rename_cursor(shared_dir, old_id, new_id):
    renamed = False
    for ext in ["_cursor", "_ts_cursor"]:
        old_path = shared_dir / f".{old_id}{ext}"
        new_path = shared_dir / f".{new_id}{ext}"
        if old_path.exists() and not new_path.exists():
            old_path.rename(new_path)
            renamed = True
    return renamed


# ─── 后台轮询 ─────────────────────────────────────────

class PollManager:
    """Manage the background polling thread."""

    def __init__(self, shared_dir, interval=DEFAULT_POLL_INTERVAL):
        self.shared_dir = shared_dir
        self.interval = interval
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.last_result = {"ok": True, "new_msgs": 0, "delivered": False,
                            "archived": None, "error": "", "to_agent": "", "from_agent": ""}
        self.last_run = None
        self.running = False
        self.history = []  # [(timestamp, result_dict), ...]
        self.MAX_HISTORY = 100

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="poll-worker")
        self._thread.start()
        self.running = True

    def stop(self):
        self._stop.set()
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def poll_now(self):
        """Run one polling cycle synchronously."""
        return self._do_poll()

    def is_running(self):
        return self.running and self._thread is not None and self._thread.is_alive()

    def _loop(self):
        while not self._stop.is_set():
            self._do_poll()
            self._stop.wait(self.interval)

    def _do_poll(self):
        """Run one poll cycle and update last_result/last_run."""
        config_path = Path(self.shared_dir) / BRIDGE_FILENAME
        if not config_path.exists():
            return self.last_result

        try:
            config = load_poll_config(str(config_path))
            result = run_poll(config)
            room_results = tick_running_rooms(config)
            if room_results:
                result["rooms"] = room_results
        except Exception as e:
            result = {"ok": False, "new_msgs": 0, "delivered": False,
                      "archived": None, "error": str(e), "to_agent": "", "from_agent": ""}

        with self._lock:
            self.last_result = result
            self.last_run = now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.history.append((now, dict(result)))
            if len(self.history) > self.MAX_HISTORY:
                self.history = self.history[-self.MAX_HISTORY:]
        return result

    def get_status(self):
        with self._lock:
            return {
                "running": self.is_running(),
                "interval": self.interval,
                "last_run": self.last_run,
                "last_result": dict(self.last_result),
            }

    def get_history(self, limit=50):
        with self._lock:
            return [{"ts": ts, **r} for ts, r in self.history[-limit:]]


# ─── HTTP Handler ─────────────────────────────────────

class BridgeHandler(http.server.SimpleHTTPRequestHandler):

    shared_dir = None
    poll_manager = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        routes = {
            "/": lambda: self.serve_static("index.html"),
            "/api/config": self.handle_get_config,
            "/api/agents/discover": self.handle_discover_agents,
            "/api/messages": lambda: self.handle_messages(parsed.query),
            "/api/status": self.handle_status,
            "/api/poll": self.handle_poll_status,
            "/api/bridge/yaml": self.handle_bridge_yaml,
            "/api/rooms": self.handle_get_rooms,
        }
        if path in routes:
            routes[path]()
        elif path.startswith("/api/rooms/") and path.endswith("/messages"):
            self.handle_room_messages(path)
        elif path.startswith("/api/rooms/") and path.endswith("/logs"):
            self.handle_room_logs(path)
        elif path.startswith("/api/rooms/") and path.endswith("/events"):
            self.handle_room_events(path)
        elif path.startswith("/api/rooms/") and path.endswith("/turn"):
            self.handle_room_current_turn(path)
        elif path.startswith("/api/history/"):
            self.handle_history(path)
        else:
            self.serve_static(path.lstrip("/"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        routes = {
            "/api/config": self.handle_update_config,
            "/api/config/full": self.handle_update_config_full,
            "/api/archive": self.handle_archive,
            "/api/open-current-folder": self.handle_open_current_folder,
            "/api/open-dir": self.handle_open_dir,
            "/api/poll/now": self.handle_poll_now,
            "/api/poll/start": self.handle_poll_start,
            "/api/poll/stop": self.handle_poll_stop,
            "/api/poll/history": self.handle_poll_history,
            "/api/send": self.handle_send_message,
            "/api/agent/test": self.handle_test_agent,
            "/api/rooms": self.handle_save_room,
            "/api/rooms/delete": self.handle_delete_room,
        }
        handler = routes.get(parsed.path)
        if handler:
            handler()
        elif parsed.path.startswith("/api/rooms/"):
            self.handle_room_action(parsed.path)
        else:
            self.send_error(404)

    do_PUT = do_POST

    # ─── Static ─────────────────────────────────────

    def serve_static(self, filename):
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
            self.send_error(403)
            return
        if not filepath.exists():
            self.send_error(404)
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
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        with open(filepath, "rb") as f:
            self.wfile.write(f.read())

    # ─── GET /api/config ─────────────────────────────

    def handle_get_config(self):
        shared = Path(self.shared_dir)
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
            })
        self.send_json({
            "ok": True,
            "shared_dir": str(shared),
            "agent_id": cfg.get("agent_id", ""),
            "agents": agents_list,
            "rooms": rooms_list,
            "active_exists": (shared / "active.jsonl").exists(),
        })

    def handle_discover_agents(self):
        shared = Path(self.shared_dir)
        cfg, _ = read_bridge(shared)
        configured = {a.get("id", key) for key, a in cfg.get("agents", {}).items()}
        discovered = discover_local_agents(shared)
        for item in discovered:
            item["configured"] = item["id"] in configured
        self.send_json({
            "ok": True,
            "agents": discovered,
            "count": len(discovered),
        })

    # ─── POST /api/config ───────────────────────────

    def handle_update_config(self):
        body, err = self._read_json_body()
        if err:
            self.send_json(err)
            return

        shared = Path(self.shared_dir)
        cfg, config_path = read_bridge(shared)
        new_agents_input = body.get("agents", [])
        changes = []

        # Validate IDs
        errors = [f"invalid ID: '{a.get('id', '')}'"
                  for a in new_agents_input
                  if not validate_agent_id(a.get("id", "").strip())]
        if errors:
            self.send_json({"ok": False, "error": "; ".join(errors)})
            return
        new_ids = [a.get("id", "").strip() for a in new_agents_input]
        if len(set(new_ids)) != len(new_ids):
            self.send_json({"ok": False, "error": "duplicate agent id"})
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
                    self.send_json({
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
        self.send_json({"ok": True, "agents": agents_list, "changes": changes})

    # ─── PUT /api/config/full ───────────────────────

    def handle_update_config_full(self):
        body, err = self._read_json_body()
        if err:
            self.send_json(err)
            return

        shared = Path(self.shared_dir)
        cfg, config_path = read_bridge(shared)

        # Shared dir
        if body.get("shared_dir"):
            cfg["shared_dir"] = body["shared_dir"]
        if body.get("agent_id"):
            cfg["agent_id"] = body["agent_id"]

        # Agents — 支持传入空列表来清空
        if "agents" in body:
            new_agents_list = body["agents"]
            if new_agents_list:
                errors = [f"invalid ID: '{a.get('id', '')}'"
                          for a in new_agents_list
                          if not validate_agent_id(a.get("id", "").strip())]
                if errors:
                    self.send_json({"ok": False, "error": "; ".join(errors)})
                    return
                new_ids = [a.get("id", "").strip() for a in new_agents_list]
                if len(set(new_ids)) != len(new_ids):
                    self.send_json({"ok": False, "error": "duplicate agent id"})
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
                    self.send_json({
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
                    self.send_json({
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
                    self.send_json({
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
        self.send_json({"ok": True,
                        "saved_agents": saved_agents_out,
                        "message": "config saved"})

    # ─── GET /api/rooms ────────────────────────────

    def handle_get_rooms(self):
        shared = Path(self.shared_dir)
        cfg, _ = read_bridge(shared)
        rooms_list = []
        for key, r in cfg.get("rooms", {}).items():
            room = normalize_room({**r, "id": r.get("id", key)})
            state = read_room_state(shared, room["id"], room)
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
            })
        agents_brief = [
            {"id": a["id"], "display_name": a.get("display_name", a["id"]),
             "color": a.get("color", "#8888a0"), "capability": adapter_capability(a)}
            for a in cfg.get("agents", {}).values()
        ]
        self.send_json({"ok": True, "rooms": rooms_list, "agents": agents_brief})

    # ─── POST /api/rooms ───────────────────────────

    def handle_save_room(self):
        body, err = self._read_json_body()
        if err:
            self.send_json(err)
            return

        shared = Path(self.shared_dir)
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
            self.send_json({"ok": False, "error": "invalid room id"})
            return

        for aid in room_agents:
            if aid not in cfg.get("agents", {}):
                self.send_json({"ok": False, "error": f"unknown agent: '{aid}'"})
                return

        if len(set(room_agents)) != len(room_agents):
            self.send_json({"ok": False, "error": "duplicate agent in room"})
            return
        if len(set(room_order)) != len(room_order):
            self.send_json({"ok": False, "error": "duplicate agent in order"})
            return
        for aid in room_order:
            if aid not in room_agents:
                self.send_json({"ok": False, "error": f"order contains non-member agent: '{aid}'"})
                return
        if room_policy not in ("round_robin", "broadcast"):
            self.send_json({"ok": False, "error": "unsupported room policy"})
            return

        existing_rooms = cfg.get("rooms", {})
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = existing_rooms.get(room_id, {})
        if existing:
            existing_room = normalize_room({**existing, "id": room_id})
            if room_runtime_status(shared, existing_room) == "running":
                existing_order = existing_room.get("order", [])
                if existing_room.get("agents", []) != room_agents or existing_order != room_order:
                    self.send_json({
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
        self.send_json({"ok": True, "room_id": room_id})

    # ─── POST /api/rooms/delete ────────────────────

    def handle_delete_room(self):
        body, err = self._read_json_body()
        if err:
            self.send_json(err)
            return

        room_id = (body.get("id") or "").strip()
        if not room_id:
            self.send_json({"ok": False, "error": "room id required"})
            return

        shared = Path(self.shared_dir)
        cfg, config_path = read_bridge(shared)

        if room_id not in cfg.get("rooms", {}):
            self.send_json({"ok": False, "error": "room not found"})
            return

        room = normalize_room({**cfg["rooms"][room_id], "id": room_id})
        if room_runtime_status(shared, room) == "running":
            self.send_json({"ok": False, "error": "运行中的聊天室不能删除"})
            return

        del cfg["rooms"][room_id]
        sync_filter_from(cfg)
        write_bridge(config_path, cfg)
        self.send_json({"ok": True, "deleted": room_id})

    # ─── POST /api/archive ─────────────────────────

    def _parse_room_api_path(self, path):
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

    def handle_room_messages(self, path):
        room_id, action, _agent_id = self._parse_room_api_path(path)
        if action != "messages":
            self.send_error(404)
            return
        shared = Path(self.shared_dir)
        cfg, _ = read_bridge(shared)
        if room_id not in cfg.get("rooms", {}):
            self.send_json({"ok": False, "error": "room not found"})
            return
        messages = read_room_messages(shared, room_id, include_history=False, limit=500)
        self.send_json({"ok": True, "room_id": room_id, "count": len(messages), "messages": messages})

    def handle_room_logs(self, path):
        room_id, action, _agent_id = self._parse_room_api_path(path)
        if action != "logs":
            self.send_error(404)
            return
        shared = Path(self.shared_dir)
        cfg, _ = read_bridge(shared)
        if room_id not in cfg.get("rooms", {}):
            self.send_json({"ok": False, "error": "room not found"})
            return
        logs = read_room_logs(shared, room_id, limit=500)
        self.send_json({"ok": True, "room_id": room_id, "count": len(logs), "logs": logs})

    def handle_room_action(self, path):
        room_id, action, agent_id = self._parse_room_api_path(path)
        if not room_id:
            self.send_error(404)
            return

        body, err = self._read_json_body()
        if err:
            self.send_json(err)
            return

        shared = Path(self.shared_dir)
        cfg, config_path = read_bridge(shared)
        if room_id not in cfg.get("rooms", {}):
            self.send_json({"ok": False, "error": "room not found"})
            return

        room = normalize_room({**cfg["rooms"][room_id], "id": room_id})

        # ── Agent callback: /api/rooms/{room_id}/agents/{agent_id}/callback ──
        if action == "callback" and agent_id:
            self._handle_agent_callback(cfg, shared, room_id, agent_id, body)
            return

        # ── Agent active message: /api/rooms/{room_id}/agents/{agent_id}/message ──
        if action == "message" and agent_id:
            self._handle_agent_message(cfg, shared, room_id, agent_id, body)
            return

        if action == "send":
            agent_id = (body.get("agent_id") or body.get("from") or "user").strip()
            text = body.get("text", "")
            to_agent = (body.get("to") or "").strip()
            kind = body.get("kind") or ("user" if agent_id == "user" else "agent")
            if not validate_agent_id(agent_id):
                self.send_json({"ok": False, "error": "invalid agent_id"})
                return
            if agent_id != "user" and agent_id not in cfg.get("agents", {}):
                self.send_json({"ok": False, "error": f"unknown agent_id: '{agent_id}'"})
                return
            if agent_id != "user" and agent_id not in room.get("agents", []):
                self.send_json({"ok": False, "error": f"agent is not in room: '{agent_id}'"})
                return
            if to_agent and to_agent not in room.get("agents", []):
                self.send_json({"ok": False, "error": f"target agent is not in room: '{to_agent}'"})
                return
            if not text:
                self.send_json({"ok": False, "error": "text required"})
                return
            msg = append_room_message(shared, room_id, agent_id, text, to_agent=to_agent, kind=kind)
            # v2: emit event + schedule room
            emit_event(shared, room_id, EVT_MESSAGE_CREATED, actor=agent_id,
                       message_id=msg.get("id", ""))
            self._schedule_room(cfg, room_id)
            self.send_json({"ok": True, "room_id": room_id, "message": msg})
            return

        if action == "start":
            if not room.get("agents"):
                self.send_json({"ok": False, "error": "room has no agents"})
                return
            missing = [aid for aid in room.get("agents", []) if aid not in cfg.get("agents", {})]
            if missing:
                self.send_json({"ok": False, "error": "room has unknown agent: " + ", ".join(missing)})
                return
            state = set_room_status(shared, room, "running")
            cfg["rooms"][room_id]["status"] = "running"
            write_bridge(config_path, cfg)
            append_room_log_safely(shared, room_id, "room_started", "聊天室已开始运行")
            # v2: emit event + schedule room
            emit_event(shared, room_id, EVT_ROOM_STARTED)
            self._schedule_room(cfg, room_id)
            self.send_json({"ok": True, "room_id": room_id, "state": state})
            return

        if action == "pause":
            state = set_room_status(shared, room, "paused")
            cfg["rooms"][room_id]["status"] = "paused"
            write_bridge(config_path, cfg)
            append_room_log_safely(shared, room_id, "room_paused", "聊天室已暂停")
            emit_event(shared, room_id, EVT_ROOM_PAUSED)
            self.send_json({"ok": True, "room_id": room_id, "state": state})
            return

        if action == "tick":
            # v2: use runtime state machine if scheduler is running
            sched = get_scheduler()
            if sched and sched.is_running:
                result = run_room_step(cfg, room_id)
            else:
                result = tick_room(cfg, room_id, force=bool(body.get("force", True)))
            self.send_json({"ok": bool(result.get("ok", True)), "result": result})
            return

        if action == "schedule":
            # Manual schedule trigger
            self._schedule_room(cfg, room_id)
            self.send_json({"ok": True, "room_id": room_id, "scheduled": True})
            return

        self.send_error(404)

    # ─── v2 Agent Callback ────────────────────────────

    def _handle_agent_callback(self, cfg, shared, room_id, agent_id, body):
        """Handle POST /api/rooms/{room_id}/agents/{agent_id}/callback"""
        # Token auth
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        token = extract_token_from_request(dict(self.headers), params)
        ok, err = verify_callback_token(cfg, agent_id, token)
        if not ok:
            self.send_json({"ok": False, "error": f"auth failed: {err}"}, status=403)
            return

        # Validate membership
        if not agent_in_room(cfg, room_id, agent_id):
            self.send_json({"ok": False, "error": f"agent {agent_id} not in room {room_id}"}, status=403)
            return

        # Extract message
        message = body.get("message", "")
        turn_id = body.get("turn_id", "")
        correlation_id = body.get("correlation_id", "")
        meta = body.get("meta") or {}

        try:
            message = sanitize_message(message)
        except ValueError as e:
            self.send_json({"ok": False, "error": str(e)})
            return

        result = receive_agent_response(
            shared, room_id, agent_id, message,
            turn_id=turn_id, correlation_id=correlation_id,
            source="callback", meta=meta,
        )

        status = 200 if result.get("ok") else 400
        self.send_json(result, status=status)

    def _handle_agent_message(self, cfg, shared, room_id, agent_id, body):
        """Handle POST /api/rooms/{room_id}/agents/{agent_id}/message"""
        # Token auth
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        token = extract_token_from_request(dict(self.headers), params)
        ok, err = verify_callback_token(cfg, agent_id, token)
        if not ok:
            self.send_json({"ok": False, "error": f"auth failed: {err}"}, status=403)
            return

        if not agent_in_room(cfg, room_id, agent_id):
            self.send_json({"ok": False, "error": f"agent {agent_id} not in room {room_id}"}, status=403)
            return

        message = body.get("message", "")
        mode = body.get("mode", "normal")
        to_agent = body.get("to", "")
        meta = body.get("meta") or {}

        try:
            message = sanitize_message(message)
        except ValueError as e:
            self.send_json({"ok": False, "error": str(e)})
            return

        msg = append_room_message(shared, room_id, agent_id, message,
                                   to_agent=to_agent, kind="agent",
                                   meta={"source": "active_push", "mode": mode, **meta})
        emit_event(shared, room_id, EVT_MESSAGE_CREATED, actor=agent_id,
                   message_id=msg.get("id", ""), meta={"source": "active_push", "mode": mode})
        self._schedule_room(cfg, room_id)
        self.send_json({"ok": True, "room_id": room_id, "agent_id": agent_id,
                        "message_id": msg.get("id", ""), "message": msg})

    def _schedule_room(self, cfg, room_id):
        """Helper: schedule room via v2 scheduler if available."""
        try:
            sched = get_scheduler()
            if sched:
                if not sched._config:
                    sched.set_config(cfg)
                sched.schedule_room(room_id)
        except Exception:
            pass  # Scheduler not available — fall back to poll

    # ─── v2 GET endpoints ─────────────────────────────

    def handle_room_events(self, path):
        """GET /api/rooms/{room_id}/events"""
        room_id, action, _agent_id = self._parse_room_api_path(path)
        if not room_id:
            self.send_error(404)
            return
        shared = Path(self.shared_dir)
        limit = 500
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if "limit" in params:
            try:
                limit = min(int(params["limit"]), 1000)
            except ValueError:
                pass
        events = read_events(shared, room_id, limit=limit)
        self.send_json({"ok": True, "room_id": room_id, "count": len(events), "events": events})

    def handle_room_current_turn(self, path):
        """GET /api/rooms/{room_id}/turn"""
        room_id, action, _agent_id = self._parse_room_api_path(path)
        if not room_id:
            self.send_error(404)
            return
        shared = Path(self.shared_dir)
        cfg, _ = read_bridge(shared)
        if room_id not in cfg.get("rooms", {}):
            self.send_json({"ok": False, "error": "room not found"})
            return
        room_cfg = normalize_room({**cfg["rooms"][room_id], "id": room_id})
        state = read_room_state(shared, room_id, room_cfg)
        state = migrate_room_state(state, room_cfg)
        current_turn = state.get("current_turn")
        self.send_json({"ok": True, "room_id": room_id, "current_turn": current_turn,
                        "status": state.get("status"), "turn_index": state.get("turn_index")})

    def handle_archive(self):
        shared = Path(self.shared_dir)
        active = shared / "active.jsonl"
        if not active.exists():
            self.send_json({"ok": False, "error": "no active file"})
            return
        msgs = parse_jsonl(active)
        if not msgs:
            self.send_json({"ok": False, "error": "active file is empty"})
            return
        name = do_archive(shared)
        if name:
            self.send_json({"ok": True, "archived_to": name, "message_count": len(msgs)})
        else:
            self.send_json({"ok": False, "error": "archive failed"})

    # ─── POST /api/open-current-folder ───────────────────

    def handle_open_current_folder(self):
        body, err = self._read_json_body()
        if err:
            self.send_json(err)
            return

        archive_name = body.get("archive", "__active__")
        shared = Path(self.shared_dir)
        try:
            chat_file = resolve_chat_file(shared, archive_name)
        except FileNotFoundError:
            self.send_json({"ok": False, "error": "archive file not found"})
            return
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)})
            return

        ok, error = open_in_file_manager(chat_file, select=True)
        if not ok:
            self.send_json({"ok": False, "error": error or "failed to open folder"})
            return

        self.send_json({"ok": True, "path": str(chat_file)})

    def handle_open_dir(self):
        body, err = self._read_json_body()
        if err:
            self.send_json(err)
            return
        dir_path = body.get("path", "")
        if not dir_path:
            self.send_json({"ok": False, "error": "path is required"})
            return
        ok, error = open_in_file_manager(dir_path)
        if not ok:
            self.send_json({"ok": False, "error": error or "failed to open directory"})
            return
        self.send_json({"ok": True, "path": str(dir_path)})

    # ─── Poll API ───────────────────────────────────

    def handle_poll_status(self):
        status = self.poll_manager.get_status() if self.poll_manager else {
            "running": False, "interval": 0, "last_run": None, "last_result": {}
        }
        self.send_json({"ok": True, **status})

    def handle_poll_now(self):
        if not self.poll_manager:
            self.send_json({"ok": False, "error": "poll manager not initialized"})
            return
        result = self.poll_manager.poll_now()
        self.send_json({"ok": True, "result": result})

    def handle_poll_start(self):
        if not self.poll_manager:
            self.send_json({"ok": False, "error": "poll manager not initialized"})
            return
        self.poll_manager.start()
        self.send_json({"ok": True, "running": True})

    def handle_poll_stop(self):
        if not self.poll_manager:
            self.send_json({"ok": False, "error": "poll manager not initialized"})
            return
        self.poll_manager.stop()
        self.send_json({"ok": True, "running": False})

    def handle_poll_history(self):
        if not self.poll_manager:
            self.send_json({"ok": False, "error": "poll manager not initialized"})
            return
        body, _ = self._read_json_body()
        limit = (body or {}).get("limit", 50)
        history = self.poll_manager.get_history(limit)
        self.send_json({"ok": True, "history": history})

    def handle_send_message(self):
        body, err = self._read_json_body()
        if err:
            self.send_json(err)
            return

        agent_id = body.get("agent_id", "")
        text = body.get("text", "")
        if not agent_id or not text:
            self.send_json({"ok": False, "error": "agent_id and text required"})
            return

        # 验证 agent_id 是否在配置中定义
        shared = Path(self.shared_dir)
        cfg, _ = read_bridge(shared)
        known_agents = cfg.get("agents", {})
        if agent_id not in known_agents:
            self.send_json({"ok": False, "error": f"unknown agent_id: '{agent_id}'"})
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

        self.send_json({"ok": True, "agent_id": agent_id, "chars": len(text)})

    def handle_test_agent(self):
        body, err = self._read_json_body()
        if err:
            self.send_json(err)
            return

        wakeup = body.get("wakeup", {})
        adapter = body.get("adapter")
        if adapter:
            wakeup = adapter_to_wakeup(adapter)
        url = wakeup.get("url", "").strip()
        if not url:
            self.send_json({"ok": False, "error": "Webhook URL not configured"})
            return

        success, detail, _body = wakeup_agent(wakeup, "[Agent Bridge] connectivity test", "agent-bridge")
        if success:
            self.send_json({"ok": True, "status": detail})
        else:
            msg = _classify_conn_error(detail, url)
            self.send_json({"ok": False, "error": msg, "raw": detail})

    def handle_bridge_yaml(self):
        shared = Path(self.shared_dir)
        config_path = shared / BRIDGE_FILENAME
        if not config_path.exists():
            self.send_json({"ok": False, "error": "bridge.yaml not found"})
            return
        text = config_path.read_text(encoding="utf-8")
        self.send_json({"ok": True, "yaml": text})

    # ─── GET /api/messages ──────────────────────────

    def handle_messages(self, query):
        params = urllib.parse.parse_qs(query)
        archive = params.get("archive", [None])[0]
        search = params.get("q", [None])[0]
        room_id = params.get("room", [None])[0]
        try:
            limit = int(params.get("limit", [500])[0])
        except (ValueError, TypeError):
            limit = 500
        shared = Path(self.shared_dir)
        all_msgs = []

        for m in parse_jsonl(shared / "active.jsonl"):
            m["_source"] = "active"
            all_msgs.append(m)

        if archive:
            archive_path = (shared / "history" / archive).resolve()
            if not is_relative_to(archive_path, (shared / "history").resolve()):
                self.send_json({"ok": False, "error": "invalid archive name"})
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

        self.send_json({"ok": True, "count": len(all_msgs), "messages": all_msgs})

    # ─── GET /api/status ────────────────────────────

    def handle_status(self):
        shared = Path(self.shared_dir)
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

        self.send_json({
            "ok": True,
            "active": {
                "size": active.stat().st_size if active.exists() else 0,
                "count": len(active_msgs),
                "path": str(active),
            },
            "history": history_files,
            "history_count": len(history_files),
        })

    # ─── GET /api/history/<name> ────────────────────

    def handle_history(self, path):
        filename = path.replace("/api/history/", "")
        if not filename.endswith(".jsonl"):
            self.send_error(400, "Only .jsonl files")
            return
        # 防止路径遍历：使用 is_relative_to 替代字符串前缀匹配
        shared = Path(self.shared_dir)
        filepath = (shared / "history" / filename).resolve()
        if not is_relative_to(filepath, (shared / "history").resolve()):
            self.send_error(403)
            return
        if not filepath.exists():
            self.send_error(404)
            return
        msgs = parse_jsonl(filepath)
        self.send_json({"ok": True, "name": filename, "count": len(msgs), "messages": msgs})

    # ─── Helpers ──────────────────────────────────────

    MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB

    def _read_json_body(self):
        """Read and parse JSON body with size limit. Returns (body, error_response)."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None, {"ok": False, "error": "empty body"}
        if length > self.MAX_BODY_SIZE:
            return None, {"ok": False, "error": "request body too large"}
        try:
            return json.loads(self.rfile.read(length)), None
        except json.JSONDecodeError:
            return None, {"ok": False, "error": "invalid JSON"}

    def send_json(self, data):
        text = json.dumps(data, ensure_ascii=False)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        # 动态回显请求 Origin，仅允许可信的 localhost 来源
        origin = self.headers.get("Origin", "")
        if origin and re.match(r'^http://127\.0\.0\.1:\d+$', origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def log_message(self, fmt, *args):
        msg = fmt % args
        _suppress_patterns = ["/api/messages", "/api/poll", "/api/status"]
        if any(p in msg for p in _suppress_patterns):
            return
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ─── 启动 ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Agent Bridge — UI + polling server")
    parser.add_argument("--dir", "-d", help="Shared chat directory (auto-detect)")
    parser.add_argument("--port", "-p", type=int, default=7899, help="Port (default: 7899)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind (default: 127.0.0.1)")
    parser.add_argument("--open", "-o", action="store_true", help="Open browser")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
                        help=f"Poll interval in seconds (default: {DEFAULT_POLL_INTERVAL})")
    parser.add_argument("--no-poll", action="store_true",
                        help="Disable automatic polling (manual poll via API only)")

    args = parser.parse_args()
    shared_dir = args.dir or str(find_shared_dir())
    BridgeHandler.shared_dir = shared_dir

    Path(shared_dir).mkdir(parents=True, exist_ok=True)

    # 确保 bridge.yaml 存在
    cfg, cfg_path = read_bridge(Path(shared_dir))
    if not cfg_path.exists():
        write_bridge(cfg_path, cfg)

    # 初始化后台轮询
    poll_mgr = PollManager(shared_dir, args.poll_interval)
    BridgeHandler.poll_manager = poll_mgr
    if not args.no_poll:
        poll_mgr.start()

    try:
        server = http.server.HTTPServer((args.host, args.port), BridgeHandler)
    except OSError as e:
        print(f"Error: Cannot bind to {args.host}:{args.port} — {e}")
        print(f"Hint: Port may be in use. Try --port with a different number (e.g. --port {args.port + 1}).")
        sys.exit(1)

    url = f"http://{args.host}:{args.port}"

    poll_text = f"every {args.poll_interval}s" if not args.no_poll else "disabled"
    print("=" * 44)
    print("Agent Bridge - UI + Poll")
    print(f"Shared dir: {shared_dir}")
    print(f"URL:        {url}")
    print(f"Polling:    {poll_text}")
    print("Ctrl+C to stop")
    print("=" * 44)

    if args.open:
        import webbrowser
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
        poll_mgr.stop()
        server.server_close()


if __name__ == "__main__":
    main()
