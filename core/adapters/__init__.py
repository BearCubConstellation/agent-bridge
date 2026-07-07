#!/usr/bin/env python3
"""Agent Bridge Adapter Layer — v2.

The Room Runtime owns messages and scheduling. Adapters only translate a
DeliveryRequest into the transport required by one agent runtime.
"""
import sys as _sys
from pathlib import Path

_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in _sys.path:
    _sys.path.insert(0, _parent)

_pkg = str(Path(__file__).resolve().parent)
if _pkg not in _sys.path:
    _sys.path.insert(0, _pkg)

from adapters._legacy import (  # noqa: E402, F401
    ADAPTER_TYPES, render_template, wakeup_to_adapter, adapter_to_wakeup,
    normalize_adapter, adapter_capability, deliver_to_adapter, _command_args,
    _deliver_cli, _deliver_file_inbox,
)
from adapters.base import BaseAdapter, register_adapter, get_adapter_class, list_adapter_types  # noqa: E402
from adapters import native_http  # noqa: E402, F401
from adapters import openclaw_sessions  # noqa: E402, F401
from adapters import cli  # noqa: E402, F401
from adapters import file_mailbox  # noqa: E402, F401
from adapters import manual  # noqa: E402, F401
from adapters import mcp_tool  # noqa: E402, F401
from adapters import chat_runtime  # noqa: E402, F401
from protocol import (  # noqa: E402
    make_delivery_request, make_delivery_ticket,
    RESPONSE_SYNC, RESPONSE_CALLBACK, RESPONSE_FILE_OUTBOX,
    RESPONSE_MCP_TOOL, RESPONSE_MANUAL, RESPONSE_NONE,
)


def deliver_via_registry(agent_cfg, message_text, from_agents, context=None):
    """Deliver via one registered adapter without changing Room ownership."""
    adapter = normalize_adapter(agent_cfg)
    adapter_type = adapter.get("type", "manual")
    adapter_cls = get_adapter_class(adapter_type)
    if adapter_cls is None:
        ok, detail, response_body = deliver_to_adapter(agent_cfg, message_text, from_agents, context)
        if adapter_type == "native_http":
            mode = RESPONSE_CALLBACK
        elif adapter_type in ("cli", "stdio_shim"):
            mode = RESPONSE_SYNC
        elif adapter_type == "file_inbox":
            mode = RESPONSE_FILE_OUTBOX
        else:
            mode = RESPONSE_MANUAL
        return {
            "ok": ok, "detail": detail if ok else "", "sync_response": response_body,
            "raw_response": "", "response_mode": mode,
            "error": "" if ok else detail, "adapter_type": adapter_type,
        }

    ctx = dict(context or {})
    ctx.setdefault("message", message_text)
    ctx.setdefault("from", from_agents)
    delivery_req = {
        "room_id": ctx.get("room", ""),
        "agent_id": ctx.get("to", ""),
        "turn_id": ctx.get("turn_id", ""),
        "delivery_id": ctx.get("delivery_id", ""),
        "correlation_id": ctx.get("correlation_id", ""),
        "message": message_text,
        "from": from_agents,
        "callback_url": ctx.get("callback_url", ""),
        "room_path": ctx.get("room_path", ""),
        "active_file": ctx.get("active_file", ""),
        "input_messages": ctx.get("input_messages", []),
        "adapter": adapter,
    }
    return adapter_cls().wake(delivery_req)


__all__ = [
    "ADAPTER_TYPES", "render_template", "wakeup_to_adapter", "adapter_to_wakeup",
    "normalize_adapter", "adapter_capability", "deliver_to_adapter", "deliver_via_registry",
    "BaseAdapter", "register_adapter", "get_adapter_class", "list_adapter_types",
    "make_delivery_request", "make_delivery_ticket",
]
