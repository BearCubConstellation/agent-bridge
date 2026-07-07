#!/usr/bin/env python3
"""Generic AgentBridge Channel sidecar.

Run one instance per Agent runtime. It turns a runtime's normal inbound webhook
and normal outbound hook into the AgentBridge WebSocket channel protocol. This
is the integration point for OpenClaw and Hermes; it does not inject callback
instructions into model messages.

Example:
  python integrations/channel_connector.py --config susu-channel.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    from websockets.sync.client import connect
except ImportError as exc:  # pragma: no cover
    raise SystemExit("websockets>=12 is required: {}".format(exc))

LOG = logging.getLogger("agent_bridge.channel_connector")


class ChannelConnector:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.agent_id = str(config["agent_id"])
        self.hub_url = str(config["hub_url"])
        self.token = str(config.get("token") or "")
        self.outgoing: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stop = threading.Event()
        self._socket = None
        self._socket_lock = threading.Lock()
        self._callback_server = None

    def run(self) -> None:
        self._start_callback_server()
        delay = 1
        while not self._stop.is_set():
            try:
                with connect(self.hub_url, open_timeout=10, max_size=256 * 1024) as socket:
                    with self._socket_lock:
                        self._socket = socket
                    socket.send(json.dumps({"type": "register", "agent_id": self.agent_id, "token": self.token}))
                    registered = json.loads(socket.recv(timeout=10))
                    if registered.get("type") != "registered":
                        raise RuntimeError("registration rejected: {}".format(registered))
                    LOG.info("registered as %s", self.agent_id)
                    delay = 1
                    self._bridge_loop(socket)
            except Exception as exc:
                LOG.warning("channel disconnected: %s; retrying in %ss", exc, delay)
                self._stop.wait(delay)
                delay = min(delay * 2, 30)
            finally:
                with self._socket_lock:
                    self._socket = None

    def stop(self) -> None:
        self._stop.set()
        if self._callback_server:
            self._callback_server.shutdown()
        with self._socket_lock:
            if self._socket:
                self._socket.close()

    def _bridge_loop(self, socket) -> None:
        while not self._stop.is_set():
            try:
                outbound = self.outgoing.get_nowait()
            except queue.Empty:
                outbound = None
            if outbound:
                socket.send(json.dumps(outbound, ensure_ascii=False))
            try:
                raw = socket.recv(timeout=0.2)
            except TimeoutError:
                continue
            event = json.loads(raw)
            if event.get("type") == "message":
                message = event.get("message") or {}
                self._inject_into_runtime(message)
                socket.send(json.dumps({"type": "ack", "message_id": message.get("id", "")}))

    def _inject_into_runtime(self, message: Dict[str, Any]) -> None:
        inject = self.config.get("inject") or {}
        url = inject.get("url")
        if not url:
            raise RuntimeError("inject.url is required")
        template = inject.get("template") or {
            "room_id": "{room_id}", "from": "{from}", "text": "{text}", "message_id": "{id}", "trace_id": "{trace_id}",
        }
        body = self._render(template, message)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **(inject.get("headers") or {})}
        request = urllib.request.Request(url, data=payload, headers=headers, method=str(inject.get("method", "POST")).upper())
        with urllib.request.urlopen(request, timeout=int(inject.get("timeout", 30))) as response:
            if response.status >= 300:
                raise RuntimeError("runtime inject returned HTTP {}".format(response.status))

    def _start_callback_server(self) -> None:
        callback = self.config.get("outbound_listener") or {}
        if not callback.get("enabled", True):
            return
        host = str(callback.get("host", "127.0.0.1"))
        port = int(callback.get("port", 8830))
        connector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path.rstrip("/") != "/outbound":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    connector.queue_outbound(payload)
                    self.send_response(202)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                except Exception as exc:
                    self.send_error(400, str(exc))

            def log_message(self, _format, *_args):
                return

        self._callback_server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self._callback_server.serve_forever, name="channel-outbound-listener", daemon=True).start()
        LOG.info("outbound hook: http://%s:%s/outbound", host, port)

    def queue_outbound(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("outbound payload must be an object")
        event = {
            "type": "message",
            "room_id": payload.get("room_id") or payload.get("roomId"),
            "to": payload.get("to"),
            "text": payload.get("text") or payload.get("message"),
            "reply_to": payload.get("reply_to") or payload.get("replyTo") or "",
            "metadata": payload.get("metadata") or {},
        }
        if payload.get("id"):
            event["id"] = payload["id"]
        self.outgoing.put(event)

    @staticmethod
    def _render(value: Any, context: Dict[str, Any]) -> Any:
        if isinstance(value, str):
            try:
                return value.format(**context)
            except KeyError:
                return value
        if isinstance(value, list):
            return [ChannelConnector._render(item, context) for item in value]
        if isinstance(value, dict):
            return {key: ChannelConnector._render(item, context) for key, item in value.items()}
        return value


def load_config(path: str) -> Dict[str, Any]:
    content = Path(path).read_text(encoding="utf-8")
    if path.endswith(".json"):
        return json.loads(content)
    if yaml is None:
        raise RuntimeError("PyYAML is required for YAML config")
    return yaml.safe_load(content) or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentBridge generic channel connector")
    parser.add_argument("--config", required=True, help="YAML or JSON connector config")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    connector = ChannelConnector(load_config(args.config))
    try:
        connector.run()
    except KeyboardInterrupt:
        connector.stop()


if __name__ == "__main__":
    main()
