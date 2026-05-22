#!/usr/bin/env python3
"""MCP Tool adapter — renders instructions for external MCP tool invocation.

This adapter does NOT actually call MCP. It renders an instructions_template
and returns a DeliveryTicket with response_mode=mcp_tool, indicating that
an external system should execute the MCP tool call.
"""
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
    RESPONSE_MCP_TOOL, ADAPTER_MCP_TOOL,
)


@register_adapter
class McpToolAdapter(BaseAdapter):
    """Render MCP tool instructions for external execution.

    Does not perform the actual MCP call — that is triggered externally.
    """

    type: str = "mcp_tool"

    # ── capability ───────────────────────────────────────

    def capability(self, agent_cfg: dict) -> dict:
        cfg = self.normalize_config(agent_cfg)
        instructions = cfg.get("instructions_template", "")
        configured = bool(instructions)
        return make_capability(
            adapter_type=ADAPTER_MCP_TOOL,
            configured=configured,
            automatic=False,
            wake_modes=["external"],
            response_modes=["mcp_tool"],
            health="configured" if configured else "missing_config",
        )

    # ── wake ─────────────────────────────────────────────

    def wake(self, delivery_request: dict) -> dict:
        """Render instructions_template for the agent, return mcp_tool ticket.

        Does NOT actually call MCP — the caller is responsible for invoking
        the MCP tool based on the rendered instructions.
        """
        agent_id = delivery_request.get("agent_id", "")
        message = delivery_request.get("message", "")
        turn_id = delivery_request.get("turn_id", "")
        correlation_id = delivery_request.get("correlation_id", "")
        callback_url = delivery_request.get("callback_url", "")
        room_id = delivery_request.get("room_id", "")
        from_agents = delivery_request.get("from", "")

        adapter_cfg = delivery_request.get("adapter", {})
        instructions_template = adapter_cfg.get("instructions_template", "")

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

        # Render instructions
        if instructions_template:
            rendered = render_template(instructions_template, context)
            # Ensure we have a string for sync_response / detail
            if isinstance(rendered, str):
                instructions = rendered
            else:
                import json
                instructions = json.dumps(rendered, ensure_ascii=False, indent=2)
        else:
            instructions = ""

        return make_delivery_ticket(
            ok=True,
            delivery_request=delivery_request,
            adapter_type=ADAPTER_MCP_TOOL,
            response_mode=RESPONSE_MCP_TOOL,
            detail="mcp_tool: instructions rendered, awaiting external invocation",
            sync_response=instructions,
        )

    # ── normalize_config ─────────────────────────────────

    def normalize_config(self, agent_cfg: dict) -> dict:
        """Extract instructions_template from adapter config."""
        agent_cfg = agent_cfg or {}
        adapter = agent_cfg.get("adapter")
        if adapter and isinstance(adapter, dict):
            return {
                "type": adapter.get("type", ADAPTER_MCP_TOOL),
                "instructions_template": adapter.get("instructions_template", ""),
                "config": dict(adapter.get("config", {})),
            }
        return {"type": ADAPTER_MCP_TOOL, "instructions_template": ""}
