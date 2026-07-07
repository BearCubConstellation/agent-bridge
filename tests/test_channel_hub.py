#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from channel_hub import ChannelHub
from rooms import read_room_messages


class ChannelHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.shared = Path(self.temp.name)
        self.config = {
            "rooms": {
                "room": {"id": "room", "agents": ["susu", "momo"], "order": ["susu", "momo"]},
            },
            "channel": {"host": "127.0.0.1", "port": 0, "allow_unauthenticated_local": True},
        }
        self.hub = ChannelHub(self.shared, lambda: self.config)

    def tearDown(self):
        self.hub.stop()
        self.temp.cleanup()

    def test_direct_message_persists_and_enqueues(self):
        message = self.hub.publish("susu", {"id": "m1", "room_id": "room", "to": "momo", "text": "hello"})
        self.assertEqual(["momo"], message["to"])
        messages = read_room_messages(self.shared, "room")
        self.assertEqual("hello", messages[-1]["msg"])
        queued = list(self.hub._read_jsonl(self.hub._inbox_path("momo")))
        self.assertEqual("m1", queued[-1]["id"])

    def test_broadcast_routes_to_other_members_only(self):
        self.hub.publish("susu", {"id": "m2", "room_id": "room", "text": "everyone"})
        self.assertEqual([], list(self.hub._read_jsonl(self.hub._inbox_path("susu"))))
        self.assertEqual("m2", list(self.hub._read_jsonl(self.hub._inbox_path("momo")))[-1]["id"])

    def test_duplicate_message_id_is_idempotent(self):
        payload = {"id": "dedup", "room_id": "room", "to": "momo", "text": "one"}
        self.hub.publish("susu", payload)
        self.hub.publish("susu", payload)
        self.assertEqual(1, len(read_room_messages(self.shared, "room")))
        self.assertEqual(1, len(list(self.hub._read_jsonl(self.hub._inbox_path("momo")))))

    def test_rejects_non_member_and_unknown_recipient(self):
        with self.assertRaises(ValueError):
            self.hub.publish("outsider", {"room_id": "room", "to": "momo", "text": "x"})
        with self.assertRaises(ValueError):
            self.hub.publish("susu", {"room_id": "room", "to": "nobody", "text": "x"})

    def test_nonlocal_requires_configured_tokens(self):
        self.assertTrue(self.hub.validate_exposure("0.0.0.0"))
        self.config["channel"]["tokens"] = {"susu": "secret"}
        self.assertEqual("", self.hub.validate_exposure("0.0.0.0"))

    def test_registration_rejects_unresolved_configured_token(self):
        self.config["channel"]["tokens"] = {"susu": "${AGENT_BRIDGE_MISSING_TEST_TOKEN}"}
        os.environ.pop("AGENT_BRIDGE_MISSING_TEST_TOKEN", None)
        with self.assertRaisesRegex(ValueError, "resolution"):
            self.hub._authenticate_registration({"agent_id": "susu", "token": ""})


if __name__ == "__main__":
    unittest.main()
