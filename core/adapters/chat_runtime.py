#!/usr/bin/env python3
"""Room-scoped chat adapters for OpenClaw and Hermes.

They reuse the existing native_http delivery transport. The important boundary
is architectural: rooms and JSONL remain authoritative while each runtime gets
one session mapping per room.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from adapters.base import BaseAdapter, register_adapter
from adapters.native_http import NativeHttpAdapter
from context_manager import build_context_bundle, get_or_create_session
from protocol import RESPONSE_CALLBACK, make_capability


class _ChatRuntimeAdapter(BaseAdapter):
    runtime = "chat"

    def normalize_config(self, agent_cfg):
        adapter = (agent_cfg or {}).get("adapter") or {}
        cfg = dict(adapter.get("config") or {})
        cfg.setdefault("url", "")
        cfg.setdefault("context_messages", 12)
        return cfg

    def capability(self, agent_cfg):
        cfg = self.normalize_config(agent_cfg)
        return make_capability(
            adapter_type=self.type,
            configured=bool(cfg.get("url")),
            automatic=bool(cfg.get("url")),
            wake_modes=["native_chat_hook"],
            response_modes=[RESPONSE_CALLBACK],
            supports_active_push=True,
            health="configured" if cfg.get("url") else "missing_config",
        )

    def wake(self, delivery_request):
        room_path = delivery_request.get("room_path", "")
        room_id = delivery_request.get("room_id", "")
        agent_id = delivery_request.get("agent_id", "")
        if not room_path:
            request = dict(delivery_request)
            request["adapter"] = {"config": {}}
            return NativeHttpAdapter().wake(request)

        shared_dir = Path(room_path).parent.parent
        cfg = dict((delivery_request.get("adapter") or {}).get("config") or {})
        session = get_or_create_session(shared_dir, room_id, agent_id, self.type)
        bundle = build_context_bundle(shared_dir, room_id, agent_id, cfg.get("context_messages", 12))

        request = dict(delivery_request)
        request["adapter"] = {
            "type": "native_http",
            "config": {**cfg, "inject_callback": False},
            "auth": (delivery_request.get("adapter") or {}).get("auth", {}),
            "template": (delivery_request.get("adapter") or {}).get("template", {
                "type": "agent_bridge.chat_delivery",
                "runtime": self.runtime,
                "room_id": "{{room_id}}",
                "agent_id": "{{agent_id}}",
                "message": "{{message}}",
                "from": "{{from}}",
                "turn_id": "{{turn_id}}",
                "correlation_id": "{{correlation_id}}",
                "callback_url": "{{callback_url}}",
            }),
        }
        request["message"] = json.dumps({
            "text": delivery_request.get("message", ""),
            "session": session,
            "context": bundle,
        }, ensure_ascii=False)
        return NativeHttpAdapter().wake(request)


@register_adapter
class OpenClawChannelAdapter(_ChatRuntimeAdapter):
    type = "openclaw_channel"
    runtime = "openclaw"


@register_adapter
class HermesChannelAdapter(_ChatRuntimeAdapter):
    type = "hermes_channel"
    runtime = "hermes"
