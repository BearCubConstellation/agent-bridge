#!/usr/bin/env python3
"""Tests for core/security.py — tokens, IDs, sanitization, and auth."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from security import (
    validate_room_id,
    validate_agent_id,
    resolve_token,
    verify_callback_token,
    extract_bearer_token,
    extract_token_from_request,
    agent_in_room,
    sanitize_message,
)


class TestValidateRoomId(unittest.TestCase):
    """Test validate_room_id with valid and invalid patterns."""

    def test_valid_simple(self):
        self.assertTrue(validate_room_id("myroom"))

    def test_valid_hyphen(self):
        self.assertTrue(validate_room_id("my-room"))

    def test_valid_underscore(self):
        self.assertTrue(validate_room_id("my_room"))

    def test_valid_numbers(self):
        self.assertTrue(validate_room_id("room123"))

    def test_invalid_empty(self):
        self.assertFalse(validate_room_id(""))

    def test_invalid_none(self):
        self.assertFalse(validate_room_id(None))

    def test_invalid_spaces(self):
        self.assertFalse(validate_room_id("my room"))

    def test_invalid_slash(self):
        self.assertFalse(validate_room_id("room/../etc"))


class TestValidateAgentId(unittest.TestCase):
    """Test validate_agent_id with valid and invalid patterns."""

    def test_valid_simple(self):
        self.assertTrue(validate_agent_id("alice"))

    def test_valid_hyphen(self):
        self.assertTrue(validate_agent_id("my-agent"))

    def test_valid_underscore(self):
        self.assertTrue(validate_agent_id("my_agent"))

    def test_valid_mixed(self):
        self.assertTrue(validate_agent_id("my-agent_42"))

    def test_invalid_empty(self):
        self.assertFalse(validate_agent_id(""))

    def test_invalid_none(self):
        self.assertFalse(validate_agent_id(None))

    def test_invalid_spaces(self):
        self.assertFalse(validate_agent_id("my agent"))

    def test_invalid_at_symbol(self):
        self.assertFalse(validate_agent_id("agent@bob"))

    def test_invalid_dot(self):
        self.assertFalse(validate_agent_id("agent.bob"))


class TestResolveToken(unittest.TestCase):
    """Test resolve_token with plain strings, env-vars, and file paths."""

    def test_plain_string(self):
        result = resolve_token("my-token-123")
        self.assertEqual(result, "my-token-123")

    def test_empty_string(self):
        self.assertIsNone(resolve_token(""))

    def test_none(self):
        self.assertIsNone(resolve_token(None))

    def test_env_var_present(self):
        os.environ["_TEST_TOKEN_VAR"] = "secret-from-env"
        try:
            result = resolve_token("${_TEST_TOKEN_VAR}")
            self.assertEqual(result, "secret-from-env")
        finally:
            del os.environ["_TEST_TOKEN_VAR"]

    def test_env_var_missing(self):
        result = resolve_token("${_NONEXISTENT_VAR_XYZ}")
        self.assertIsNone(result)

    def test_file_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".token", delete=False) as f:
            f.write("file-secret-token\n")
            token_path = f.name
        try:
            result = resolve_token(token_path)
            self.assertEqual(result, "file-secret-token")
        finally:
            os.unlink(token_path)

    def test_file_path_nonexistent(self):
        result = resolve_token("/tmp/nonexistent-file-xyz123.token")
        # Not a file, falls through to return as plain string
        self.assertEqual(result, "/tmp/nonexistent-file-xyz123.token")

    def test_stripped_value(self):
        result = resolve_token("  token-with-spaces  ")
        self.assertEqual(result, "token-with-spaces")


class TestVerifyCallbackToken(unittest.TestCase):
    """Test verify_callback_token with various configs and tokens."""

    def test_no_security_configured(self):
        """No tokens in config → allow all (local mode)."""
        ok, err = verify_callback_token({}, "alice", "anything")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_empty_security_dict(self):
        ok, err = verify_callback_token({"security": {}}, "alice", "anything")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_global_token_valid(self):
        config = {"security": {"callback_token": "globalsecret"}}
        ok, err = verify_callback_token(config, "alice", "globalsecret")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_global_token_invalid(self):
        config = {"security": {"callback_token": "globalsecret"}}
        ok, err = verify_callback_token(config, "alice", "wrongtoken")
        self.assertFalse(ok)
        self.assertEqual(err, "invalid token")

    def test_per_agent_token_valid(self):
        config = {"security": {"callback_tokens": {"alice": "alice-secret"}}}
        ok, err = verify_callback_token(config, "alice", "alice-secret")
        self.assertTrue(ok)

    def test_per_agent_token_invalid(self):
        config = {"security": {"callback_tokens": {"alice": "alice-secret"}}}
        ok, err = verify_callback_token(config, "alice", "wrong")
        self.assertFalse(ok)
        self.assertEqual(err, "invalid token")

    def test_per_agent_priority_over_global(self):
        config = {
            "security": {
                "callback_token": "global-secret",
                "callback_tokens": {"alice": "alice-secret"},
            }
        }
        ok, _ = verify_callback_token(config, "alice", "alice-secret")
        self.assertTrue(ok)
        # Global token should NOT work for alice since per-agent is set
        ok2, _ = verify_callback_token(config, "alice", "global-secret")
        self.assertFalse(ok2)

    def test_missing_provided_token(self):
        config = {"security": {"callback_token": "globalsecret"}}
        ok, err = verify_callback_token(config, "alice", "")
        self.assertFalse(ok)
        self.assertEqual(err, "missing token")

    def test_no_token_configured_for_agent(self):
        config = {"security": {"callback_tokens": {"bob": "bob-secret"}}}
        ok, err = verify_callback_token(config, "alice", "some-token")
        self.assertFalse(ok)
        self.assertIn("no token configured", err)

    def test_env_var_in_token_config(self):
        os.environ["_TEST_CALLBACK_TOKEN"] = "env-secret"
        try:
            config = {"security": {"callback_token": "${_TEST_CALLBACK_TOKEN}"}}
            ok, err = verify_callback_token(config, "alice", "env-secret")
            self.assertTrue(ok)
        finally:
            del os.environ["_TEST_CALLBACK_TOKEN"]


class TestExtractBearerToken(unittest.TestCase):
    """Test extract_bearer_token from headers."""

    def test_extract_bearer(self):
        headers = {"Authorization": "Bearer abc123"}
        self.assertEqual(extract_bearer_token(headers), "abc123")

    def test_extract_bearer_lowercase(self):
        headers = {"authorization": "Bearer abc123"}
        self.assertEqual(extract_bearer_token(headers), "abc123")

    def test_extract_no_header(self):
        self.assertEqual(extract_bearer_token({}), "")

    def test_extract_invalid_prefix(self):
        headers = {"Authorization": "Basic abc123"}
        self.assertEqual(extract_bearer_token(headers), "")

    def test_extract_bearer_with_extra_whitespace(self):
        headers = {"Authorization": "Bearer   abc123  "}
        self.assertEqual(extract_bearer_token(headers), "abc123")


class TestExtractTokenFromRequest(unittest.TestCase):
    """Test extract_token_from_request with headers and query params."""

    def test_header_priority(self):
        headers = {"Authorization": "Bearer header-token"}
        params = {"token": "query-token"}
        self.assertEqual(extract_token_from_request(headers, params), "header-token")

    def test_fallback_to_query_param(self):
        headers = {}
        params = {"token": "query-token"}
        self.assertEqual(extract_token_from_request(headers, params), "query-token")

    def test_no_token_anywhere(self):
        self.assertEqual(extract_token_from_request({}, {}), "")

    def test_query_without_token_key(self):
        self.assertEqual(extract_token_from_request({}, {"other": "val"}), "")


class TestAgentInRoom(unittest.TestCase):
    """Test agent_in_room membership checking."""

    def setUp(self):
        self.config = {
            "rooms": {
                "room1": {
                    "agents": ["alice", "bob"],
                    "order": ["alice", "bob"],
                },
                "room2": {
                    "order": ["carol", "dave"],
                },
            }
        }

    def test_agent_in_agents_list(self):
        self.assertTrue(agent_in_room(self.config, "room1", "alice"))

    def test_agent_in_order_list(self):
        self.assertTrue(agent_in_room(self.config, "room2", "carol"))

    def test_agent_not_in_room(self):
        self.assertFalse(agent_in_room(self.config, "room1", "eve"))

    def test_room_not_in_config(self):
        self.assertFalse(agent_in_room(self.config, "nonexistent", "alice"))

    def test_empty_config(self):
        self.assertFalse(agent_in_room({}, "room1", "alice"))

    def test_agents_and_order_both_checked(self):
        config = {
            "rooms": {
                "room3": {
                    "agents": ["alice"],
                    "order": ["bob"],
                }
            }
        }
        self.assertTrue(agent_in_room(config, "room3", "alice"))
        self.assertTrue(agent_in_room(config, "room3", "bob"))
        self.assertFalse(agent_in_room(config, "room3", "carol"))


class TestSanitizeMessage(unittest.TestCase):
    """Test sanitize_message function."""

    def test_normal_message(self):
        result = sanitize_message("hello world")
        self.assertEqual(result, "hello world")

    def test_strips_whitespace(self):
        result = sanitize_message("  hello  ")
        self.assertEqual(result, "hello")

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError) as ctx:
            sanitize_message("")
        self.assertIn("non-empty string", str(ctx.exception))

    def test_whitespace_only_raises(self):
        with self.assertRaises(ValueError) as ctx:
            sanitize_message("   ")
        self.assertIn("non-empty string", str(ctx.exception))

    def test_non_string_raises(self):
        with self.assertRaises(ValueError):
            sanitize_message(None)
        with self.assertRaises(ValueError):
            sanitize_message(42)
        with self.assertRaises(ValueError):
            sanitize_message([])

    def test_too_long_raises(self):
        long_msg = "x" * 50001
        with self.assertRaises(ValueError) as ctx:
            sanitize_message(long_msg)
        self.assertIn("exceeds max length", str(ctx.exception))

    def test_custom_max_length(self):
        long_msg = "x" * 100
        with self.assertRaises(ValueError):
            sanitize_message(long_msg, max_length=50)

    def test_null_bytes_removed(self):
        result = sanitize_message("hello\x00world")
        self.assertEqual(result, "helloworld")

    def test_null_bytes_and_whitespace(self):
        result = sanitize_message("  \x00test\x00  ")
        self.assertEqual(result, "test")

    def test_unicode_preserved(self):
        result = sanitize_message("你好世界 🌍")
        self.assertEqual(result, "你好世界 🌍")


if __name__ == "__main__":
    unittest.main()
