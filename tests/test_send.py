#!/usr/bin/env python3
"""send.py 的单元测试。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from send import validate_agent_id, find_active_jsonl, send


class TestValidateAgentId(unittest.TestCase):
    def test_valid_simple(self):
        self.assertTrue(validate_agent_id("alice"))

    def test_valid_with_hyphen(self):
        self.assertTrue(validate_agent_id("my-agent"))

    def test_valid_with_underscore(self):
        self.assertTrue(validate_agent_id("my_agent"))

    def test_valid_with_numbers(self):
        self.assertTrue(validate_agent_id("agent123"))

    def test_valid_mixed(self):
        self.assertTrue(validate_agent_id("my-agent_42"))

    def test_empty_string(self):
        self.assertFalse(validate_agent_id(""))

    def test_none(self):
        self.assertFalse(validate_agent_id(None))

    def test_spaces(self):
        self.assertFalse(validate_agent_id("my agent"))

    def test_special_chars(self):
        self.assertFalse(validate_agent_id("agent@bob"))

    def test_dot(self):
        self.assertFalse(validate_agent_id("agent.bob"))

    def test_chinese(self):
        self.assertFalse(validate_agent_id("苏苏"))


class TestFindActiveJsonl(unittest.TestCase):
    def test_explicit_path(self):
        result = find_active_jsonl("/tmp/my-shared")
        self.assertEqual(result, Path("/tmp/my-shared/active.jsonl"))

    def test_tilde_expansion(self):
        result = find_active_jsonl("~/test-dir")
        expected = Path(os.path.expanduser("~/test-dir")) / "active.jsonl"
        self.assertEqual(result, expected)

    def test_empty_string_falls_back(self):
        # 空字符串时应 fallback 到默认路径之一
        result = find_active_jsonl("")
        self.assertEqual(result.name, "active.jsonl")


class TestSend(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.active_file = Path(self.tmpdir) / "active.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_send_writes_json_line(self):
        send("alice", self.active_file, "hello world")
        lines = self.active_file.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 1)
        msg = json.loads(lines[0])
        self.assertEqual(msg["from"], "alice")
        self.assertEqual(msg["msg"], "hello world")
        self.assertIn("ts", msg)

    def test_send_appends_multiple(self):
        send("alice", self.active_file, "msg1")
        send("bob", self.active_file, "msg2")
        lines = self.active_file.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["from"], "alice")
        self.assertEqual(json.loads(lines[1])["from"], "bob")

    def test_send_creates_parent_dir(self):
        nested = Path(self.tmpdir) / "deep" / "nested" / "active.jsonl"
        send("alice", nested, "test")
        self.assertTrue(nested.exists())
        msg = json.loads(nested.read_text(encoding="utf-8").strip())
        self.assertEqual(msg["msg"], "test")

    def test_send_timestamp_format(self):
        send("alice", self.active_file, "ts test")
        msg = json.loads(self.active_file.read_text(encoding="utf-8").strip())
        # 格式应为 YYYY-MM-DD HH:MM:SS
        ts = msg["ts"]
        self.assertRegex(ts, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_send_unicode(self):
        send("alice", self.active_file, "你好世界 🌍")
        msg = json.loads(self.active_file.read_text(encoding="utf-8").strip())
        self.assertEqual(msg["msg"], "你好世界 🌍")

    def test_send_invalid_agent_id_exits(self):
        from send import InvalidAgentError
        with self.assertRaises(InvalidAgentError):
            send("invalid id!", self.active_file, "test")


if __name__ == "__main__":
    unittest.main()
