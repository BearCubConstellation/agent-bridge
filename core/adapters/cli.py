#!/usr/bin/env python3
"""CLI adapter — delivers messages by running a subprocess.

Runs a command with optional stdin, captures stdout as a sync response.
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from adapters.base import BaseAdapter, register_adapter      # noqa: E402
from adapters._legacy import render_template                  # noqa: E402
from protocol import (                                        # noqa: E402
    make_delivery_ticket, make_capability,
    RESPONSE_SYNC, ADAPTER_CLI,
)


def _command_args(command, context):
    """Render a command template and split into arg list."""
    rendered = render_template(command, context)
    if isinstance(rendered, list):
        return [str(part) for part in rendered]
    if isinstance(rendered, str):
        return shlex.split(rendered, posix=(os.name != "nt"))
    return []


@register_adapter
class CliAdapter(BaseAdapter):
    """Deliver messages by running a local subprocess."""

    type: str = "cli"

    # ── capability ───────────────────────────────────────

    def capability(self, agent_cfg: dict) -> dict:
        cfg = self.normalize_config(agent_cfg)
        command = cfg.get("command", "")
        configured = bool(command)
        return make_capability(
            adapter_type=ADAPTER_CLI,
            configured=configured,
            automatic=configured,
            wake_modes=["subprocess"],
            response_modes=["sync"],
            health="configured" if configured else "missing_config",
        )

    # ── wake ─────────────────────────────────────────────

    def wake(self, delivery_request: dict) -> dict:
        """Run subprocess with command and optional stdin, capture stdout."""
        agent_id = delivery_request.get("agent_id", "")
        message = delivery_request.get("message", "")
        turn_id = delivery_request.get("turn_id", "")
        correlation_id = delivery_request.get("correlation_id", "")
        callback_url = delivery_request.get("callback_url", "")
        room_id = delivery_request.get("room_id", "")
        from_agents = delivery_request.get("from", "")

        adapter_cfg = delivery_request.get("adapter", {})
        cfg = adapter_cfg.get("config", {})

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

        # Resolve command
        args = _command_args(cfg.get("command", ""), context)
        if not args:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_CLI,
                response_mode=RESPONSE_SYNC,
                error="cli adapter: command is empty",
            )

        # Resolve optional stdin
        stdin_template = cfg.get("stdin", None)
        stdin_text = None
        if stdin_template is not None:
            stdin_text = render_template(stdin_template, context)

        timeout = int(cfg.get("timeout", 60))

        try:
            completed = subprocess.run(
                args,
                input=stdin_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_CLI,
                response_mode=RESPONSE_SYNC,
                error=f"cli adapter: timed out after {timeout}s",
            )
        except Exception as exc:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_CLI,
                response_mode=RESPONSE_SYNC,
                error=f"cli adapter: {exc}",
            )

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()

        if completed.returncode == 0:
            return make_delivery_ticket(
                ok=True,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_CLI,
                response_mode=RESPONSE_SYNC,
                detail=f"exit=0",
                sync_response=stdout,
            )

        detail = stderr or stdout or "no output"
        return make_delivery_ticket(
            ok=False,
            delivery_request=delivery_request,
            adapter_type=ADAPTER_CLI,
            response_mode=RESPONSE_SYNC,
            error=f"exit={completed.returncode}: {detail[:200]}",
            sync_response=stdout,
        )

    # ── normalize_config ─────────────────────────────────

    def normalize_config(self, agent_cfg: dict) -> dict:
        """Extract command/stdin from adapter config."""
        agent_cfg = agent_cfg or {}
        adapter = agent_cfg.get("adapter")
        if adapter and isinstance(adapter, dict):
            cfg = dict(adapter.get("config", {}))
            cfg["type"] = adapter.get("type", ADAPTER_CLI)
            cfg["stdin"] = adapter.get("stdin") or adapter.get("template", {}).get("stdin")
            return cfg

        # Legacy: no wakeup equivalent for cli
        return {"type": ADAPTER_CLI}
