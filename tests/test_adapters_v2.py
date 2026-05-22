#!/usr/bin/env python3
"""Tests for core/adapters/__init__.py — adapter registry and deliver_via_registry."""
import sys
import unittest
from pathlib import Path
from unittest import mock

# Must set up paths before importing adapters
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from adapters import (
    BaseAdapter,
    get_adapter_class,
    list_adapter_types,
    register_adapter,
    deliver_via_registry,
)


class TestAdapterRegistry(unittest.TestCase):
    """Test all 6 adapter types are registered."""

    def test_all_six_types_registered(self):
        types = list_adapter_types()
        expected = {
            "native_http",
            "openclaw_sessions",
            "cli",
            "file_mailbox",
            "mcp_tool",
            "manual",
        }
        found = set(types)
        self.assertEqual(found, expected,
                         f"Expected all 6 adapter types, got: {found}")

    def test_get_adapter_class_returns_correct_classes(self):
        cli_cls = get_adapter_class("cli")
        self.assertIsNotNone(cli_cls)
        self.assertEqual(cli_cls.type, "cli")
        self.assertTrue(issubclass(cli_cls, BaseAdapter))

        manual_cls = get_adapter_class("manual")
        self.assertIsNotNone(manual_cls)
        self.assertEqual(manual_cls.type, "manual")
        self.assertTrue(issubclass(manual_cls, BaseAdapter))

    def test_get_adapter_class_for_all_types(self):
        for atype in list_adapter_types():
            cls = get_adapter_class(atype)
            self.assertIsNotNone(cls, f"get_adapter_class({atype!r}) returned None")
            self.assertEqual(cls.type, atype)

    def test_get_adapter_class_unknown_returns_none(self):
        self.assertIsNone(get_adapter_class("nonexistent_adapter_type"))

    def test_register_adapter_decorator(self):
        """Test that register_adapter decorator works."""

        @register_adapter
        class TestAdapter(BaseAdapter):
            type = "test_temp"

            def capability(self, agent_cfg):
                return {}

            def wake(self, delivery_request):
                return {"ok": True}

            def normalize_config(self, agent_cfg):
                return {}

        self.assertEqual(get_adapter_class("test_temp"), TestAdapter)

    def test_list_adapter_types_returns_list_of_strings(self):
        types = list_adapter_types()
        self.assertIsInstance(types, list)
        for t in types:
            self.assertIsInstance(t, str)


class TestDeliverViaRegistry(unittest.TestCase):
    """Test deliver_via_registry calls adapter.wake() and returns ticket."""

    def test_deliver_via_registry_calls_wake(self):
        """Test that deliver_via_registry calls adapter.wake() and returns its result."""
        agent_cfg = {
            "adapter": {
                "type": "cli",
                "command": "echo hello",
                "stdin": "{{ message }}",
            }
        }
        ticket = deliver_via_registry(
            agent_cfg,
            message_text="test message",
            from_agents=["alice"],
            context={
                "room": "test-room",
                "to": "cli-agent",
                "turn_id": "turn_1",
                "correlation_id": "corr_1",
                "callback_url": "http://localhost/cb",
                "room_path": "/tmp/rooms/test",
                "active_file": "/tmp/active.jsonl",
            },
        )

        # Should return a DeliveryTicket-like dict
        self.assertIsInstance(ticket, dict)
        self.assertIn("ok", ticket)
        self.assertIn("adapter_type", ticket)
        self.assertIn("detail", ticket)

    def test_deliver_via_registry_legacy_fallback(self):
        """When get_adapter_class returns None, falls back to deliver_to_adapter."""
        agent_cfg = {
            "adapter": {
                "type": "cli",
            }
        }

        # Mock get_adapter_class to simulate an unregistered type fallback
        with mock.patch("adapters.get_adapter_class", return_value=None) as mock_get:
            with mock.patch("adapters.deliver_to_adapter") as mock_deliver:
                mock_deliver.return_value = (True, "legacy ok", "response body")
                ticket = deliver_via_registry(
                    agent_cfg,
                    message_text="test",
                    from_agents=["alice"],
                )
                mock_deliver.assert_called_once()
                self.assertTrue(ticket["ok"])
                self.assertEqual(ticket["sync_response"], "response body")

    def test_deliver_via_registry_without_context(self):
        """Should not crash when context is None."""
        agent_cfg = {"adapter": {"type": "manual"}}
        ticket = deliver_via_registry(
            agent_cfg,
            message_text="test",
            from_agents=["alice"],
        )
        self.assertIsInstance(ticket, dict)
        self.assertFalse(ticket["ok"])  # manual adapter always returns False

    def test_deliver_via_registry_ticket_has_required_fields(self):
        agent_cfg = {"adapter": {"type": "manual"}}
        ticket = deliver_via_registry(
            agent_cfg,
            message_text="test",
            from_agents=["alice"],
        )
        for key in ["ok", "adapter_type", "detail", "sync_response", "error", "response_mode"]:
            self.assertIn(key, ticket, f"Ticket missing key: {key}")

    def test_deliver_via_registry_manual_adapter_fails(self):
        """The manual adapter always returns ok=False."""
        agent_cfg = {"adapter": {"type": "manual"}}
        ticket = deliver_via_registry(
            agent_cfg,
            message_text="test",
            from_agents=["alice"],
        )
        self.assertFalse(ticket["ok"])
        self.assertEqual(ticket["adapter_type"], "manual")
        self.assertIn("manual", ticket["error"])


if __name__ == "__main__":
    unittest.main()
