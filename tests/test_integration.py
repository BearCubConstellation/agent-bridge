#!/usr/bin/env python3
"""Integration tests for Agent Bridge — hard fix verification.

Tests:
  1. Callback flow (runtime.receive_agent_response)
  2. Scheduler worker (scheduler.Scheduler)
  3. OpenClaw adapter payload format (openclaw_sessions.OpenClawSessionsAdapter)
  4. Adapter type resolution (adapters.normalize_adapter, adapter_capability)
  5. append_room_message with reply_to/correlation_id (rooms.append_room_message)
  6. send_json with custom status parameter (ui/server BridgeHandler.send_json)
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# ── Import path setup ──────────────────────────────────
_CORE = str(Path(__file__).resolve().parent.parent / "core")
_UI = str(Path(__file__).resolve().parent.parent / "ui")
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
if _UI not in sys.path:
    sys.path.insert(0, _UI)

# ── Imports ────────────────────────────────────────────
from protocol import (                                                    # noqa: E402
    ROOM_RUNNING, ROOM_PAUSED,
    TURN_WAITING_RESPONSE, TURN_COMPLETED,
    EVT_AGENT_RESPONSE_RECEIVED, EVT_TURN_COMPLETED,
    RESPONSE_CALLBACK, ADAPTER_OPENCLAW_SESSIONS,
    ADAPTER_MCP_TOOL, ADAPTER_FILE_MAILBOX,
    make_turn,
)
from rooms import (                                                        # noqa: E402
    append_room_message, ensure_room, normalize_room,
    read_room_state, write_room_state, room_dir, room_active_file,
    read_room_messages,
)
from runtime import run_room_step, receive_agent_response                  # noqa: E402
from scheduler import Scheduler                                            # noqa: E402
from adapters import normalize_adapter, adapter_capability                 # noqa: E402
from events import read_events, emit_event                                 # noqa: E402
from poll import parse_jsonl                                               # noqa: E402


# ══════════════════════════════════════════════════════════
# Test 1: Callback flow integration
# ══════════════════════════════════════════════════════════

class TestCallbackFlow(unittest.TestCase):
    """Verify the full callback flow: run_room_step → delivery →
    receive_agent_response → turn completed → events emitted."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-callback-"))
        self.shared_dir = self.tmpdir / "shared"
        self.shared_dir.mkdir(parents=True, exist_ok=True)

        # Config with 2 agents: agent-A (cli, sync) and agent-B (callback)
        self.config = {
            "shared_dir": str(self.shared_dir),
            "server": {"host": "127.0.0.1", "port": 17899},
            "rooms": {
                "test_callback_room": {
                    "status": "running",
                    "agents": ["agent-A", "agent-B"],
                    "order": ["agent-A", "agent-B"],
                    "policy": "round_robin",
                    "max_turns": 50,
                }
            },
            "agents": {
                "agent-A": {
                    "adapter": {
                        "type": "cli",
                        "config": {
                            "command": "echo '{\"result\": \"sync response from A\"}'",
                            "timeout": 10,
                        },
                    },
                },
                "agent-B": {
                    "adapter": {
                        "type": "openclaw_sessions",
                        "config": {
                            "url": "http://127.0.0.1:19999/fake-openclaw",
                            "sessionsKey": "test-session-key",
                        },
                    },
                },
            },
        }
        self.room_id = "test_callback_room"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _inject_user_message(self, from_agent, text):
        """Simulate a user sending a message to the room."""
        append_room_message(self.shared_dir, self.room_id, from_agent,
                            text, kind="user")

    def test_callback_flow(self):
        """Full callback flow: user message → delivery → callback → completed."""
        # 1. Inject a user message
        self._inject_user_message("user", "Hello agents!")

        # 2. Run one step — agent-A (cli/sync) should get the turn
        result = run_room_step(self.config, self.room_id)

        # agent-A is cli adapter with sync mode → should get a sync_response
        self.assertTrue(result["ok"], f"run_room_step failed: {result}")
        self.assertEqual(result.get("to_agent"), "agent-A",
                         f"Expected agent-A, got: {result}")

        # In sync mode, the response is written immediately and turn completes
        if result["action"] == "sync_response":
            # After sync, next turn is scheduled. But let's pause it and
            # test callback manually via agent-B path instead.
            # Reset room to test callback flow more clearly.
            pass

        # For a cleaner callback test, set up the room with only callback agents
        # Reset config to use only callback agents
        self.config["rooms"][self.room_id]["order"] = ["agent-B"]
        write_room_state(
            self.shared_dir, self.room_id,
            {"status": "running", "order": ["agent-B"],
             "turn_index": 0, "turn_count": 0, "round": 0,
             "max_turns": 50, "current_turn": None,
             "last_error": "", "last_message_id": "",
             "waiting_for": "", "waiting_line": 0},
        )

        # 1b. Inject another message
        self._inject_user_message("user", "Hello agent-B!")

        # 2b. Run step triggering agent-B (openclaw_sessions → callback mode)
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = mock.MagicMock(return_value=False)
            mock_resp.read.return_value = b'{"ok":true}'
            mock_urlopen.return_value = mock_resp

            result = run_room_step(self.config, self.room_id)

        self.assertTrue(result["ok"], f"Step failed: {result}")
        self.assertEqual(result.get("to_agent"), "agent-B")

        # Agent B is callback mode → waiting_response
        self.assertIn(result["action"], ("waiting", "noop"),
                      f"Expected waiting/noop, got {result['action']}")

        # 3. Read state to get turn_id and correlation_id
        state = read_room_state(self.shared_dir, self.room_id)
        current_turn = state.get("current_turn", {})
        turn_id = current_turn.get("turn_id", "")
        correlation_id = current_turn.get("correlation_id", "")
        self.assertTrue(turn_id, "Expected non-empty turn_id")
        self.assertTrue(correlation_id, "Expected non-empty correlation_id")
        self.assertEqual(current_turn.get("state"), TURN_WAITING_RESPONSE)

        # 4. Simulate callback via receive_agent_response
        cb_result = receive_agent_response(
            self.shared_dir, self.room_id, "agent-B",
            "Response from agent-B via callback!",
            turn_id=turn_id, correlation_id=correlation_id,
            source="callback",
        )

        self.assertTrue(cb_result["ok"], f"Callback failed: {cb_result}")
        msg_id = cb_result.get("message_id", "")
        self.assertTrue(msg_id, "Expected non-empty message_id from callback")

        # 5. Verify message is in active.jsonl with reply_to and correlation_id
        active_path = room_active_file(self.shared_dir, self.room_id)
        messages = parse_jsonl(active_path)
        agent_b_msgs = [m for m in messages
                        if m.get("from") == "agent-B"]
        self.assertGreater(len(agent_b_msgs), 0,
                           "No messages from agent-B found in active.jsonl")

        cb_msg = None
        for m in agent_b_msgs:
            if m.get("msg") == "Response from agent-B via callback!":
                cb_msg = m
                break
        self.assertIsNotNone(cb_msg, "Callback message not found in active.jsonl")
        self.assertEqual(cb_msg.get("reply_to"), turn_id,
                         f"reply_to should be {turn_id}, got {cb_msg.get('reply_to')}")
        self.assertEqual(cb_msg.get("correlation_id"), correlation_id,
                         f"correlation_id should be {correlation_id}, "
                         f"got {cb_msg.get('correlation_id')}")

        # 6. Verify turn state transitions after callback reception
        state = read_room_state(self.shared_dir, self.room_id)
        current_turn = state.get("current_turn", {})
        self.assertEqual(
            current_turn.get("response_message_id"), msg_id,
            f"response_message_id mismatch: {current_turn.get('response_message_id')} vs {msg_id}"
        )

        # 7. Run another step — should complete the turn and advance
        with mock.patch("urllib.request.urlopen") as mock_urlopen2:
            mock_resp = mock.MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = mock.MagicMock(return_value=False)
            mock_resp.read.return_value = b'{"ok":true}'
            mock_urlopen2.return_value = mock_resp

            # Patch scheduler to avoid scheduling issues
            try:
                from scheduler import get_scheduler
                get_scheduler().schedule_room(self.room_id)
            except Exception:
                pass

            # Actually, _complete_turn_and_advance handles this internally
            # Just run the step again
            result2 = run_room_step(self.config, self.room_id)

        # The turn should complete (response already received)
        self.assertTrue(result2["ok"], f"Second step failed: {result2}")

        # State should show no current_turn (completed)
        state_after = read_room_state(self.shared_dir, self.room_id)
        self.assertIsNone(
            state_after.get("current_turn"),
            "current_turn should be None after turn completion"
        )

        # 8. Verify events emitted to events.jsonl
        events = read_events(self.shared_dir, self.room_id)
        event_types = [e.get("type") for e in events]

        # Should have response received + turn completed events
        self.assertIn(EVT_AGENT_RESPONSE_RECEIVED, event_types,
                      "Missing EVT_AGENT_RESPONSE_RECEIVED in events")
        self.assertIn(EVT_TURN_COMPLETED, event_types,
                      "Missing EVT_TURN_COMPLETED in events")

        # Verify event details
        resp_events = [e for e in events
                       if e.get("type") == EVT_AGENT_RESPONSE_RECEIVED]
        self.assertGreater(len(resp_events), 0)
        resp_event = resp_events[-1]
        self.assertEqual(resp_event.get("actor"), "agent-B")
        self.assertEqual(resp_event.get("turn_id"), turn_id)
        self.assertEqual(resp_event.get("correlation_id"), correlation_id)
        self.assertEqual(resp_event.get("message_id"), msg_id)


