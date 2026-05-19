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
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

# 从 core/ 导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from lock import file_lock
from poll import do_archive, run_poll, load_config as load_poll_config


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


# ─── 配置 ─────────────────────────────────────────────

def find_shared_dir():
    for d in [Path.home() / ".agent-bridge", Path.home() / ".shared-chat"]:
        active = d / "active.jsonl"
        if d.exists() and (active.exists() or (d / "history").exists()):
            return d
    return Path.home() / ".agent-bridge"


# parse_jsonl 从 core/poll.py 导入（避免重复定义）
from poll import parse_jsonl  # noqa: E402


def default_agents(shared_dir):
    msgs = parse_jsonl(shared_dir / "active.jsonl")
    found = set()
    for m in msgs:
        if m.get("from"):
            found.add(m["from"])
    palette = ["#ff6b6b", "#4ecdc4", "#ffd93d", "#a29bfe", "#fd79a8", "#00cec9"]
    agents = {}
    for i, aid in enumerate(sorted(found)):
        agents[aid] = {
            "id": aid,
            "display_name": aid.capitalize(),
            "color": palette[i % len(palette)],
            "cursor": "line",
            "filter_from": "",
            "wakeup": {"url": "", "method": "POST", "body_template": {"message": "{{message}}"}},
        }
    if agents:
        return agents
    agents = {
        "alice": {
            "id": "alice",
            "display_name": "Alice",
            "color": "#ff6b6b",
            "cursor": "line",
            "filter_from": "bob",
            "wakeup": {
                "url": "",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_template": {"message": "{{message}}"},
            },
        },
        "bob": {
            "id": "bob",
            "display_name": "Bob",
            "color": "#4ecdc4",
            "cursor": "line",
            "filter_from": "alice",
            "wakeup": {
                "url": "",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body_template": {"message": "{{message}}"},
            },
        },
    }
    return agents


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


def _discovered_agent(agent_id, display_name, kind, source, details="", wakeup=None):
    item = {
        "id": agent_id,
        "display_name": display_name,
        "kind": kind,
        "source": _agent_source(source),
        "details": details,
    }
    if wakeup:
        item["wakeup"] = wakeup
    return item


