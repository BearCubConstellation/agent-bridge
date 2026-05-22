#!/usr/bin/env python3
"""Agent Bridge Adapter Layer — v2.

Re-exports from the legacy ``adapters/_legacy.py`` for backward compatibility
while providing the new adapter subpackage structure.

New code should import from ``adapters`` directly:
    from adapters import deliver_to_adapter, normalize_adapter, adapter_capability
    from adapters import deliver_via_registry  # new v2 registry path
"""
import sys as _sys
from pathlib import Path

# Ensure sibling modules are importable
_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in _sys.path:
    _sys.path.insert(0, _parent)

_pkg = str(Path(__file__).resolve().parent)
if _pkg not in _sys.path:
    _sys.path.insert(0, _pkg)

from adapters._legacy import (           # noqa: E402, F401
    ADAPTER_TYPES,
    render_template,
    wakeup_to_adapter,
    adapter_to_wakeup,
    normalize_adapter,
    adapter_capability,
    deliver_to_adapter,
    _command_args,
    _deliver_cli,
    _deliver_file_inbox,
)

# ── Import new adapter modules — triggers @register_adapter ──
# Import order: base first, then all adapter implementations.
from adapters.base import (              # noqa: E402
    BaseAdapter, register_adapter, get_adapter_class, list_adapter_types,
)
from adapters import native_http         # noqa: E402, F401
from adapters import openclaw_sessions   # noqa: E402, F401
from adapters import cli                 # noqa: E402, F401
from adapters import file_mailbox        # noqa: E402, F401
from adapters import manual              # noqa: E402, F401
from adapters import mcp_tool            # noqa: E402, F401

from protocol import (                   # noqa: E402
    make_delivery_request, make_delivery_ticket,
    RESPONSE_SYNC, RESPONSE_CALLBACK, RESPONSE_FILE_OUTBOX,
    RESPONSE_MCP_TOOL, RESPONSE_MANUAL, RESPONSE_NONE,
)


def deliver_via_registry(agent_cfg, message_text, from_agents, context=None):
    """Deliver a message via the new adapter registry.

    Resolves adapter type from agent config, looks up the registered
    adapter class, instantiates it, builds a DeliveryRequest, and calls
    ``adapter.wake()`` → DeliveryTicket.

    Falls back to ``deliver_to_adapter()`` if the adapter type is not
    in the registry (backward compat with old adapter types like
    ``stdio_shim``).

    Returns a DeliveryTicket-like dict with at least:
        ok, detail, sync_response, response_mode, error, adapter_type
    """
    adapter = normalize_adapter(agent_cfg)
    adapter_type = adapter.get("type", "manual")

    # ── Try new registry path ──
    adapter_cls = get_adapter_class(adapter_type)
    if adapter_cls is None:
        # Fall back to legacy deliver_to_adapter
        ok, detail, response_body = deliver_to_adapter(
            agent_cfg, message_text, from_agents, context,
        )
        # Infer response_mode from legacy behavior:
        # native_http → callback, cli/stdio_shim → sync, file_inbox → file_outbox
        if adapter_type == "native_http":
            mode = RESPONSE_CALLBACK
        elif adapter_type in ("cli", "stdio_shim"):
            mode = RESPONSE_SYNC
        elif adapter_type == "file_inbox":
            mode = RESPONSE_FILE_OUTBOX
        else:
            mode = RESPONSE_MANUAL
        return {
            "ok": ok,
            "detail": detail if ok else "",
            "sync_response": response_body,
            "raw_response": "",
            "response_mode": mode,
            "error": "" if ok else detail,
            "adapter_type": adapter_type,
        }

    # ── Build delivery context ──
    ctx = dict(context or {})
    ctx.setdefault("message", message_text)
    ctx.setdefault("from", from_agents)

    delivery_req = {
        "room_id": ctx.get("room", ""),
        "agent_id": ctx.get("to", ""),
        "turn_id": ctx.get("turn_id", ""),
        "correlation_id": ctx.get("correlation_id", ""),
        "message": message_text,
        "from": from_agents,
        "callback_url": ctx.get("callback_url", ""),
        "room_path": ctx.get("room_path", ""),
        "active_file": ctx.get("active_file", ""),
        "input_messages": [],
        "adapter": adapter,
    }

    adapter_instance = adapter_cls()
    ticket = adapter_instance.wake(delivery_req)
    return ticket


__all__ = [
    "ADAPTER_TYPES",
    "render_template",
    "wakeup_to_adapter",
    "adapter_to_wakeup",
    "normalize_adapter",
    "adapter_capability",
    "deliver_to_adapter",
    "deliver_via_registry",
    "BaseAdapter",
    "register_adapter",
    "get_adapter_class",
    "list_adapter_types",
    "make_delivery_request",
    "make_delivery_ticket",
]