# ══════════════════════════════════════════════════════════
# Test 2: Scheduler worker integration
# ══════════════════════════════════════════════════════════

class TestSchedulerWorkerIntegration(unittest.TestCase):
    """Verify the scheduler worker thread processes queued rooms."""

    def setUp(self):
        self.scheduler = Scheduler(idle_interval=0.1)

    def tearDown(self):
        if self.scheduler.is_running:
            self.scheduler.stop(timeout=3.0)

    def test_worker_processes_queued_room(self):
        """Worker drains queue and calls _run_step for queued rooms."""
        config = {"rooms": {"test-room": {"status": "running"}}}

        # Patch _run_step and set_config
        mock_run_step = mock.MagicMock(return_value={
            "ok": True, "room_id": "test-room", "action": "noop"
        })
        self.scheduler.set_config(config)

        # Queue a room
        self.scheduler.schedule_room("test-room")
        self.assertEqual(self.scheduler.queue_size, 1)

        with mock.patch.object(self.scheduler, "_run_step", mock_run_step):
            self.scheduler.start()
            # Wait for worker to drain the queue
            time.sleep(0.8)
            self.scheduler.stop(timeout=2.0)

        # Verify _run_step was called
        mock_run_step.assert_called_with(config, "test-room")

        # Queue should be drained
        self.assertEqual(self.scheduler.queue_size, 0,
                         f"Queue should be empty after processing, "
                         f"got {self.scheduler.queue_size}")

    def test_worker_drains_queue_after_processing(self):
        """After scheduler stops, queue should be empty."""
        config = {"rooms": {}}
        self.scheduler.set_config(config)

        mock_run_step = mock.MagicMock(return_value={
            "ok": True, "room_id": "dummy", "action": "noop"
        })

        self.scheduler.schedule_room("room-1")
        self.scheduler.schedule_room("room-2")
        self.assertEqual(self.scheduler.queue_size, 2)

        with mock.patch.object(self.scheduler, "_run_step", mock_run_step):
            self.scheduler.start()
            time.sleep(1.0)
            self.scheduler.stop(timeout=2.0)

        # Both should be processed
        self.assertEqual(self.scheduler.queue_size, 0)
        self.assertGreaterEqual(mock_run_step.call_count, 2)

    def test_stop_clean_shutdown(self):
        """Scheduler stops cleanly even with items in queue."""
        config = {"rooms": {}}
        self.scheduler.set_config(config)
        self.scheduler.schedule_room("room-x")

        self.scheduler.start()
        self.assertTrue(self.scheduler.is_running)
        self.scheduler.stop(timeout=2.0)
        self.assertFalse(self.scheduler.is_running)

        # Worker thread should be cleaned up
        self.assertIsNone(self.scheduler._worker)


