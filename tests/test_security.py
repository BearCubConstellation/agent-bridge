#!/usr/bin/env python3
"""Tests for the supported V2 security boundary."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from security import (
    agent_in_room,
    extract_bearer_token,
    extract_token_from_request,
    resolve_token,
    sanitize_message,
    validate_agent_id,
    validate_network_exposure,
    validate_room_id,
    verify_callback_token,
    verify_mcp_token,
)


class ValidationTests(unittest.TestCase):
    def test_ids_and_message_validation(self):
        self.assertTrue(validate_room_id("room_1"))
        self.assertFalse(validate_room_id("../bad"))
        self.assertTrue(validate_agent_id("agent-a"))
        self.assertFalse(validate_agent_id(""))
        self.assertEqual("hello", sanitize_message(" hello "))
        with self.assertRaises(ValueError):
            sanitize_message("   ")

    def test_token_resolution_and_bearer_header(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("file-secret\n")
            path = handle.name
        try:
            self.assertEqual("file-secret", resolve_token(path))
        finally:
            os.unlink(path)
        self.assertEqual("abc", extract_bearer_token({"Authorization": "Bearer abc"}))


class AccessPolicyTests(unittest.TestCase):
    def test_loopback_can_omit_tokens(self):
        cfg = {"server": {"host": "127.0.0.1"}, "security": {}}
        self.assertTrue(verify_callback_token(cfg, "agent", "")[0])
        self.assertTrue(verify_mcp_token(cfg, "")[0])

    def test_nonlocal_requires_tokens(self):
        cfg = {"server": {"host": "0.0.0.0"}, "security": {}}
        self.assertFalse(verify_callback_token(cfg, "agent", "")[0])
        self.assertFalse(verify_mcp_token(cfg, "")[0])
        self.assertTrue(validate_network_exposure(cfg, "0.0.0.0"))

    def test_configured_tokens_and_header_only_extraction(self):
        cfg = {
            "server": {"host": "0.0.0.0"},
            "security": {"callback_token": "callback", "mcp_token": "mcp"},
        }
        self.assertTrue(verify_callback_token(cfg, "agent", "callback")[0])
        self.assertTrue(verify_mcp_token(cfg, "mcp")[0])
        self.assertEqual("header", extract_token_from_request({"Authorization": "Bearer header"}, {"token": "query"}))
        self.assertEqual("", extract_token_from_request({}, {"token": "query"}))
        self.assertEqual("query", extract_token_from_request({}, {"token": "query"}, allow_query=True))

    def test_room_membership(self):
        cfg = {"rooms": {"r": {"agents": ["a"], "order": ["b"]}}}
        self.assertTrue(agent_in_room(cfg, "r", "a"))
        self.assertTrue(agent_in_room(cfg, "r", "b"))
        self.assertFalse(agent_in_room(cfg, "r", "c"))


if __name__ == "__main__":
    unittest.main()
