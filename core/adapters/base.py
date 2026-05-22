#!/usr/bin/env python3
"""Adapter Base — protocol definition for Agent Bridge adapters.

Every adapter must implement the BaseAdapter interface.
"""
import sys
from pathlib import Path
from abc import ABC, abstractmethod

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from protocol import (                                    # noqa: E402
    make_delivery_request, make_delivery_ticket, make_capability,
)


class BaseAdapter(ABC):
    """Abstract base class for all Agent Bridge adapters.

    Subclasses must implement:
      - ``capability(agent_cfg)`` → dict
      - ``wake(delivery_request)`` → DeliveryTicket dict
      - ``normalize_config(agent_cfg)`` → dict
    """

    type: str = "base"

    @abstractmethod
    def capability(self, agent_cfg: dict) -> dict:
        """Return capability dict for this adapter + agent config."""
        ...

    @abstractmethod
    def wake(self, delivery_request: dict) -> dict:
        """Deliver a message to the agent. Returns a DeliveryTicket dict."""
        ...

    @abstractmethod
    def normalize_config(self, agent_cfg: dict) -> dict:
        """Normalize adapter config from agent config entry."""
        ...

    def health_check(self, agent_cfg: dict) -> dict:
        """Optional health check. Default: check capability.configured."""
        cap = self.capability(agent_cfg)
        return {
            "healthy": cap.get("configured", False),
            "health": cap.get("health", "unknown"),
        }


# ── Adapter Registry ────────────────────────────────────

_REGISTRY = {}


def register_adapter(adapter_cls):
    """Register an adapter class by its type string."""
    adapter_type = getattr(adapter_cls, "type", None)
    if adapter_type:
        _REGISTRY[adapter_type] = adapter_cls
    return adapter_cls


def get_adapter_class(adapter_type):
    """Get adapter class by type string. Returns None if not found."""
    return _REGISTRY.get(adapter_type)


def list_adapter_types():
    """List all registered adapter type strings."""
    return list(_REGISTRY.keys())