# ══════════════════════════════════════════════════════════
# Test 3: OpenClaw adapter payload format
# ══════════════════════════════════════════════════════════

class TestOpenClawPayloadFormat(unittest.TestCase):
    """Verify OpenClawSessionsAdapter sends the correct sessions_send payload."""

    def test_wake_builds_correct_payload(self):
        """wake() should POST {"tool":"sessions_send","args":{...}} to the URL."""
        from adapters.openclaw_sessions import OpenClawSessionsAdapter

        adapter = OpenClawSessionsAdapter()

        delivery_request = {
            "room_id": "my-room",
            "agent_id": "my-agent",
            "turn_id": "turn_test123",
            "correlation_id": "corr_test456",
            "message": "Hello from the bridge!",
            "from": "other-agent",
            "callback_url": "http://127.0.0.1:7899/api/rooms/my-room/agents/my-agent/callback",
            "adapter": {
                "type": "openclaw_sessions",
                "config": {
                    "url": "http://localhost:9999/tools/invoke",
                    "sessionsKey": "session-abc-123",
                    "method": "POST",
                    "timeout": 30,
                    "headers": {},
                },
                "auth": {},
                "message_template": {},
            },
        }

        # Patch urllib.request.urlopen to capture the request
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = mock.MagicMock(return_value=False)
            mock_resp.read.return_value = b'{"status":"ok"}'
            mock_urlopen.return_value = mock_resp

            ticket = adapter.wake(delivery_request)

        # Check ticket
        self.assertTrue(ticket["ok"], f"Ticket not ok: {ticket}")

        # Get the captured request
        self.assertTrue(mock_urlopen.called, "urlopen was not called")
        call_args = mock_urlopen.call_args
        req = call_args[0][0]  # urllib.request.Request

        # Verify payload body
        payload_body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload_body["tool"], "sessions_send",
                         f"Expected tool=sessions_send, got {payload_body.get('tool')}")

        args = payload_body.get("args", {})
        self.assertIsInstance(args, dict, "args should be a dict")

        # Verify all required args fields
        self.assertEqual(args.get("sessionKey"), "session-abc-123")
        # message now contains callback instructions appended to original text
        self.assertIn("Hello from the bridge!", args.get("message", ""))
        self.assertIn("turn_id=turn_test123", args.get("message", ""))
        self.assertIn("callback_url=http://127.0.0.1:7899/api/rooms/my-room/agents/my-agent/callback", args.get("message", ""))
        self.assertIn("agent_bridge.reply_turn", args.get("message", ""))
        # Structured fields also present
        self.assertEqual(args.get("turn_id"), "turn_test123")
        self.assertEqual(args.get("correlation_id"), "corr_test456")
        self.assertEqual(
            args.get("callback_url"),
            "http://127.0.0.1:7899/api/rooms/my-room/agents/my-agent/callback"
        )
        self.assertEqual(args.get("room_id"), "my-room")
        self.assertEqual(args.get("from"), "other-agent")

    def test_wake_without_url_returns_error(self):
        """wake() should return ok=False if no URL configured."""
        from adapters.openclaw_sessions import OpenClawSessionsAdapter

        adapter = OpenClawSessionsAdapter()

        delivery_request = {
            "room_id": "my-room",
            "agent_id": "my-agent",
            "turn_id": "turn_test123",
            "correlation_id": "corr_test456",
            "message": "Hello!",
            "from": "other-agent",
            "callback_url": "http://127.0.0.1:7899/callback",
            "adapter": {
                "type": "openclaw_sessions",
                "config": {
                    "url": "",  # empty URL
                    "sessionsKey": "session-abc-123",
                },
                "auth": {},
                "message_template": {},
            },
        }

        ticket = adapter.wake(delivery_request)
        self.assertFalse(ticket["ok"], "Expected ticket to not be ok")
        self.assertIn("url is empty", ticket.get("error", ""))


