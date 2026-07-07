#!/usr/bin/env python3
"""Agent Bridge local UI and HTTP API server."""
from __future__ import annotations

import argparse
import http.server
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
from scheduler import get_scheduler
from security import (
    extract_token_from_request,
    is_loopback_host,
    validate_network_exposure,
    verify_mcp_token,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import BRIDGE_FILENAME, DEFAULT_POLL_INTERVAL, find_shared_dir, read_bridge, write_bridge
from poll_manager import PollManager
import routes


class BridgeHandler(http.server.SimpleHTTPRequestHandler):
    shared_dir = None
    poll_manager = None
    bind_host = "127.0.0.1"

    def send_error(self, code, message=None, explain=None):
        self.send_response(code)
        self.send_header("Content-Security-Policy", routes.CSP_HEADER)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        body = explain or message or ""
        if body:
            self.wfile.write(body.encode("utf-8"))

    def _mcp_authorized(self, parsed):
        shared = Path(self.shared_dir)
        config, _ = read_bridge(shared)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        token = extract_token_from_request(dict(self.headers), params)
        ok, error = verify_mcp_token(config, token, allow_unauthenticated=is_loopback_host(self.bind_host))
        if ok:
            return True
        routes._send_json(self, {"ok": False, "error": "mcp auth failed: {}".format(error)}, status=403)
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/mcp") and not self._mcp_authorized(parsed):
            return
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
            "/api/mcp/tools": lambda: routes.handle_mcp_tools_list(self),
            "/api/mcp/config": lambda: routes.handle_mcp_config(self),
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
        if parsed.path.startswith("/api/mcp") and not self._mcp_authorized(parsed):
            return
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
            "/api/mcp": lambda: routes.handle_mcp_jsonrpc(self),
        }
        handler = route_map.get(parsed.path)
        if handler:
            handler()
        elif parsed.path.startswith("/api/rooms/"):
            routes.handle_room_action(self, parsed.path)
        elif parsed.path.startswith("/api/agents/") and parsed.path.count("/") >= 3:
            routes.handle_update_single_agent(self, parsed.path.rstrip("/").split("/")[-1])
        elif parsed.path.startswith("/api/mcp/tools/call/"):
            routes.handle_mcp_tools_call(self, parsed.path.rstrip("/").split("/")[-1])
        else:
            self.send_error(404)

    do_PUT = do_POST

    def _read_json_body(self):
        return routes._read_json_body(self)

    def send_json(self, data, status=200):
        routes._send_json(self, data, status=status)

    def log_message(self, fmt, *args):
        message = fmt % args
        if any(pattern in message for pattern in ("/api/messages", "/api/poll", "/api/status")):
            return
        print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), message))


def main():
    parser = argparse.ArgumentParser(description="Agent Bridge — UI + polling server")
    parser.add_argument("--dir", "-d", help="Shared chat directory (auto-detect)")
    parser.add_argument("--port", "-p", type=int, default=8825, help="Port (default: 8825)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--open", "-o", action="store_true", help="Open browser")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--no-poll", action="store_true", help="Disable automatic polling")
    args = parser.parse_args()

    shared_dir = args.dir or str(find_shared_dir())
    Path(shared_dir).mkdir(parents=True, exist_ok=True)
    BridgeHandler.shared_dir = shared_dir
    BridgeHandler.bind_host = args.host

    bridge_path = Path(shared_dir) / BRIDGE_FILENAME
    bridge_existed = bridge_path.exists()
    config, config_path = read_bridge(Path(shared_dir))
    if not config_path.exists():
        write_bridge(config_path, config)
    exposure_error = validate_network_exposure(config, args.host)
    if exposure_error:
        parser.error(exposure_error)

    # Make callback URLs match the process that is actually listening.
    server_cfg = dict(config.get("server") or {})
    server_cfg.update({"host": args.host, "port": args.port})
    config["server"] = server_cfg
    write_bridge(config_path, config)

    settings = routes._read_settings(shared_dir)
    poll_interval = args.poll_interval or settings.get("poll_interval", DEFAULT_POLL_INTERVAL)
    poll_manager = PollManager(shared_dir, poll_interval)
    BridgeHandler.poll_manager = poll_manager
    if not args.no_poll and settings.get("auto_start_poll", True):
        poll_manager.start()

    scheduler = get_scheduler()
    scheduler.set_config(config)
    if bridge_existed:
        scheduler.start()
        scheduler.scan_running_rooms(config)

    try:
        server = http.server.ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    except OSError as error:
        if bridge_existed:
            scheduler.stop()
        poll_manager.stop()
        parser.error("cannot bind {}:{} — {}".format(args.host, args.port, error))

    url = "http://{}:{}".format(args.host, args.port)
    print("=" * 44)
    print("Agent Bridge - UI + Poll")
    print("Shared dir: {}".format(shared_dir))
    print("URL:        {}".format(url))
    print("MCP HTTP:   {}/api/mcp".format(url))
    print("Ctrl+C to stop")
    print("=" * 44)

    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        scheduler.stop()
        poll_manager.stop()
        server.server_close()


if __name__ == "__main__":
    main()
