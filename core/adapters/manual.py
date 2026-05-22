#!/usr/bin/env python3
"""Manual adapter — no automatic delivery, requires human action.

This adapter always returns ok=False with a message explaining that
manual agents cannot be auto-triggered.
"""
import sys
from pathlib import Path

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from adapters.base import BaseAdapter, register_adapter      # noqa: E402
from protocol import (                                        # noqa: E402
    make_delivery_ticket, make_capability,
    RESPONSE_MANUAL, ADAPTER_MANUAL,
)


@register_adapter
class ManualAdapter(BaseAdapter):
    """Manual adapter — cannot be auto-triggered, requires human action."""

    type: str = "manual"

    # ── capability ───────────────────────────────────────

    def capability(self, agent_cfg: dict) -> dict:
        return make_capability(
            adapter_type=ADAPTER_MANUAL,
            configured=False,
            automatic=False,
            wake_modes=[],
            response_modes=["manual"],
            health="manual",
        )

    # ── wake ─────────────────────────────────────────────

    def wake(self, delivery_request: dict) -> dict:
        """Always returns ok=False — manual agents cannot be auto-triggered."""
        return make_delivery_ticket(
            ok=False,
            delivery_request=delivery_request,
            adapter_type=ADAPTER_MANUAL,
            response_mode=RESPONSE_MANUAL,
            error="manual agent cannot be auto-triggered",
        )

    # ── normalize_config ─────────────────────────────────

    def normalize_config(self, agent_cfg: dict) -> dict:
        """Return minimal config — manual adapter needs no configuration."""
        return {
            "type": ADAPTER_MANUAL,
        }

    # ── health_check override ────────────────────────────

    def health_check(self, agent_cfg: dict) -> dict:
        """Manual adapters are always in 'manual' health state."""
        return {
            "healthy": False,
            "health": "manual",
        }