# ══════════════════════════════════════════════════════════
# Test 4: Adapter type resolution
# ══════════════════════════════════════════════════════════

class TestAdapterTypeResolution(unittest.TestCase):
    """Verify normalize_adapter and adapter_capability resolve types correctly."""

    def test_openclaw_sessions_type(self):
        """openclaw_sessions should NOT be resolved to manual."""
        adapter = normalize_adapter({
            "adapter": {
                "type": "openclaw_sessions",
                "config": {"url": "http://localhost", "sessionsKey": "test"},
            },
        })
        self.assertEqual(adapter["type"], "openclaw_sessions",
                         f"Expected openclaw_sessions, got {adapter['type']}")

    def test_mcp_tool_type(self):
        """mcp_tool should be preserved."""
        adapter = normalize_adapter({
            "adapter": {
                "type": "mcp_tool",
                "config": {"tool_name": "my_tool"},
            },
        })
        self.assertEqual(adapter["type"], "mcp_tool",
                         f"Expected mcp_tool, got {adapter['type']}")

    def test_file_mailbox_type(self):
        """file_mailbox should be preserved."""
        adapter = normalize_adapter({
            "adapter": {
                "type": "file_mailbox",
                "config": {"inbox_dir": "/tmp/mailbox"},
            },
        })
        self.assertEqual(adapter["type"], "file_mailbox",
                         f"Expected file_mailbox, got {adapter['type']}")

    def test_unknown_type_falls_back_to_manual(self):
        """Unknown adapter type should fall back to manual."""
        adapter = normalize_adapter({
            "adapter": {
                "type": "nonexistent_type_xyz",
                "config": {},
            },
        })
        self.assertEqual(adapter["type"], "manual",
                         f"Expected manual fallback, got {adapter['type']}")

    def test_missing_adapter_falls_back_to_manual(self):
        """Missing adapter config should fall back to manual from wakeup."""
        adapter = normalize_adapter({})
        self.assertEqual(adapter["type"], "manual")

    def test_adapter_capability_openclaw_sessions(self):
        """adapter_capability for openclaw_sessions returns correct capabilities."""
        cap = adapter_capability({
            "adapter": {
                "type": "openclaw_sessions",
                "config": {"url": "http://localhost", "sessionsKey": "test"},
            },
        })
        self.assertEqual(cap["type"], "openclaw_sessions")
        self.assertTrue(cap.get("automatic"), "openclaw_sessions should be automatic")
        self.assertTrue(cap.get("configured"), "openclaw_sessions should be configured")
        self.assertIn("callback", cap.get("response_modes", []),
                      "openclaw_sessions should support callback mode")
        self.assertTrue(cap.get("requires_callback_url"),
                        "openclaw_sessions should require callback_url")

    def test_adapter_capability_mcp_tool(self):
        """adapter_capability for mcp_tool returns correct capabilities."""
        cap = adapter_capability({
            "adapter": {
                "type": "mcp_tool",
                "config": {"tool_name": "test_tool"},
            },
        })
        self.assertEqual(cap["type"], "mcp_tool")
        # mcp_tool is intentionally NOT automatic — it requires external invocation
        self.assertFalse(cap.get("automatic"), "mcp_tool should NOT be automatic")
        self.assertFalse(cap.get("configured"),
                         "mcp_tool should not be configured without instructions_template")
        self.assertIn("mcp_tool", cap.get("response_modes", []),
                      "mcp_tool should support mcp_tool response mode")

    def test_adapter_capability_manual(self):
        """adapter_capability for manual returns non-automatic."""
        cap = adapter_capability({
            "adapter": {"type": "manual"},
        })
        self.assertEqual(cap["type"], "manual")
        self.assertFalse(cap.get("automatic"), "manual should not be automatic")
        self.assertFalse(cap.get("configured"), "manual should not be configured")


