#!/usr/bin/env python3
"""Local AgentBridge channel hub.

The hub is a message bus, not a turn orchestrator. Clients register once over a
WebSocket, receive normal chat messages, acknowledge deliveries, and publish
normal replies. Rooms, persistence and audit logs remain owned by AgentBridge.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from channel_protocol import error_event, normalize_message, registration_response
from lock import file_lock
from rooms import append_room_log, append_room_message, ensure_room, normalize_room
from security import is_loopback_host, resolve_token

logger = logging.getLogger(__name__)

try:
    import websockets
except ImportError:  # pragma: no cover - exercised by startup diagnostics
    websockets = None


class ChannelHub:
    """Persistent WebSocket hub with per-agent offline inboxes and ACKs."""

    def __init__(self, shared_dir, config_provider: Callable[[], Dict[str, Any]]):
        self.shared_dir = Path(shared_dir)
        self._config_provider = config_provider
        self._connections: Dict[str, Any] = {}
        self._connections_lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._start_error = ""
        self._host = "127.0.0.1"
        self._port = 8826

    @property
    def root(self) -> Path:
        return self.shared_dir / "channels"

    def _path(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _config(self) -> Dict[str, Any]:
        config = self._config_provider() or {}
        channel = config.get("channel") or {}
        return channel if isinstance(channel, dict) else {}

    def validate_exposure(self, host: str) -> str:
        config = self._config()
        if is_loopback_host(host):
            return ""
        tokens = config.get("tokens") or {}
        if not tokens:
            return "non-loopback channel bind requires channel.tokens"
        return ""

    def start(self, host: Optional[str] = None, port: Optional[int] = None, timeout: float = 5.0) -> bool:
        if self.is_running:
            return True
        if websockets is None:
            self._start_error = "websockets package is not installed"
            return False
        config = self._config()
        self._host = str(host or config.get("host") or "127.0.0.1")
        self._port = int(port or config.get("port") or 8826)
        self._start_error = self.validate_exposure(self._host)
        if self._start_error:
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        self._ready.clear()
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run_loop, name="agent-bridge-channel", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.is_running

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        loop = self._loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=timeout)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        self._loop = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._start_error)

    def status(self) -> Dict[str, Any]:
        with self._connections_lock:
            agents = sorted(self._connections)
        return {
            "enabled": bool(self._config().get("enabled", True)),
            "running": self.is_running,
            "host": self._host,
            "port": self._port,
            "online_agents": agents,
            "error": self._start_error,
        }

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
            self._ready.set()
            self._loop.run_forever()
        except Exception as exc:
            self._start_error = str(exc)
            logger.exception("channel hub failed to start")
            self._ready.set()
        finally:
            try:
                self._loop.run_until_complete(self._shutdown())
            except Exception:
                pass
            self._loop.close()

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handle_socket, self._host, self._port, max_size=256 * 1024)

    async def _shutdown(self) -> None:
        server, self._server = self._server, None
        if server:
            server.close()
            await server.wait_closed()
        with self._connections_lock:
            sockets = list(self._connections.values())
            self._connections.clear()
        for socket in sockets:
            try:
                await socket.close(code=1001, reason="channel hub stopping")
            except Exception:
                pass
        if self._loop and self._loop.is_running():
            self._loop.call_soon(self._loop.stop)

    async def _handle_socket(self, websocket) -> None:
        agent_id = ""
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=15)
            event = self._decode(raw)
            if event.get("type") != "register":
                await websocket.send(json.dumps(error_event("register_required", "first event must be register")))
                return
            agent_id = self._authenticate_registration(event)
            with self._connections_lock:
                previous = self._connections.get(agent_id)
                self._connections[agent_id] = websocket
            if previous and previous is not websocket:
                try:
                    await previous.close(code=4000, reason="replaced by newer connection")
                except Exception:
                    pass
            await websocket.send(json.dumps(registration_response(agent_id), ensure_ascii=False))
            self._log_channel(agent_id, "channel_registered", "Channel client connected")
            await self._flush_pending(agent_id, websocket)
            async for raw in websocket:
                await self._handle_event(agent_id, websocket, self._decode(raw))
        except Exception as exc:
            if agent_id:
                self._log_channel(agent_id, "channel_disconnected", "{}".format(exc), level="warn")
            else:
                logger.debug("channel socket closed before registration: %s", exc)
        finally:
            if agent_id:
                with self._connections_lock:
                    if self._connections.get(agent_id) is websocket:
                        self._connections.pop(agent_id, None)

    @staticmethod
    def _decode(raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, str):
            raise ValueError("binary frames are not supported")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("event must be a JSON object")
        return value

    def _authenticate_registration(self, event: Dict[str, Any]) -> str:
        from channel_protocol import ensure_id
        agent_id = ensure_id(event.get("agent_id") or event.get("agentId"), "agent_id")
        config = self._config()
        tokens = config.get("tokens") or {}
        expected = resolve_token(tokens.get(agent_id)) if isinstance(tokens, dict) else None
        provided = str(event.get("token") or "")
        allow_local = bool(config.get("allow_unauthenticated_local", True)) and is_loopback_host(self._host)
        if expected:
            import hmac
            if not hmac.compare_digest(str(expected), provided):
                raise ValueError("invalid channel token")
        elif not allow_local:
            raise ValueError("channel token is required")
        return agent_id

    async def _handle_event(self, agent_id: str, websocket, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "ping":
            await websocket.send(json.dumps({"type": "pong"}))
            return
        if kind == "ack":
            message_id = str(event.get("message_id") or event.get("messageId") or "")
            if not message_id:
                raise ValueError("ack requires message_id")
            self._append_jsonl(self._path("acks", "{}.jsonl".format(agent_id)), {"id": message_id})
            return
        if kind == "message":
            message = self.publish(agent_id, event)
            await websocket.send(json.dumps({"type": "accepted", "id": message["id"], "trace_id": message["trace_id"]}, ensure_ascii=False))
            return
        await websocket.send(json.dumps(error_event("unknown_event", "unsupported event type")))

    def publish(self, sender: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Persist and route a channel message. Safe to call outside WebSocket code."""
        message = normalize_message(payload, sender)
        if self._seen(message["id"]):
            return message
        room = self._room(message["room_id"])
        members = list(room.get("agents") or [])
        if sender not in members:
            raise ValueError("sender is not a member of room {}".format(message["room_id"]))
        recipients = message["to"] or [agent for agent in members if agent != sender]
        invalid = [agent for agent in recipients if agent not in members]
        if invalid:
            raise ValueError("recipient is not a room member: {}".format(", ".join(invalid)))
        if not recipients:
            raise ValueError("message has no recipient")
        message["to"] = recipients
        self._remember(message["id"])
        append_room_message(
            self.shared_dir,
            message["room_id"],
            sender,
            message["text"],
            to_agent=recipients if len(recipients) > 1 else recipients[0],
            kind="channel",
            reply_to=message["reply_to"],
            correlation_id=message["trace_id"],
            meta={"transport": "channel", "channel_message_id": message["id"], "trace_id": message["trace_id"]},
        )
        self._log_room(message["room_id"], sender, "channel_message", "Channel message accepted", {"id": message["id"], "to": recipients})
        for recipient in recipients:
            self._enqueue(recipient, message)
            self._push_if_online(recipient, message)
        return message

    def _room(self, room_id: str) -> Dict[str, Any]:
        cfg = self._config_provider() or {}
        raw = (cfg.get("rooms") or {}).get(room_id)
        if not isinstance(raw, dict):
            raise ValueError("room not found")
        room = normalize_room({**raw, "id": room_id})
        ensure_room(self.shared_dir, room)
        return room

    def _log_room(self, room_id: str, agent_id: str, event: str, message: str, meta=None) -> None:
        try:
            append_room_log(self.shared_dir, room_id, event, message, agent_id=agent_id, meta=meta)
        except Exception:
            pass

    def _log_channel(self, agent_id: str, event: str, message: str, level="info") -> None:
        self._append_jsonl(self._path("events.jsonl"), {"agent_id": agent_id, "event": event, "message": message, "level": level})

    def _inbox_path(self, agent_id: str) -> Path:
        return self._path("inbox", "{}.jsonl".format(agent_id))

    def _ack_ids(self, agent_id: str) -> set:
        path = self._path("acks", "{}.jsonl".format(agent_id))
        return {str(row.get("id")) for row in self._read_jsonl(path) if row.get("id")}

    def _enqueue(self, agent_id: str, message: Dict[str, Any]) -> None:
        self._append_jsonl(self._inbox_path(agent_id), message)

    async def _flush_pending(self, agent_id: str, websocket) -> None:
        acknowledged = self._ack_ids(agent_id)
        for message in self._read_jsonl(self._inbox_path(agent_id)):
            if message.get("id") not in acknowledged:
                await websocket.send(json.dumps({"type": "message", "message": message}, ensure_ascii=False))

    def _push_if_online(self, agent_id: str, message: Dict[str, Any]) -> None:
        loop = self._loop
        with self._connections_lock:
            websocket = self._connections.get(agent_id)
        if loop and websocket and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                websocket.send(json.dumps({"type": "message", "message": message}, ensure_ascii=False)), loop
            )

    def _seen(self, message_id: str) -> bool:
        return message_id in {str(row.get("id")) for row in self._read_jsonl(self._path("dedup.jsonl"))}

    def _remember(self, message_id: str) -> None:
        self._append_jsonl(self._path("dedup.jsonl"), {"id": message_id})

    @staticmethod
    def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict):
                            rows.append(row)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return rows

    @staticmethod
    def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path.with_suffix(path.suffix + ".lock")):
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
