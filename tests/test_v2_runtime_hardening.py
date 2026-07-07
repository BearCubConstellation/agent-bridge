#!/usr/bin/env python3
import importlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT))

import runtime
from room_state import mutate_room_state, read_room_state_consistent
from rooms import append_room_message, read_room_messages
from scheduler import Scheduler
from security import (
    extract_token_from_request,
    validate_network_exposure,
    verify_callback_token,
    verify_mcp_token,
)


class RuntimeHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.shared = Path(self.temp.name)
        self.config = {
            "shared_dir": str(self.shared),
            "server": {"host": "127.0.0.1", "port": 8825},
            "agents": {
                "a": {"adapter": {"type": "native_http", "config": {"url": "http://127.0.0.1/a"}}},
                "b": {"adapter": {"type": "native_http", "config": {"url": "http://127.0.0.1/b"}}},
            },
            "rooms": {
                "r": {"id": "r", "agents": ["a", "b"], "order": ["a", "b"], "status": "running", "policy": {"on_timeout": "retry"}},
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _ticket():
        return {"ok": True, "response_mode": "callback", "detail": "queued"}

    def test_directed_message_does_not_wait_for_round_robin_cursor(self):
        append_room_message(self.shared, "r", "user", "for b", to_agent="b", kind="user")
        with patch("runtime.deliver_via_registry", return_value=self._ticket()) as deliver:
            result = runtime.run_room_step(self.config, "r")
        self.assertTrue(result["ok"])
        self.assertEqual("b", result["to_agent"])
        self.assertEqual(1, deliver.call_count)

    def test_duplicate_callback_writes_one_message(self):
        append_room_message(self.shared, "r", "user", "hello", to_agent="a", kind="user")
        with patch("runtime.deliver_via_registry", return_value=self._ticket()):
            runtime.run_room_step(self.config, "r")
        state = read_room_state_consistent(self.shared, "r", self.config["rooms"]["r"])
        turn = state["current_turn"]
        first = runtime.receive_agent_response(self.shared, "r", "a", "answer", turn["turn_id"], turn["correlation_id"])
        duplicate = runtime.receive_agent_response(self.shared, "r", "a", "answer", turn["turn_id"], turn["correlation_id"])
        self.assertTrue(first["ok"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(2, len(read_room_messages(self.shared, "r")))

    def test_callback_arriving_during_delivery_is_not_overwritten(self):
        append_room_message(self.shared, "r", "user", "hello", to_agent="a", kind="user")

        def early_callback(_agent_cfg, _text, _from, context):
            runtime.receive_agent_response(
                self.shared, "r", "a", "early answer",
                context["turn_id"], context["correlation_id"], source="test",
            )
            return self._ticket()

        with patch("runtime.deliver_via_registry", side_effect=early_callback):
            runtime.run_room_step(self.config, "r")
        state = read_room_state_consistent(self.shared, "r", self.config["rooms"]["r"])
        self.assertTrue(state["current_turn"]["response_message_id"])
        completed = runtime.run_room_step(self.config, "r")
        self.assertEqual("response_received", completed["action"])

    def test_retry_redelivers_the_same_turn(self):
        append_room_message(self.shared, "r", "user", "hello", to_agent="a", kind="user")
        calls = []

        def no_reply(*args):
            calls.append(args)
            return self._ticket()

        with patch("runtime.deliver_via_registry", side_effect=no_reply):
            runtime.run_room_step(self.config, "r")

            def expire(state):
                state["current_turn"]["timeout_at"] = "2000-01-01 00:00:00"
                return True

            mutate_room_state(self.shared, "r", self.config["rooms"]["r"], expire)
            runtime.run_room_step(self.config, "r")
        self.assertEqual(2, len(calls))


class SchedulerHardeningTests(unittest.TestCase):
    def test_slow_room_does_not_block_another_room(self):
        started = []
        released = threading.Event()
        fast_done = threading.Event()

        def step(_config, room_id):
            started.append(room_id)
            if room_id == "slow":
                released.wait(1.5)
            else:
                fast_done.set()
            return {"ok": True}

        scheduler = Scheduler(max_workers=2, idle_interval=0.01)
        scheduler.set_config({"rooms": {"slow": {}, "fast": {}}})
        with patch("runtime.run_room_step", side_effect=step):
            scheduler.start()
            scheduler.schedule_room("slow")
            scheduler.schedule_room("fast")
            self.assertTrue(fast_done.wait(0.7))
            released.set()
            time.sleep(0.05)
            scheduler.stop()
        self.assertIn("slow", started)
        self.assertIn("fast", started)


class SecurityHardeningTests(unittest.TestCase):
    def test_non_loopback_bind_requires_callback_and_mcp_tokens(self):
        self.assertTrue(validate_network_exposure({}, "0.0.0.0"))
        self.assertEqual("", validate_network_exposure({"security": {"callback_token": "x", "mcp_token": "y"}}, "0.0.0.0"))
        self.assertEqual("", validate_network_exposure({}, "127.0.0.1"))

    def test_nonlocal_callback_and_mcp_reject_missing_tokens(self):
        cfg = {"server": {"host": "0.0.0.0"}, "security": {}}
        self.assertFalse(verify_callback_token(cfg, "agent", "")[0])
        self.assertFalse(verify_mcp_token(cfg, "")[0])
        self.assertEqual("", extract_token_from_request({}, {"token": "leaky"}))

    def test_server_module_imports(self):
        module = importlib.import_module("ui.server")
        self.assertTrue(hasattr(module, "BridgeHandler"))


if __name__ == "__main__":
    unittest.main()
