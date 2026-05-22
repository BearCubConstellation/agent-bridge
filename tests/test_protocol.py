#!/usr/bin/env python3
"""Tests for core/protocol.py — v2 data structures and ID generation."""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from protocol import (
    gen_message_id,
    gen_event_id,
    gen_turn_id,
    gen_delivery_id,
    gen_correlation_id,
    make_message,
    make_event,
    make_turn,
    make_delivery_request,
    make_delivery_ticket,
    make_capability,
    normalize_message,
    default_room_state,
    migrate_room_state,
    validate_id,
    MSG_KIND_AGENT,
    MSG_KIND_USER,
    TURN_IDLE,
    ROOM_PAUSED,
    TURN_WAITING_RESPONSE,
    RESPONSE_CALLBACK,
)


class TestIdGeneration(unittest.TestCase):
    """Test all gen_* functions produce correct prefixes."""

    def test_gen_message_id_prefix(self):
        mid = gen_message_id()
        self.assertTrue(mid.startswith("msg_"), f"Expected 'msg_' prefix, got: {mid}")

    def test_gen_event_id_prefix(self):
        eid = gen_event_id()
        self.assertTrue(eid.startswith("evt_"), f"Expected 'evt_' prefix, got: {eid}")

    def test_gen_turn_id_prefix(self):
        tid = gen_turn_id()
        self.assertTrue(tid.startswith("turn_"), f"Expected 'turn_' prefix, got: {tid}")

    def test_gen_delivery_id_prefix(self):
        did = gen_delivery_id()
        self.assertTrue(did.startswith("deliv_"), f"Expected 'deliv_' prefix, got: {did}")

    def test_gen_correlation_id_prefix(self):
        cid = gen_correlation_id()
        self.assertTrue(cid.startswith("corr_"), f"Expected 'corr_' prefix, got: {cid}")

    def test_ids_are_unique(self):
        """Generated IDs should be unique even when called rapidly."""
        ids = [gen_message_id() for _ in range(100)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_ids_are_strings(self):
        self.assertIsInstance(gen_message_id(), str)
        self.assertIsInstance(gen_event_id(), str)
        self.assertIsInstance(gen_turn_id(), str)
        self.assertIsInstance(gen_delivery_id(), str)
        self.assertIsInstance(gen_correlation_id(), str)


class TestValidateId(unittest.TestCase):
    """Test validate_id function."""

    def test_valid_simple(self):
        self.assertTrue(validate_id("alice"))

    def test_valid_hyphen(self):
        self.assertTrue(validate_id("my-agent"))

    def test_valid_underscore(self):
        self.assertTrue(validate_id("my_agent"))

    def test_valid_numbers(self):
        self.assertTrue(validate_id("agent123"))

    def test_valid_mixed(self):
        self.assertTrue(validate_id("my-agent_42"))

    def test_invalid_empty_string(self):
        self.assertFalse(validate_id(""))

    def test_invalid_none(self):
        self.assertFalse(validate_id(None))

    def test_invalid_spaces(self):
        self.assertFalse(validate_id("my agent"))

    def test_invalid_special_chars(self):
        self.assertFalse(validate_id("agent@bob"))

    def test_invalid_dot(self):
        self.assertFalse(validate_id("agent.bob"))

    def test_invalid_slash(self):
        self.assertFalse(validate_id("agent/bob"))

    def test_valid_uppercase(self):
        self.assertTrue(validate_id("AGENT_BOB"))


class TestMakeMessage(unittest.TestCase):
    """Test make_message creates correct dict structure."""

    def test_basic_message(self):
        msg = make_message(room="room1", from_agent="alice", msg="hello")
        self.assertEqual(msg["room"], "room1")
        self.assertEqual(msg["from"], "alice")
        self.assertEqual(msg["msg"], "hello")
        self.assertEqual(msg["kind"], MSG_KIND_AGENT)
        self.assertTrue(msg["id"].startswith("msg_"))
        self.assertIn("ts", msg)

    def test_message_with_to(self):
        msg = make_message(room="room1", from_agent="alice", msg="x", to_agent="bob")
        self.assertEqual(msg["to"], "bob")

    def test_message_with_reply_to(self):
        msg = make_message(room="room1", from_agent="alice", msg="x", reply_to="msg_123")
        self.assertEqual(msg["reply_to"], "msg_123")

    def test_message_with_correlation_id(self):
        msg = make_message(room="room1", from_agent="alice", msg="x", correlation_id="corr_1")
        self.assertEqual(msg["correlation_id"], "corr_1")

    def test_message_with_meta(self):
        meta = {"priority": "high"}
        msg = make_message(room="room1", from_agent="alice", msg="x", meta=meta)
        self.assertEqual(msg["meta"], meta)

    def test_message_with_user_kind(self):
        msg = make_message(room="room1", from_agent="alice", msg="x", kind=MSG_KIND_USER)
        self.assertEqual(msg["kind"], MSG_KIND_USER)

    def test_optional_fields_absent_when_not_provided(self):
        msg = make_message(room="room1", from_agent="alice", msg="x")
        self.assertNotIn("to", msg)
        self.assertNotIn("reply_to", msg)
        self.assertNotIn("correlation_id", msg)
        self.assertNotIn("meta", msg)


class TestMakeEvent(unittest.TestCase):
    """Test make_event creates correct dict structure."""

    def test_basic_event(self):
        evt = make_event(room="room1", event_type="room.started")
        self.assertEqual(evt["room"], "room1")
        self.assertEqual(evt["type"], "room.started")
        self.assertTrue(evt["id"].startswith("evt_"))
        self.assertIn("ts", evt)
        self.assertEqual(evt["actor"], "")
        self.assertEqual(evt["turn_id"], "")
        self.assertEqual(evt["correlation_id"], "")
        self.assertEqual(evt["message_id"], "")
        self.assertEqual(evt["meta"], {})

    def test_event_with_actor(self):
        evt = make_event(room="room1", event_type="agent.wakeup.succeeded", actor="alice")
        self.assertEqual(evt["actor"], "alice")

    def test_event_with_turn_id(self):
        evt = make_event(room="room1", event_type="turn.completed", turn_id="turn_123")
        self.assertEqual(evt["turn_id"], "turn_123")

    def test_event_with_correlation_id(self):
        evt = make_event(room="room1", event_type="turn.completed", correlation_id="corr_1")
        self.assertEqual(evt["correlation_id"], "corr_1")

    def test_event_with_message_id(self):
        evt = make_event(room="room1", event_type="message.created", message_id="msg_1")
        self.assertEqual(evt["message_id"], "msg_1")

    def test_event_with_meta(self):
        meta = {"retry_count": 3}
        evt = make_event(room="room1", event_type="agent.wakeup.failed", meta=meta)
        self.assertEqual(evt["meta"], meta)


class TestMakeTurn(unittest.TestCase):
    """Test make_turn with timeout calculation."""

    def test_basic_turn(self):
        turn = make_turn(room_id="room1", agent_id="alice", input_messages=[])
        self.assertTrue(turn["turn_id"].startswith("turn_"))
        self.assertEqual(turn["agent_id"], "alice")
        self.assertEqual(turn["state"], TURN_IDLE)
        self.assertTrue(turn["delivery_id"].startswith("deliv_"))
        self.assertTrue(turn["correlation_id"].startswith("corr_"))
        self.assertIn("started_at", turn)
        self.assertIn("timeout_at", turn)
        self.assertEqual(turn["timeout_seconds"], 180)
        self.assertEqual(turn["attempts"], 1)
        self.assertEqual(turn["max_attempts"], 2)

    def test_turn_custom_timeout(self):
        turn = make_turn(room_id="room1", agent_id="alice", input_messages=[], timeout_seconds=30)
        self.assertEqual(turn["timeout_seconds"], 30)

    def test_turn_input_message_ids(self):
        msgs = [
            {"id": "msg_1", "from": "bob", "msg": "hi"},
            {"id": "msg_2", "from": "carol", "msg": "hey"},
        ]
        turn = make_turn(room_id="room1", agent_id="alice", input_messages=msgs)
        self.assertEqual(turn["input_message_ids"], ["msg_1", "msg_2"])

    def test_turn_messages_without_ids(self):
        msgs = [{"from": "bob", "msg": "hi"}]
        turn = make_turn(room_id="room1", agent_id="alice", input_messages=msgs)
        self.assertEqual(turn["input_message_ids"], [""])

    def test_turn_timeout_at_is_future(self):
        """timeout_at should be approximately now + timeout_seconds."""
        import time as _time
        before = _time.time()
        turn = make_turn(room_id="room1", agent_id="alice", input_messages=[], timeout_seconds=300)
        after = _time.time()
        from datetime import datetime as _dt
        timeout_dt = _dt.strptime(turn["timeout_at"], "%Y-%m-%d %H:%M:%S")
        timeout_epoch = timeout_dt.timestamp()
        # Should be roughly between before + 300 and after + 300
        self.assertGreaterEqual(timeout_epoch, before + 295)
        self.assertLessEqual(timeout_epoch, after + 305)


class TestDeliveryRequest(unittest.TestCase):
    """Test make_delivery_request and make_delivery_ticket."""

    def test_make_delivery_request(self):
        turn = make_turn(room_id="room1", agent_id="alice", input_messages=[])
        req = make_delivery_request(
            room_id="room1",
            agent_id="alice",
            turn=turn,
            message_text="hello bob",
            from_agents=["bob"],
            callback_url="http://localhost/callback",
            room_path="/tmp/rooms/room1",
            active_file="/tmp/active.jsonl",
        )
        self.assertEqual(req["room_id"], "room1")
        self.assertEqual(req["agent_id"], "alice")
        self.assertEqual(req["turn_id"], turn["turn_id"])
        self.assertEqual(req["correlation_id"], turn["correlation_id"])
        self.assertEqual(req["message"], "hello bob")
        self.assertEqual(req["from"], ["bob"])
        self.assertEqual(req["callback_url"], "http://localhost/callback")
        self.assertEqual(req["room_path"], "/tmp/rooms/room1")
        self.assertEqual(req["active_file"], "/tmp/active.jsonl")
        self.assertEqual(req["input_messages"], [])

    def test_make_delivery_request_with_input_messages(self):
        turn = make_turn(room_id="room1", agent_id="alice", input_messages=[])
        msgs = [{"id": "msg_1", "from": "bob", "msg": "hello"}]
        req = make_delivery_request(
            room_id="room1",
            agent_id="alice",
            turn=turn,
            message_text="hello",
            from_agents=["bob"],
            callback_url="http://localhost/callback",
            room_path="/tmp/rooms/room1",
            active_file="/tmp/active.jsonl",
            input_messages=msgs,
        )
        self.assertEqual(req["input_messages"], msgs)

    def test_make_delivery_ticket_ok(self):
        turn = make_turn(room_id="room1", agent_id="alice", input_messages=[])
        req = make_delivery_request(
            room_id="room1", agent_id="alice", turn=turn,
            message_text="hello", from_agents=["bob"],
            callback_url="", room_path="", active_file="",
        )
        ticket = make_delivery_ticket(ok=True, delivery_request=req,
                                       adapter_type="cli", detail="success")
        self.assertTrue(ticket["ok"])
        self.assertEqual(ticket["turn_id"], turn["turn_id"])
        self.assertEqual(ticket["agent_id"], "alice")
        self.assertEqual(ticket["adapter_type"], "cli")
        self.assertEqual(ticket["response_mode"], RESPONSE_CALLBACK)
        self.assertEqual(ticket["detail"], "success")
        self.assertEqual(ticket["error"], "")

    def test_make_delivery_ticket_fail(self):
        ticket = make_delivery_ticket(ok=False, adapter_type="manual",
                                       error="cannot auto-trigger")
        self.assertFalse(ticket["ok"])
        self.assertEqual(ticket["adapter_type"], "manual")
        self.assertEqual(ticket["error"], "cannot auto-trigger")

    def test_make_delivery_ticket_with_sync_response(self):
        ticket = make_delivery_ticket(ok=True, sync_response="hello back",
                                       adapter_type="cli")
        self.assertEqual(ticket["sync_response"], "hello back")

    def test_make_delivery_ticket_without_delivery_request(self):
        """Should still generate delivery_id when no request provided."""
        ticket = make_delivery_ticket(ok=True, adapter_type="manual")
        self.assertTrue(ticket["delivery_id"].startswith("deliv_"))

    def test_make_delivery_ticket_preserves_correlation_id(self):
        turn = make_turn(room_id="room1", agent_id="alice", input_messages=[])
        req = make_delivery_request(
            room_id="room1", agent_id="alice", turn=turn,
            message_text="hello", from_agents=["bob"],
            callback_url="", room_path="", active_file="",
        )
        ticket = make_delivery_ticket(ok=True, delivery_request=req, adapter_type="cli")
        self.assertEqual(ticket["correlation_id"], turn["correlation_id"])


class TestMakeCapability(unittest.TestCase):
    """Test make_capability creates correct dict structure."""

    def test_basic_capability(self):
        cap = make_capability(adapter_type="cli")
        self.assertEqual(cap["type"], "cli")
        self.assertTrue(cap["configured"])
        self.assertTrue(cap["automatic"])
        self.assertEqual(cap["wake_modes"], [])
        self.assertEqual(cap["response_modes"], [])
        self.assertFalse(cap["supports_active_push"])
        self.assertFalse(cap["supports_streaming"])
        self.assertFalse(cap["requires_callback_url"])
        self.assertEqual(cap["health"], "configured")

    def test_capability_full(self):
        cap = make_capability(
            adapter_type="native_http",
            configured=True,
            automatic=True,
            wake_modes=["http_push"],
            response_modes=["sync", "callback"],
            supports_active_push=True,
            supports_streaming=True,
            requires_callback_url=True,
            health="healthy",
        )
        self.assertEqual(cap["type"], "native_http")
        self.assertEqual(cap["wake_modes"], ["http_push"])
        self.assertEqual(cap["response_modes"], ["sync", "callback"])
        self.assertTrue(cap["supports_active_push"])
        self.assertTrue(cap["supports_streaming"])
        self.assertTrue(cap["requires_callback_url"])
        self.assertEqual(cap["health"], "healthy")


class TestNormalizeMessage(unittest.TestCase):
    """Test normalize_message adds missing fields."""

    def test_normalize_adds_defaults(self):
        raw = {"ts": "2024-01-01 00:00:00", "from": "alice", "msg": "hello"}
        norm = normalize_message(raw)
        self.assertEqual(norm["id"], "")
        self.assertEqual(norm["room"], "")
        self.assertEqual(norm["to"], "")
        self.assertEqual(norm["kind"], MSG_KIND_AGENT)
        self.assertEqual(norm["reply_to"], "")
        self.assertEqual(norm["correlation_id"], "")
        self.assertEqual(norm["meta"], {})

    def test_normalize_preserves_existing(self):
        raw = {"id": "msg_1", "ts": "x", "room": "r1", "from": "alice",
               "to": "bob", "kind": "user", "msg": "hello",
               "reply_to": "msg_0", "correlation_id": "corr_1",
               "meta": {"key": "val"}}
        norm = normalize_message(raw)
        self.assertEqual(norm["id"], "msg_1")
        self.assertEqual(norm["room"], "r1")
        self.assertEqual(norm["to"], "bob")
        self.assertEqual(norm["kind"], "user")
        self.assertEqual(norm["reply_to"], "msg_0")
        self.assertEqual(norm["correlation_id"], "corr_1")
        self.assertEqual(norm["meta"], {"key": "val"})

    def test_normalize_does_not_mutate_original(self):
        raw = {"from": "alice", "msg": "hello"}
        normalize_message(raw)
        self.assertNotIn("id", raw)
        self.assertNotIn("room", raw)

    def test_normalize_non_dict(self):
        self.assertEqual(normalize_message("string"), "string")
        self.assertEqual(normalize_message(None), None)
        self.assertEqual(normalize_message(42), 42)


class TestRoomState(unittest.TestCase):
    """Test default_room_state and migrate_room_state."""

    def test_default_room_state(self):
        state = default_room_state()
        self.assertEqual(state["status"], ROOM_PAUSED)
        self.assertEqual(state["policy"], "round_robin")
        self.assertEqual(state["turn_index"], 0)
        self.assertEqual(state["round"], 0)
        self.assertEqual(state["turn_count"], 0)
        self.assertEqual(state["max_turns"], 50)
        self.assertEqual(state["order"], [])
        self.assertIsNone(state["current_turn"])
        self.assertEqual(state["last_message_id"], "")
        self.assertEqual(state["last_error"], "")
        self.assertEqual(state["waiting_for"], "")
        self.assertEqual(state["waiting_line"], 0)

    def test_default_room_state_with_config(self):
        cfg = {"status": "running", "policy": "random", "max_turns": 10,
               "order": ["alice", "bob"]}
        state = default_room_state(cfg)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["policy"], "random")
        self.assertEqual(state["max_turns"], 10)
        self.assertEqual(state["order"], ["alice", "bob"])

    def test_migrate_room_state_adds_current_turn(self):
        old_state = {"status": "running", "waiting_for": "alice", "waiting_line": 5}
        migrated = migrate_room_state(old_state)
        self.assertEqual(migrated["status"], "running")
        self.assertIsNotNone(migrated["current_turn"])
        self.assertEqual(migrated["current_turn"]["agent_id"], "alice")
        self.assertEqual(migrated["current_turn"]["state"], TURN_WAITING_RESPONSE)
        self.assertEqual(migrated["current_turn"]["input_line_max"], 5)

    def test_migrate_room_state_preserves_existing_current_turn(self):
        existing_turn = {
            "turn_id": "turn_existing",
            "agent_id": "bob",
            "state": "delivering",
            "started_at": "2024-01-01 00:00:00",
            "timeout_at": "2024-01-01 00:03:00",
            "timeout_seconds": 180,
            "input_message_ids": ["msg_1"],
            "input_line_max": 0,
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 2,
            "last_error": "",
        }
        old_state = {"status": "running", "current_turn": existing_turn,
                     "waiting_for": "alice"}
        migrated = migrate_room_state(old_state)
        self.assertEqual(migrated["current_turn"], existing_turn)
        self.assertEqual(migrated["current_turn"]["turn_id"], "turn_existing")

    def test_migrate_room_state_merges_with_room_cfg(self):
        cfg = {"status": "running", "max_turns": 5}
        old_state = {"waiting_for": "bob"}
        migrated = migrate_room_state(old_state, room_cfg=cfg)
        self.assertEqual(migrated["status"], "running")
        self.assertEqual(migrated["max_turns"], 5)

    def test_migrate_without_waiting_for(self):
        old_state = {"status": "paused"}
        migrated = migrate_room_state(old_state)
        self.assertIsNone(migrated["current_turn"])


if __name__ == "__main__":
    unittest.main()
