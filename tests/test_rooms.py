#!/usr/bin/env python3
"""Room runtime tests."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from rooms import (  # noqa: E402
    append_room_message,
    ensure_room,
    read_room_cursor,
    read_room_messages,
    read_room_state,
    tick_room,
    write_room_state,
)


class TestRoomRuntime(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-room-"))
        self.room = {
            "id": "room_dev",
            "name": "Dev",
            "agents": ["alice", "bob"],
            "order": ["alice", "bob"],
            "policy": "round_robin",
            "status": "running",
            "max_turns": 10,
        }
        self.config = {
            "shared_dir": str(self.tmpdir),
            "agents": {
                "alice": {
                    "id": "alice",
                    "adapter": {"type": "file_inbox", "config": {"path": str(self.tmpdir / "alice.jsonl")}},
                },
                "bob": {
                    "id": "bob",
                    "adapter": {"type": "file_inbox", "config": {"path": str(self.tmpdir / "bob.jsonl")}},
                },
            },
            "rooms": {"room_dev": self.room},
        }
        ensure_room(self.tmpdir, self.room)
        state = read_room_state(self.tmpdir, "room_dev", self.room)
        state["status"] = "running"
        write_room_state(self.tmpdir, "room_dev", state)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_room_message_uses_room_directory_envelope(self):
        msg = append_room_message(self.tmpdir, "room_dev", "alice", "hello", to_agent="bob", kind="agent")

        messages = read_room_messages(self.tmpdir, "room_dev")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], msg["id"])
        self.assertEqual(messages[0]["room"], "room_dev")
        self.assertEqual(messages[0]["to"], "bob")
        self.assertTrue((self.tmpdir / "rooms" / "room_dev" / "active.jsonl").exists())

    def test_round_robin_waits_for_current_agent_reply_before_advancing(self):
        append_room_message(self.tmpdir, "room_dev", "user", "start", kind="user")

        with mock.patch("rooms.deliver_to_adapter", return_value=(True, "ok")) as deliver:
            first = tick_room(self.config, "room_dev", force=True)
            second = tick_room(self.config, "room_dev", force=True)
            append_room_message(self.tmpdir, "room_dev", "alice", "alice reply")
            third = tick_room(self.config, "room_dev", force=True)
            fourth = tick_room(self.config, "room_dev", force=True)

        self.assertTrue(first["delivered"])
        self.assertEqual(first["to_agent"], "alice")
        self.assertEqual(first["waiting_for"], "alice")
        self.assertFalse(second["delivered"])
        self.assertEqual(second["waiting_for"], "alice")
        self.assertTrue(third.get("response_seen"))
        self.assertFalse(third["delivered"])
        self.assertTrue(fourth["delivered"])
        self.assertEqual(fourth["to_agent"], "bob")
        self.assertEqual(deliver.call_count, 2)
        self.assertEqual(read_room_cursor(self.tmpdir, "room_dev", "alice"), 1)

    def test_manual_adapter_marks_room_error_without_advancing_cursor(self):
        self.config["agents"]["alice"] = {"id": "alice", "adapter": {"type": "manual"}}
        append_room_message(self.tmpdir, "room_dev", "user", "start", kind="user")

        result = tick_room(self.config, "room_dev", force=True)
        state = read_room_state(self.tmpdir, "room_dev", self.room)

        self.assertFalse(result["ok"])
        self.assertIn("not auto-triggerable", result["error"])
        self.assertEqual(state["status"], "error")
        self.assertEqual(read_room_cursor(self.tmpdir, "room_dev", "alice"), 0)


if __name__ == "__main__":
    unittest.main()