# ══════════════════════════════════════════════════════════
# Test 5: append_room_message with reply_to/correlation_id
# ══════════════════════════════════════════════════════════

class TestAppendRoomMessageFields(unittest.TestCase):
    """Verify append_room_message correctly persists reply_to and correlation_id."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-append-"))
        self.shared_dir = self.tmpdir / "shared"
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        self.room_id = "test_append_room"

        # Create room directory
        rdir = self.shared_dir / "rooms" / self.room_id
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "history").mkdir(exist_ok=True)
        (rdir / "cursors").mkdir(exist_ok=True)
        (rdir / "active.jsonl").touch(exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_append_with_reply_to_and_correlation_id(self):
        """append_room_message should write reply_to and correlation_id fields."""
        msg = append_room_message(
            self.shared_dir, self.room_id, "agent1",
            "hello world",
            reply_to="turn_test_abc123",
            correlation_id="corr_test_def456",
        )

        # Verify return value
        self.assertEqual(msg.get("reply_to"), "turn_test_abc123")
        self.assertEqual(msg.get("correlation_id"), "corr_test_def456")
        self.assertEqual(msg.get("from"), "agent1")
        self.assertIn("id", msg)

        # Read back from file
        active_path = room_active_file(self.shared_dir, self.room_id)
        messages = parse_jsonl(active_path)
        self.assertEqual(len(messages), 1, "Expected exactly 1 message in active.jsonl")

        record = messages[0]
        self.assertEqual(record["reply_to"], "turn_test_abc123",
                         f"reply_to mismatch: {record.get('reply_to')}")
        self.assertEqual(record["correlation_id"], "corr_test_def456",
                         f"correlation_id mismatch: {record.get('correlation_id')}")
        self.assertEqual(record["msg"], "hello world")
        self.assertEqual(record["from"], "agent1")

    def test_append_without_optional_fields(self):
        """append_room_message without reply_to/correlation_id should not have them."""
        msg = append_room_message(
            self.shared_dir, self.room_id, "agent2",
            "simple message",
        )

        self.assertNotIn("reply_to", msg)
        self.assertNotIn("correlation_id", msg)

        active_path = room_active_file(self.shared_dir, self.room_id)
        messages = parse_jsonl(active_path)
        self.assertEqual(len(messages), 1)
        record = messages[0]
        self.assertNotIn("reply_to", record)
        self.assertNotIn("correlation_id", record)

    def test_append_with_kind_and_meta(self):
        """append_room_message with kind and meta preserves all fields."""
        msg = append_room_message(
            self.shared_dir, self.room_id, "agent3",
            "test with kind",
            kind="agent",
            meta={"source": "test", "version": 1},
            reply_to="turn_999",
            correlation_id="corr_888",
        )

        self.assertEqual(msg["kind"], "agent")
        self.assertEqual(msg["meta"], {"source": "test", "version": 1})
        self.assertEqual(msg["reply_to"], "turn_999")
        self.assertEqual(msg["correlation_id"], "corr_888")

        active_path = room_active_file(self.shared_dir, self.room_id)
        messages = parse_jsonl(active_path)
        record = messages[0]
        self.assertEqual(record["kind"], "agent")
        self.assertEqual(record["meta"], {"source": "test", "version": 1})
        self.assertEqual(record["reply_to"], "turn_999")
        self.assertEqual(record["correlation_id"], "corr_888")


# ══════════════════════════════════════════════════════════
# Test 6: send_json with custom status parameter
# ══════════════════════════════════════════════════════════

class TestSendJsonStatus(unittest.TestCase):
    """Verify BridgeHandler.send_json(data, status=403) returns HTTP 403."""

    @classmethod
    def setUpClass(cls):
        """Start a test HTTP server with a handler that calls send_json."""
        import http.server
        from server import BridgeHandler

        class TestStatusHandler(BridgeHandler):
            """Handler that exposes send_json with custom status codes."""

            def do_GET(self):
                if self.path == "/test-403":
                    self.send_json({"error": "forbidden", "detail": "access denied"}, status=403)
                elif self.path == "/test-201":
                    self.send_json({"created": True, "id": "abc123"}, status=201)
                elif self.path == "/test-500":
                    self.send_json({"error": "internal", "reason": "test failure"}, status=500)
                else:
                    self.send_json({"default": True})

            def log_message(self, fmt, *args):
                pass  # Suppress log output during tests

        cls.server = http.server.HTTPServer(("127.0.0.1", 0), TestStatusHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.server_thread.start()
        # Small delay to ensure server is ready
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _make_request(self, path, expect_status=None):
        """Make an HTTP request to the test server. Returns (status, body)."""
        import urllib.request
        import urllib.error

        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if expect_status and resp.status != expect_status:
                    return resp.status, resp.read().decode("utf-8")
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_send_json_403(self):
        """send_json(data, status=403) should return HTTP 403."""
        status, body = self._make_request("/test-403")
        self.assertEqual(status, 403,
                         f"Expected HTTP 403, got {status}. Body: {body}")
        data = json.loads(body)
        self.assertEqual(data["error"], "forbidden")
        self.assertEqual(data["detail"], "access denied")

    def test_send_json_201(self):
        """send_json(data, status=201) should return HTTP 201."""
        status, body = self._make_request("/test-201")
        self.assertEqual(status, 201,
                         f"Expected HTTP 201, got {status}. Body: {body}")
        data = json.loads(body)
        self.assertTrue(data["created"])
        self.assertEqual(data["id"], "abc123")

    def test_send_json_500(self):
        """send_json(data, status=500) should return HTTP 500."""
        status, body = self._make_request("/test-500")
        self.assertEqual(status, 500,
                         f"Expected HTTP 500, got {status}. Body: {body}")
        data = json.loads(body)
        self.assertEqual(data["error"], "internal")
        self.assertEqual(data["reason"], "test failure")

    def test_send_json_default_200(self):
        """send_json(data) without explicit status should default to 200."""
        status, body = self._make_request("/")
        self.assertEqual(status, 200,
                         f"Expected HTTP 200, got {status}. Body: {body}")
        data = json.loads(body)
        self.assertTrue(data["default"])


# ══════════════════════════════════════════════════════════
# Test 7: V2-only runtime
# ══════════════════════════════════════════════════════════

class TestV2RuntimeOnly(unittest.TestCase):
    """Verify V2 mode uses runtime.run_room_step."""

    def test_run_room_step_advances_turn(self):
        """run_room_step should attempt delivery to first agent."""
        import tempfile, shutil
        from pathlib import Path
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

        tmpdir = tempfile.mkdtemp()
        try:
            shared = Path(tmpdir)
            from rooms import ensure_room, normalize_room, room_active_file
            from storage import append_jsonl

            room_id = "v2test"
            room_cfg = normalize_room({
                "id": room_id, "agents": ["agent-a", "agent-b"],
                "order": ["agent-a", "agent-b"],
            })
            ensure_room(shared, room_cfg)

            # Set room to running state
            from rooms import write_room_state
            from protocol import ROOM_RUNNING
            state = {"status": ROOM_RUNNING, "turn_index": 0, "round": 0,
                     "turn_count": 0, "max_turns": 50, "order": ["agent-a", "agent-b"],
                     "current_turn": None, "last_message_id": "", "last_error": "",
                     "waiting_for": "", "waiting_line": 0}
            write_room_state(shared, room_id, state)

            agents_cfg = {
                "agent-a": {"adapter": {"type": "cli", "config": {"command": "echo sync reply"}}},
                "agent-b": {"adapter": {"type": "manual"}},
            }

            active = room_active_file(shared, room_id)
            append_jsonl(active, {
                "id": "msg_test", "ts": "2026-05-23 12:00:00", "room": room_id,
                "from": "user", "msg": "hello agent-a", "kind": "user",
            })

            config = {
                "shared_dir": str(shared),
                "agents": agents_cfg,
                "rooms": {room_id: {**room_cfg}},
                "server": {"host": "127.0.0.1", "port": 7899},
            }

            from runtime import run_room_step
            result = run_room_step(config, room_id)
            self.assertEqual(result.get("to_agent"), "agent-a",
                             f"Expected to_agent=agent-a, got: {result}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestOpenClawDiscovery(unittest.TestCase):
    """Verify OpenClaw discovery generates openclaw_sessions adapter."""

    def test_discovery_generates_openclaw_sessions(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))

        from discovery import _discovered_agent
        item = _discovered_agent(
            "openclaw", "OpenClaw", "OpenClaw",
            Path("/tmp/fake_home/.openclaw"), "test",
            adapter_override={
                "type": "openclaw_sessions",
                "config": {"url": "http://127.0.0.1:18789/tools/invoke", "sessions_key": "agent:main:main"},
                "auth": {"type": "bearer", "token_path": "/tmp/fake_home/.openclaw/openclaw.json"},
                "response": {"mode": "callback", "timeout_seconds": 180},
            },
        )
        self.assertEqual(item["adapter"]["type"], "openclaw_sessions")
        self.assertIn("response", item["adapter"])
        self.assertEqual(item["adapter"]["response"]["mode"], "callback")


class TestOpenClawMessageCallbackInstructions(unittest.TestCase):
    """Verify args.message contains full callback instructions."""

    def test_message_contains_callback_block(self):
        import sys, json
        from pathlib import Path
        from unittest import mock
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

        from adapters.openclaw_sessions import OpenClawSessionsAdapter
        adapter = OpenClawSessionsAdapter()
        delivery_request = {
            "room_id": "test-room", "agent_id": "openclaw",
            "turn_id": "turn_abc", "correlation_id": "corr_xyz",
            "message": "User said: hello", "from": "user",
            "callback_url": "http://127.0.0.1:7899/api/rooms/test-room/agents/openclaw/callback",
            "adapter": {
                "type": "openclaw_sessions",
                "config": {"url": "http://localhost:18789/tools/invoke", "sessions_key": "agent:main:main"},
            },
        }

        with mock.patch("urllib.request.urlopen") as m_urlopen:
            m_resp = mock.MagicMock()
            m_resp.status = 200
            m_resp.__enter__ = mock.MagicMock(return_value=m_resp)
            m_resp.__exit__ = mock.MagicMock(return_value=False)
            m_resp.read.return_value = b'{"status":"ok"}'
            m_urlopen.return_value = m_resp
            ticket = adapter.wake(delivery_request)

        self.assertTrue(ticket["ok"])
        req = m_urlopen.call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        msg = payload["args"]["message"]

        self.assertIn("User said: hello", msg)
        self.assertIn("room_id=test-room", msg)
        self.assertIn("turn_id=turn_abc", msg)
        self.assertIn("correlation_id=corr_xyz", msg)
        self.assertIn("callback_url=http://127.0.0.1:7899/api/rooms/test-room/agents/openclaw/callback", msg)
        self.assertIn("agent_bridge.reply_turn", msg)
        self.assertIn("POST", msg)


if __name__ == "__main__":
    unittest.main()
