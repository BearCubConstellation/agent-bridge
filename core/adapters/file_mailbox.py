#!/usr/bin/env python3
"""File Mailbox adapter — delivers messages by writing to inbox files.

Writes a delivery request as JSON to the configured inbox path.
Thread-safe via file_lock from lock.py.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from adapters.base import BaseAdapter, register_adapter      # noqa: E402
from adapters._legacy import render_template                  # noqa: E402
from lock import file_lock                                    # noqa: E402
from protocol import (                                        # noqa: E402
    make_delivery_ticket, make_capability,
    RESPONSE_FILE_OUTBOX, ADAPTER_FILE_MAILBOX,
)


def _resolve_path(p):
    """Expand env vars and user home in a path string."""
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


@register_adapter
class FileMailboxAdapter(BaseAdapter):
    """Deliver messages by writing to an inbox file."""

    type: str = "file_mailbox"

    # ── capability ───────────────────────────────────────

    def capability(self, agent_cfg: dict) -> dict:
        cfg = self.normalize_config(agent_cfg)
        inbox_path = cfg.get("inbox_path", "")
        configured = bool(inbox_path)
        return make_capability(
            adapter_type=ADAPTER_FILE_MAILBOX,
            configured=configured,
            automatic=configured,
            wake_modes=["file_write"],
            response_modes=["file_outbox"],
            health="configured" if configured else "missing_config",
        )

    # ── wake ─────────────────────────────────────────────

    def wake(self, delivery_request: dict) -> dict:
        """Write delivery request to inbox path, return file_outbox ticket."""
        agent_id = delivery_request.get("agent_id", "")
        message = delivery_request.get("message", "")
        turn_id = delivery_request.get("turn_id", "")
        correlation_id = delivery_request.get("correlation_id", "")
        callback_url = delivery_request.get("callback_url", "")
        room_id = delivery_request.get("room_id", "")
        from_agents = delivery_request.get("from", "")

        adapter_cfg = delivery_request.get("adapter", {})
        cfg = adapter_cfg.get("config", {})
        template = adapter_cfg.get("template", {})

        inbox_path = cfg.get("inbox_path", "")
        if not inbox_path:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_FILE_MAILBOX,
                response_mode=RESPONSE_FILE_OUTBOX,
                error="file_mailbox adapter: inbox_path is empty",
            )

        inbox = _resolve_path(inbox_path)
        outbox_path = cfg.get("outbox_path", "")

        # Build template context
        context = {
            "message": message,
            "from": from_agents,
            "agent_id": agent_id,
            "room_id": room_id,
            "turn_id": turn_id,
            "correlation_id": correlation_id,
            "callback_url": callback_url,
        }

        # Build the delivery record
        record = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "from": from_agents,
            "room": room_id,
            "msg": message,
            "turn_id": turn_id,
            "correlation_id": correlation_id,
            "callback_url": callback_url,
        }

        # Merge rendered template if present
        if template:
            rendered = render_template(template, context)
            if isinstance(rendered, dict):
                record.update(rendered)

        # Ensure inbox directory exists
        inbox.parent.mkdir(parents=True, exist_ok=True)

        # Write with file lock for thread safety
        lock_path = inbox.parent / ".agent-bridge-inbox.lock"
        try:
            with file_lock(str(lock_path)):
                with open(inbox, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_FILE_MAILBOX,
                response_mode=RESPONSE_FILE_OUTBOX,
                error=f"file_mailbox adapter: write failed: {exc}",
            )

        return make_delivery_ticket(
            ok=True,
            delivery_request=delivery_request,
            adapter_type=ADAPTER_FILE_MAILBOX,
            response_mode=RESPONSE_FILE_OUTBOX,
            detail=f"file={inbox}",
        )

    # ── normalize_config ─────────────────────────────────

    def normalize_config(self, agent_cfg: dict) -> dict:
        """Extract inbox_path/outbox_path from adapter config."""
        agent_cfg = agent_cfg or {}
        adapter = agent_cfg.get("adapter")
        if adapter and isinstance(adapter, dict):
            cfg = dict(adapter.get("config", {}))
            cfg["type"] = adapter.get("type", ADAPTER_FILE_MAILBOX)
            cfg["template"] = adapter.get("template", {})
            return cfg

        # Legacy wakeup: file_inbox used "path" for inbox
        wakeup = agent_cfg.get("wakeup") or {}
        if not wakeup:
            return {"type": "manual"}

        return {
            "type": ADAPTER_FILE_MAILBOX,
            "inbox_path": wakeup.get("path", wakeup.get("inbox_path", "")),
            "outbox_path": wakeup.get("outbox_path", ""),
        }
