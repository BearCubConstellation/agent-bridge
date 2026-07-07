#!/usr/bin/env python3
"""OpenClaw sessions adapter with cached tool-name discovery."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from adapters._legacy import render_template
from adapters.base import BaseAdapter, register_adapter
from adapters.native_http import _resolve_token
from protocol import ADAPTER_OPENCLAW_SESSIONS, RESPONSE_CALLBACK, make_capability, make_delivery_ticket

_CANDIDATE_TOOLS = ["sessions_send", "send_message", "session_send", "chat", "message_send"]
_PROBE_PATHS = ["/tools", "/tools/list", "/api/tools", "/mcp/tools"]
_TOOL_CACHE = {}


def _extract_base_url(url):
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return "{}://{}".format(parsed.scheme, parsed.netloc)


def _headers(auth_cfg):
    headers = {"Content-Type": "application/json"}
    if (auth_cfg or {}).get("type") == "bearer":
        token = _resolve_token(auth_cfg)
        if token:
            headers["Authorization"] = "Bearer {}".format(token)
    return headers


def probe_openclaw_tool(url, auth_cfg=None, timeout=5):
    """Discover the first compatible OpenClaw message tool, if exposed."""
    base = _extract_base_url(url)
    for path in _PROBE_PATHS:
        try:
            request = urllib.request.Request(base + path, headers=_headers(auth_cfg), method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read(65536).decode("utf-8", errors="replace"))
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("tools") or data.get("data") or data.get("result") or []
                if isinstance(items, dict):
                    items = items.get("tools", [])
            else:
                items = []
            names = set()
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("tool") or item.get("id")
                else:
                    name = item
                if name:
                    names.add(str(name).lower())
            for candidate in _CANDIDATE_TOOLS:
                if candidate in names:
                    return candidate
            for name in sorted(names):
                if any(token in name for token in ("send", "message", "session", "chat")):
                    return name
        except Exception:
            continue
    return None


@register_adapter
class OpenClawSessionsAdapter(BaseAdapter):
    type = ADAPTER_OPENCLAW_SESSIONS

    def capability(self, agent_cfg):
        cfg = self.normalize_config(agent_cfg)
        configured = bool(cfg.get("url") and (cfg.get("sessions_key") or cfg.get("sessionsKey")))
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

    def wake(self, delivery_request):
        agent_id = delivery_request.get("agent_id", "")
        adapter_cfg = delivery_request.get("adapter", {}) or {}
        cfg = adapter_cfg.get("config", {}) or {}
        url = cfg.get("url", "")
        sessions_key = cfg.get("sessions_key", "") or cfg.get("sessionsKey", "")
        if not url:
            return make_delivery_ticket(False, delivery_request, ADAPTER_OPENCLAW_SESSIONS, RESPONSE_CALLBACK,
                                        error="openclaw_sessions adapter: url is empty")

        auth = adapter_cfg.get("auth", {}) or {}
        headers = dict(cfg.get("headers", {}) or {})
        headers.setdefault("Content-Type", "application/json")
        if auth.get("type") == "bearer":
            token = _resolve_token(auth)
            if token:
                headers["Authorization"] = "Bearer {}".format(token)
        timeout = int(cfg.get("timeout", 60))
        method = str(cfg.get("method", "POST")).upper()
        context = {
            "message": delivery_request.get("message", ""),
            "from": delivery_request.get("from", ""),
            "agent_id": agent_id,
            "room_id": delivery_request.get("room_id", ""),
            "turn_id": delivery_request.get("turn_id", ""),
            "correlation_id": delivery_request.get("correlation_id", ""),
            "callback_url": delivery_request.get("callback_url", ""),
            "sessions_key": sessions_key,
        }
        rendered = render_template(adapter_cfg.get("message_template", {}), context) if adapter_cfg.get("message_template") else {}
        callback_instruction = ""
        if context["callback_url"] and context["turn_id"]:
            callback_instruction = (
                "\n\n[Agent Bridge 回写指令]\nroom_id={room_id}\nturn_id={turn_id}\n"
                "correlation_id={correlation_id}\ncallback_url={callback_url}\n"
                "最终回复后必须调用 agent_bridge.reply_turn，或以 Authorization: Bearer token POST 到 callback_url。"
            ).format(**context)
        args = {
            "sessionKey": sessions_key,
            "turn_id": context["turn_id"],
            "correlation_id": context["correlation_id"],
            "callback_url": context["callback_url"],
            "message": context["message"] + callback_instruction,
            "from": context["from"],
            "room_id": context["room_id"],
        }
        if isinstance(rendered, dict):
            args.update(rendered)

        configured_tool = cfg.get("tool", "") or adapter_cfg.get("tool", "")
        tool_name = configured_tool or _TOOL_CACHE.get(url) or "sessions_send"

        def invoke(name):
            body = json.dumps({"tool": name, "args": args}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            return urllib.request.urlopen(request, timeout=timeout)

        def success(response, tool, suffix=""):
            raw = response.read(65536).decode("utf-8", errors="replace")
            _TOOL_CACHE[url] = tool
            # Keep the successful discovery in the shared in-memory config too.
            if not configured_tool:
                cfg["tool"] = tool
            return make_delivery_ticket(True, delivery_request, ADAPTER_OPENCLAW_SESSIONS, RESPONSE_CALLBACK,
                                        detail="status={} tool={}{}".format(response.status, tool, suffix), raw_response=raw)

        try:
            with invoke(tool_name) as response:
                return success(response, tool_name)
        except urllib.error.HTTPError as error:
            error_body = error.read().decode(errors="replace")[:300]
            unavailable = error.code == 404 and "tool not available" in error_body.lower()
            if not unavailable:
                return make_delivery_ticket(False, delivery_request, ADAPTER_OPENCLAW_SESSIONS, RESPONSE_CALLBACK,
                                            error="HTTP {}: {}".format(error.code, error_body))
            _TOOL_CACHE.pop(url, None)
            detected = probe_openclaw_tool(url, auth, timeout=min(timeout, 5))
            if not detected or detected == tool_name:
                return make_delivery_ticket(False, delivery_request, ADAPTER_OPENCLAW_SESSIONS, RESPONSE_CALLBACK,
                                            error="HTTP 404: OpenClaw tool '{}' is unavailable and discovery found no replacement".format(tool_name))
            try:
                with invoke(detected) as response:
                    return success(response, detected, " (auto-detected, was {})".format(tool_name))
            except urllib.error.HTTPError as retry_error:
                retry_body = retry_error.read().decode(errors="replace")[:200]
                return make_delivery_ticket(False, delivery_request, ADAPTER_OPENCLAW_SESSIONS, RESPONSE_CALLBACK,
                                            error="HTTP {} after discovering '{}': {}".format(retry_error.code, detected, retry_body))
            except Exception as retry_error:
                return make_delivery_ticket(False, delivery_request, ADAPTER_OPENCLAW_SESSIONS, RESPONSE_CALLBACK,
                                            error="auto-detected tool '{}' failed: {}".format(detected, retry_error))
        except Exception as error:
            return make_delivery_ticket(False, delivery_request, ADAPTER_OPENCLAW_SESSIONS, RESPONSE_CALLBACK, error=str(error))

    def normalize_config(self, agent_cfg):
        agent_cfg = agent_cfg or {}
        adapter = agent_cfg.get("adapter")
        if isinstance(adapter, dict):
            cfg = dict(adapter.get("config", {}) or {})
            cfg["type"] = adapter.get("type", ADAPTER_OPENCLAW_SESSIONS)
            cfg["message_template"] = adapter.get("message_template", {})
            cfg["auth"] = adapter.get("auth", {})
            return cfg
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
