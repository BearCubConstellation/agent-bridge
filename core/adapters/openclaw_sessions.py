#!/usr/bin/env python3
"""OpenClaw Sessions adapter — delivers via OpenClaw sessions_send tool format.

Specialized HTTP adapter that includes turn_id, correlation_id, and
callback_url in the message payload for session-based communication.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from adapters.base import BaseAdapter, register_adapter      # noqa: E402
from adapters._legacy import render_template                  # noqa: E402
from adapters.native_http import _resolve_token               # noqa: E402
from protocol import (                                        # noqa: E402
    make_delivery_ticket, make_capability,
    RESPONSE_CALLBACK, RESPONSE_PULL_SESSION,
    ADAPTER_OPENCLAW_SESSIONS,
)


@register_adapter
class OpenClawSessionsAdapter(BaseAdapter):
    """Deliver messages via OpenClaw sessions_send tool format."""

    type: str = "openclaw_sessions"

    # ── capability ───────────────────────────────────────

    def capability(self, agent_cfg: dict) -> dict:
        cfg = self.normalize_config(agent_cfg)
        url = cfg.get("url", "")
        sessions_key = cfg.get("sessions_key", "") or cfg.get("sessionsKey", "")
        configured = bool(url and sessions_key)
        return make_capability(
            adapter_type=ADAPTER_OPENCLAW_SESSIONS,
            configured=configured,
            automatic=True,
            wake_modes=["http"],
            response_modes=["callback", "pull_session"],
            requires_callback_url=True,
            supports_active_push=True,
            health="configured" if configured else "missing_config",
        )

    # ── wake ─────────────────────────────────────────────

    def wake(self, delivery_request: dict) -> dict:
        """Build OpenClaw sessions_send payload and POST it."""
        agent_id = delivery_request.get("agent_id", "")
        message = delivery_request.get("message", "")
        turn_id = delivery_request.get("turn_id", "")
        correlation_id = delivery_request.get("correlation_id", "")
        callback_url = delivery_request.get("callback_url", "")
        room_id = delivery_request.get("room_id", "")
        from_agents = delivery_request.get("from", "")

        adapter_cfg = delivery_request.get("adapter", {})
        cfg = adapter_cfg.get("config", {})
        sessions_key = cfg.get("sessions_key", "") or cfg.get("sessionsKey", "")
        url = cfg.get("url", "")
        method = (cfg.get("method", "POST")).upper()
        headers = dict(cfg.get("headers", {}))
        timeout = int(cfg.get("timeout", 60))
        auth = adapter_cfg.get("auth", {})

        # Build message template with session-specific fields
        message_template = adapter_cfg.get("message_template", {})
        context = {
            "message": message,
            "from": from_agents,
            "agent_id": agent_id,
            "room_id": room_id,
            "turn_id": turn_id,
            "correlation_id": correlation_id,
            "callback_url": callback_url,
            "sessions_key": sessions_key,
        }

        # Build sessions_send tool format payload
        rendered = render_template(message_template, context) if message_template else {}

        # Construct the OpenClaw tool call payload
        payload_body = {
            "tool": "sessions_send",
            "sessionsKey": sessions_key,
            "turn_id": turn_id,
            "correlation_id": correlation_id,
            "callback_url": callback_url,
            "message": message,
            "from": from_agents,
            "room_id": room_id,
        }
        # Merge rendered template fields
        if isinstance(rendered, dict):
            payload_body.update(rendered)

        if not url:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                response_mode=RESPONSE_CALLBACK,
                error="openclaw_sessions adapter: url is empty",
            )

        # Prepare headers
        headers.setdefault("Content-Type", "application/json")

        # Auth: bearer token
        if auth.get("type") == "bearer":
            token = _resolve_token(auth)
            if token:
                headers["Authorization"] = f"Bearer {token}"

        # Build request
        payload = json.dumps(payload_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(65536).decode("utf-8", errors="replace")
                return make_delivery_ticket(
                    ok=True,
                    delivery_request=delivery_request,
                    adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                    response_mode=RESPONSE_CALLBACK,
                    detail=f"status={resp.status}",
                    raw_response=raw,
                )
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")[:200]
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                response_mode=RESPONSE_CALLBACK,
                error=f"HTTP {e.code}: {err_body}",
            )
        except Exception as e:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                response_mode=RESPONSE_CALLBACK,
                error=str(e),
            )

    # ── normalize_config ─────────────────────────────────

    def normalize_config(self, agent_cfg: dict) -> dict:
        """Handle new-style adapter config and legacy wakeup with tool/sessionsKey."""
        agent_cfg = agent_cfg or {}
        adapter = agent_cfg.get("adapter")
        if adapter and isinstance(adapter, dict):
            cfg = dict(adapter.get("config", {}))
            cfg["type"] = adapter.get("type", ADAPTER_OPENCLAW_SESSIONS)
            cfg["message_template"] = adapter.get("message_template", {})
            cfg["auth"] = adapter.get("auth", {})
            return cfg

        # Legacy wakeup config with tool/sessionsKey
        wakeup = agent_cfg.get("wakeup") or {}
        if not wakeup:
            return {"type": "manual"}

        return {
            "type": ADAPTER_OPENCLAW_SESSIONS,
            "url": wakeup.get("url", ""),
            "method": wakeup.get("method", "POST"),
            "headers": wakeup.get("headers", {}),
            "sessions_key": wakeup.get("sessionsKey", wakeup.get("sessions_key", "")),
            "tool": wakeup.get("tool", "sessions_send"),
            "message_template": wakeup.get("message_template", {}),
            "auth": wakeup.get("auth", {}),
            "retry": wakeup.get("retry"),
        }
