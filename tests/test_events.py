#!/usr/bin/env python3
"""Tests for core/events.py — EventBus emit and read events."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))


# The events module imports from rooms (room_dir, append_room_log).
# We mock those to isolate event logic.


class TestEmitEvent(unittest.TestCase):
    """Test emit_event creates and persists events."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-events-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _room_dir(self, shared_dir, room_id):
        """Real room_dir behavior — creates directory."""
        d = Path(shared_dir) / "rooms" / room_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @mock.patch("events.append_room_log")
    def test_emit_event_creates_events_jsonl(self, mock_log):
        with mock.patch("events.room_dir", side_effect=self._room_dir):
            from events import emit_event

            evt = emit_event(
                shared_dir=str(self.tmpdir),
                room_id="test-room",
                event_type="room.started",
                actor="alice",
            )

        # Check return value
        self.assertEqual(evt["room"], "test-room")
        self.assertEqual(evt["type"], "room.started")
        self.assertEqual(evt["actor"], "alice")
        self.assertTrue(evt["id"].startswith("evt_"))
        self.assertIn("ts", evt)

        # Check file was created
        events_file = self.tmpdir / "rooms" / "test-room" / "events.jsonl"
        self.assertTrue(events_file.exists(), f"Expected {events_file} to exist")

        # Check content
        lines = events_file.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["room"], "test-room")
        self.assertEqual(parsed["type"], "room.started")
        self.assertEqual(parsed["actor"], "alice")

        # Verify append_room_log was called
        mock_log.assert_called_once()
        call_args = mock_log.call_args
        self.assertEqual(call_args[0][1], "test-room")

    @mock.patch("events.append_room_log")
    def test_emit_event_appends_multiple(self, mock_log):
        with mock.patch("events.room_dir", side_effect=self._room_dir):
            from events import emit_event

            emit_event(str(self.tmpdir), "test-room", "room.started", actor="alice")
            emit_event(str(self.tmpdir), "test-room", "message.created", actor="bob")
            emit_event(str(self.tmpdir), "test-room", "room.paused", actor="system")

        events_file = self.tmpdir / "rooms" / "test-room" / "events.jsonl"
        lines = events_file.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 3)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(parsed[0]["type"], "room.started")
        self.assertEqual(parsed[1]["type"], "message.created")
        self.assertEqual(parsed[2]["type"], "room.paused")

    @mock.patch("events.append_room_log")
    def test_emit_event_with_turn_id(self, mock_log):
        with mock.patch("events.room_dir", side_effect=self._room_dir):
            from events import emit_event

            evt = emit_event(
                str(self.tmpdir), "test-room", "turn.completed",
                actor="alice", turn_id="turn_123",
            )
        self.assertEqual(evt["turn_id"], "turn_123")

    @mock.patch("events.append_room_log")
    def test_emit_event_with_correlation_id(self, mock_log):
        with mock.patch("events.room_dir", side_effect=self._room_dir):
            from events import emit_event

            evt = emit_event(
                str(self.tmpdir), "test-room", "agent.wakeup.succeeded",
                correlation_id="corr_abc",
            )
        self.assertEqual(evt["correlation_id"], "corr_abc")

    @mock.patch("events.append_room_log")
    def test_emit_event_with_message_id(self, mock_log):
        with mock.patch("events.room_dir", side_effect=self._room_dir):
            from events import emit_event

            evt = emit_event(
                str(self.tmpdir), "test-room", "message.created",
                message_id="msg_456",
            )
        self.assertEqual(evt["message_id"], "msg_456")

    @mock.patch("events.append_room_log")
    def test_emit_event_with_meta(self, mock_log):
        with mock.patch("events.room_dir", side_effect=self._room_dir):
            from events import emit_event

            meta = {"retry": 3, "source": "scheduler"}
            evt = emit_event(
                str(self.tmpdir), "test-room", "agent.wakeup.failed",
                meta=meta,
            )
        self.assertEqual(evt["meta"], meta)

    @mock.patch("events.append_room_log")
    def test_emit_event_meta_defaults_to_dict(self, mock_log):
        with mock.patch("events.room_dir", side_effect=self._room_dir):
            from events import emit_event

            evt = emit_event(str(self.tmpdir), "test-room", "room.started")
        self.assertEqual(evt["meta"], {})


class TestReadEvents(unittest.TestCase):
    """Test read_events reads back events from disk."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-events-"))
        self.room_dir_path = self.tmpdir / "rooms" / "test-room"
        self.room_dir_path.mkdir(parents=True, exist_ok=True)
        self.events_path = self.room_dir_path / "events.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_event(self, event_type, actor=""):
        """Helper: write one event line to events.jsonl."""
        from protocol import make_event
        from storage import append_jsonl
        evt = make_event(room="test-room", event_type=event_type, actor=actor)
        append_jsonl(self.events_path, evt)
        return evt

    def test_read_events_returns_all(self):
        e1 = self._make_event("room.started", "alice")
        e2 = self._make_event("message.created", "bob")

        from events import read_events
        events = read_events(str(self.tmpdir), "test-room")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], e1["type"])
        self.assertEqual(events[1]["type"], e2["type"])

    def test_read_events_with_limit(self):
        for i in range(10):
            self._make_event("room.started", f"actor{i}")

        from events import read_events
        events = read_events(str(self.tmpdir), "test-room", limit=3)
        self.assertEqual(len(events), 3)

    def test_read_events_default_limit(self):
        """Default limit is 500."""
        for i in range(600):
            self._make_event("room.started", f"actor{i}")

        from events import read_events
        events = read_events(str(self.tmpdir), "test-room")
        self.assertEqual(len(events), 500)

    def test_read_events_missing_room_returns_empty(self):
        from events import read_events
        events = read_events(str(self.tmpdir), "nonexistent-room")
        self.assertEqual(events, [])

    def test_read_events_returns_newest_last(self):
        """Records should be returned in append order (oldest first)."""
        self._make_event("room.started")
        import time
        time.sleep(0.01)
        self._make_event("room.paused")

        from events import read_events
        events = read_events(str(self.tmpdir), "test-room")
        self.assertEqual(events[0]["type"], "room.started")
        self.assertEqual(events[1]["type"], "room.paused")


if __name__ == "__main__":
    unittest.main()
