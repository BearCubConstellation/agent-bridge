#!/usr/bin/env python3
"""Focused compatibility tests for the V2 room runtime.

Detailed race, retry, routing and scheduler tests live in
``test_v2_runtime_hardening.py``.  This module keeps only stable public
contracts and avoids mocking pre-V2 read/write internals.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import runtime
from protocol import ROOM_PAUSED, ROOM_RUNNING
from room_state import read_room_state_consistent
from rooms import append_room_message


class RuntimePublicContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shared = Path(self.tmp.name)
        self.config = {
            "shared_dir": str(self.shared),
            "server": {"host": "127.0.0.1", "port": 8825},
            "agents": {
                "agent_a": {"adapter": {"type": "native_http", "config": {"url": "http://127.0.0.1/a"}}},
                "agent_b": {"adapter": {"type": "native_http", "config": {"url": "http://127.0.0.1/b"}}},
            },
            "rooms": {
                "room": {"id": "room", "agents": ["agent_a", "agent_b"], "order": ["agent_a", "agent_b"], "status": ROOM_RUNNING},
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_room_returns_error(self):
        result = runtime.run_room_step(self.config, "missing")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])

    def test_paused_room_is_not_delivered(self):
        self.config["rooms"]["room"]["status"] = ROOM_PAUSED
        result = runtime.run_room_step(self.config, "room")
        self.assertTrue(result["ok"])
        self.assertEqual("noop", result["action"])

    def test_delivery_enters_waiting_state(self):
        append_room_message(self.shared, "room", "user", "hello", to_agent="agent_a", kind="user")
        ticket = {"ok": True, "response_mode": "callback", "detail": "queued"}
        with patch("runtime.deliver_via_registry", return_value=ticket):
            result = runtime.run_room_step(self.config, "room")
        self.assertEqual("waiting", result["action"])
        state = read_room_state_consistent(self.shared, "room", self.config["rooms"]["room"])
        self.assertEqual("agent_a", state["current_turn"]["agent_id"])


if __name__ == "__main__":
    unittest.main()
