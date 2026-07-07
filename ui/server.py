#!/usr/bin/env python3
"""
Agent Bridge — 本地 UI 服务（精简入口）

集成了轮询 + 配置管理 + 聊天时间线。
只需要跑这一个进程。

用法:
    python3 server.py                          # 默认 8825 端口
    python3 server.py --open                   # 自动打开浏览器
    python3 server.py --poll-interval 60       # 每 60 秒轮询一次
"""
import argparse
import http.server
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

# 从 core/ 导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from scheduler import get_scheduler

# 从同目录子模块导入
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (
    BRIDGE_FILENAME,
    DEFAULT_POLL_INTERVAL,
    find_shared_dir,
    read_bridge,
    write_bridge,
)
from poll_manager import PollManager
import routes


# ─── HTTP Handler ─────────────────────────────────────

class BridgeHandler(http.server.SimpleHTTPRequestHandler):

    shared_dir = None
    poll_manager = None

    def send_error(self, code, message=None, explain=None):
        """覆写基类，为错误响应也添加安全头。"""
        self.send_response(code)
        self.send_header("Content-Security-Policy", routes.CSP_HEADER)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        body = explain or message or ""
        if body:
            self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        route_map = {
            "/": lambda: routes.serve_static(self, "index.html"),
            "/api/config": lambda: routes.handle_get_config(self),
            "/api/agents/discover": lambda: routes.handle_discover_agents(self),
            "/api/messages": lambda: routes.handle_messages(self, parsed.query),
            "/api/status": lambda: routes.handle_status(self),
            "/api/poll": lambda: routes.handle_poll_status(self),
            "/api/bridge/yaml": lambda: routes.handle_bridge_yaml(self),
            "/api/rooms": lambda: routes.handle_get_rooms(self),
            "/api/settings": lambda: routes.handle_get_settings(self),
        }
        if path in route_map:
            route_map[path]()
        elif path.startswith("/api/rooms/") and path.endswith("/messages"):
            routes.handle_room_messages(self, path)
        elif path.startswith("/api/rooms/") and path.endswith("/logs"):
            routes.handle_room_logs(self, path)
        elif path.startswith("/api/rooms/") and path.endswith("/events"):
            routes.handle_room_events(self, path)
        elif path.startswith("/api/rooms/") and path.endswith("/turn"):
            routes.handle_room_current_turn(self, path)
        elif path.startswith("/api/history/"):
            routes.handle_history(self, path)
        else:
            routes.serve_static(self, path.lstrip("/"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route_map = {
            "/api/config": lambda: routes.handle_update_config(self),
            "/api/config/full": lambda: routes.handle_update_config_full(self),
            "/api/archive": lambda: routes.handle_archive(self),
            "/api/open-current-folder": lambda: routes.handle_open_current_folder(self),
            "/api/open-dir": lambda: routes.handle_open_dir(self),
            "/api/poll/now": lambda: routes.handle_poll_now(self),
            "/api/poll/start": lambda: routes.handle_poll_start(self),
            "/api/poll/stop": lambda: routes.handle_poll_stop(self),
            "/api/poll/history": lambda: routes.handle_poll_history(self),
            "/api/send": lambda: routes.handle_send_message(self),
            "/api/agent/test": lambda: routes.handle_test_agent(self),
            "/api/agent/integration-test": lambda: routes.handle_agent_integration_test(self),
            "/api/rooms": lambda: routes.handle_save_room(self),
            "/api/rooms/delete": lambda: routes.handle_delete_room(self),
            "/api/settings": lambda: routes.handle_update_settings(self),
        }
        handler = route_map.get(parsed.path)
        if handler:
            handler()
        elif parsed.path.startswith("/api/rooms/"):
            routes.handle_room_action(self, parsed.path)
        elif parsed.path.startswith("/api/agents/") and parsed.path.count("/") >= 3:
            # PUT /api/agents/{agent_id} — single agent update
            agent_id = parsed.path.rstrip("/").split("/")[-1]
            routes.handle_update_single_agent(self, agent_id)
        else:
            self.send_error(404)

    do_PUT = do_POST

    # ─── Helpers (delegated to routes) ──────────────

    def _read_json_body(self):
        """Read and parse JSON body with size limit."""
        return routes._read_json_body(self)

    def send_json(self, data, status=200):
        """Send a JSON response."""
        routes._send_json(self, data, status=status)

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
    parser.add_argument("--port", "-p", type=int, default=8825, help="Port (default: 8825)")
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

    # 读取 bridge.yaml（记录是否已存在）
    bridge_yaml_path = Path(shared_dir) / BRIDGE_FILENAME
    bridge_existed = bridge_yaml_path.exists()
    cfg, cfg_path = read_bridge(Path(shared_dir))
    if not cfg_path.exists():
        write_bridge(cfg_path, cfg)

    # 初始化后台轮询（优先使用 bridge.yaml 中保存的 settings）
    from routes import _read_settings
    saved_settings = _read_settings(shared_dir)
    poll_interval = args.poll_interval or saved_settings.get("poll_interval", DEFAULT_POLL_INTERVAL)
    auto_start_poll = saved_settings.get("auto_start_poll", True)
    poll_mgr = PollManager(shared_dir, poll_interval)
    BridgeHandler.poll_manager = poll_mgr
    if not args.no_poll and auto_start_poll:
        poll_mgr.start()

    # 初始化 V2 Scheduler（仅在 bridge.yaml 已存在时启动）
    sched = get_scheduler()
    if bridge_existed:
        cfg, cfg_path = read_bridge(Path(shared_dir))
        sched.set_config(cfg)
        sched.start()
        sched.scan_running_rooms(cfg)
    else:
        print("[main] bridge.yaml not found, Scheduler not auto-started. "
              "Will be started on first PollManager cycle.")

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
        sched.stop()
        poll_mgr.stop()
        server.server_close()


if __name__ == "__main__":
    main()