def discover_local_agents(shared_dir):
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

    cfg, cfg_path = read_bridge(shared)
    for key, agent in cfg.get("agents", {}).items():
        aid = agent.get("id", key)
        add(_discovered_agent(
            aid,
            agent.get("display_name", aid),
            "Bridge 配置",
            cfg_path,
            "当前 bridge.yaml 中已配置",
            agent.get("wakeup", {}),
        ))

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
        add(_discovered_agent(
            "hermes",
            "Hermes Agent",
            "Hermes",
            hermes_config,
            "检测到 ~/.hermes/config.yaml",
            {
                "url": f"http://{host}:{port}/webhooks/{route}",
                "method": "POST",
                "body_template": {"message": "{{message}}"},
            },
        ))
    elif (home / ".hermes").exists():
        add(_discovered_agent("hermes", "Hermes Agent", "Hermes", home / ".hermes", "检测到 ~/.hermes 目录"))

    # OpenClaw.
    openclaw_config = home / ".openclaw" / "openclaw.json"
    if openclaw_config.exists() or (home / ".openclaw").exists():
        add(_discovered_agent(
            "openclaw",
            "OpenClaw",
            "OpenClaw",
            openclaw_config if openclaw_config.exists() else home / ".openclaw",
            "检测到 OpenClaw 本地配置",
            {
                "url": "http://127.0.0.1:18789/tools/invoke",
                "method": "POST",
                "auth": {
                    "type": "bearer",
                    "token_path": "~/.openclaw/openclaw.json",
                    "token_jsonpath": "gateway.auth.password",
                },
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
    if "agents" not in cfg or not cfg["agents"]:
        cfg["agents"] = default_agents(shared_dir)
    cfg.setdefault("agent_id", "")
    if not cfg["agent_id"] and cfg["agents"]:
        cfg["agent_id"] = next(iter(cfg["agents"].keys()))
    for key, a in cfg["agents"].items():
        a.setdefault("display_name", a.get("id", key).capitalize())
        a.setdefault("color", "#ff6b6b" if list(cfg["agents"].keys())[0] == key else "#4ecdc4")
        a.setdefault("id", key)
        a.setdefault("cursor", "line")
        a.setdefault("wakeup", {"url": "", "method": "POST", "body_template": {"message": "{{message}}"}})
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
    """管理后台轮询线程。"""

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
        """立即触发一次轮询（同步执行）。"""
        return self._do_poll()

    def is_running(self):
        return self.running and (self._thread is None or self._thread.is_alive())

    def _loop(self):
        while not self._stop.is_set():
            self._do_poll()
            self._stop.wait(self.interval)

    def _do_poll(self):
        """执行一次轮询，更新 last_result 和 last_run。"""
        config_path = Path(self.shared_dir) / BRIDGE_FILENAME
        if not config_path.exists():
            return self.last_result

        try:
            config = load_poll_config(str(config_path))
            result = run_poll(config)
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
        }
        if path in routes:
            routes[path]()
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
            "/api/poll/now": self.handle_poll_now,
            "/api/poll/start": self.handle_poll_start,
            "/api/poll/stop": self.handle_poll_stop,
            "/api/poll/history": self.handle_poll_history,
            "/api/send": self.handle_send_message,
        }
        handler = routes.get(parsed.path)
        if handler:
            handler()
        else:
            self.send_error(404)

    do_PUT = do_POST

    # ─── Static ─────────────────────────────────────

    def serve_static(self, filename):
        script_dir = Path(__file__).resolve().parent
        filepath = (script_dir / filename).resolve()
        # 防止路径遍历：确保解析后的路径仍在 script_dir 下
        if not is_relative_to(filepath, script_dir):
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
                "wakeup": a.get("wakeup", {}),
            })
        self.send_json({
            "ok": True,
            "shared_dir": str(shared),
            "agent_id": cfg.get("agent_id", ""),
            "agents": agents_list,
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

        old_lookup = {}
        for key, a in cfg.get("agents", {}).items():
            old_lookup[a["id"]] = (key, a)

        for item in new_agents_input:
            new_id = item.get("id", "").strip()
            old_id = item.get("old_id", "")
            display_name = item.get("display_name", "").strip()
            color = item.get("color", "").strip()

            if old_id and old_id != new_id and old_id in old_lookup:
                old_key, old_agent = old_lookup[old_id]
                if old_key in cfg["agents"]:
                    del cfg["agents"][old_key]
                old_agent["id"] = new_id
                cfg["agents"][new_id] = old_agent
                cursor_moved = rename_cursor(shared, old_id, new_id)
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
                    if auth and auth.get("type") == "bearer" and auth.get("token_path"):
                        wakeup["auth"] = {
                            "type": "bearer",
                            "token_path": auth["token_path"],
                            "token_jsonpath": auth.get("token_jsonpath", ""),
                        }
                    entry["wakeup"] = wakeup
                    agents_dict[aid] = entry

                cfg["agents"] = agents_dict
                saved_agents = list(agents_dict.keys())
            else:
                # 传入空列表：清空 agents
                cfg["agents"] = {}
                saved_agents = []

        write_bridge(config_path, cfg)
        saved_agents_out = saved_agents if "agents" in body else []
        self.send_json({"ok": True,
                        "saved_agents": saved_agents_out,
                        "message": "配置已保存"})

    # ─── POST /api/archive ─────────────────────────

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
        length = int(self.headers.get("Content-Length", 0))
        limit = 50
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length))
                limit = body.get("limit", 50)
            except Exception:
                pass
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
        limit = int(params.get("limit", [500])[0])
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
        if "GET /api/messages" in msg or "GET /api/poll" in msg:
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

    server = http.server.HTTPServer((args.host, args.port), BridgeHandler)
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
