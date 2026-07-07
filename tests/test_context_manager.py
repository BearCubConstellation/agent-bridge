import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from context_manager import (
    build_context_bundle,
    get_or_create_session,
    read_agent_memory,
    read_room_context,
    save_session,
    update_agent_memory,
    update_room_context,
)
from rooms import append_room_message, ensure_room


class ContextManagerTests(unittest.TestCase):
    def test_context_and_memory_are_room_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            ensure_room(tmp, {"id": "quiz", "agents": ["susu", "momo"]})
            update_room_context(tmp, "quiz", {"summary": "round one", "game_state": {"score": 1}})
            update_agent_memory(tmp, "quiz", "susu", {"role_memory": "player"})
            self.assertEqual(read_room_context(tmp, "quiz")["summary"], "round one")
            self.assertEqual(read_agent_memory(tmp, "quiz", "susu")["role_memory"], "player")

    def test_session_is_stable_per_agent_and_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            ensure_room(tmp, {"id": "quiz", "agents": ["susu"]})
            first = get_or_create_session(tmp, "quiz", "susu", "openclaw_channel")
            save_session(tmp, "quiz", "susu", "openclaw_channel", "native-1")
            second = get_or_create_session(tmp, "quiz", "susu", "openclaw_channel")
            self.assertEqual(first["agent_id"], "susu")
            self.assertEqual(second["native_session_id"], "native-1")

    def test_bundle_contains_recent_room_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            ensure_room(tmp, {"id": "quiz", "agents": ["susu", "momo"]})
            append_room_message(tmp, "quiz", "momo", "hello", to_agent="susu")
            bundle = build_context_bundle(tmp, "quiz", "susu", recent_limit=4)
            self.assertEqual(bundle["recent_messages"][-1]["text"], "hello")
            self.assertEqual(bundle["source"], "room_jsonl")


if __name__ == "__main__":
    unittest.main()
