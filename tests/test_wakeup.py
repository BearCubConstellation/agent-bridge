#!/usr/bin/env python3
"""webhook 唤醒和 body 构建的单元测试。"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from poll import build_body, resolve_token, wakeup_agent


class TestBuildBody(unittest.TestCase):
    def test_simple_string_substitution(self):
        template = {"message": "{{message}}"}
        result = build_body(template, "hello", "alice")
        self.assertEqual(result, {"message": "hello"})

    def test_string_with_from(self):
        template = {"message": "[{{from}}] {{message}}"}
        result = build_body(template, "hi", "bob")
        self.assertEqual(result, {"message": "[bob] hi"})

    def test_nested_dict(self):
        template = {"tool": "send", "args": {"text": "{{message}}", "from": "{{from}}"}}
        result = build_body(template, "test", "alice")
        self.assertEqual(result["tool"], "send")
        self.assertEqual(result["args"]["text"], "test")
        self.assertEqual(result["args"]["from"], "alice")

    def test_list_substitution(self):
        template = ["{{message}}", "{{from}}"]
        result = build_body(template, "hello", "bob")
        self.assertEqual(result, ["hello", "bob"])

    def test_nested_list_in_dict(self):
        template = {"messages": ["{{from}}: {{message}}"]}
        result = build_body(template, "hi", "alice")
        self.assertEqual(result["messages"], ["alice: hi"])

    def test_int_values_preserved(self):
        template = {"count": 42, "text": "{{message}}"}
        result = build_body(template, "hello", "alice")
        self.assertEqual(result["count"], 42)
        self.assertIsInstance(result["count"], int)

    def test_bool_values_preserved(self):
        template = {"enabled": True, "text": "{{message}}"}
        result = build_body(template, "hello", "alice")
        self.assertEqual(result["enabled"], True)
        self.assertIsInstance(result["enabled"], bool)

    def test_none_values_preserved(self):
        template = {"value": None, "text": "{{message}}"}
        result = build_body(template, "hello", "alice")
        self.assertIsNone(result["value"])

    def test_empty_template(self):
        result = build_body({}, "hello", "alice")
        self.assertEqual(result, {})

    def test_no_placeholders(self):
        template = {"key": "static value"}
        result = build_body(template, "hello", "alice")
        self.assertEqual(result, {"key": "static value"})

    def test_does_not_mutate_original(self):
        template = {"msg": "{{message}}"}
        original = json.dumps(template)
        build_body(template, "hello", "alice")
        self.assertEqual(json.dumps(template), original)

    def test_sessions_send_template(self):
        """模拟 OpenClaw sessions_send 真实模板。"""
        template = {
            "tool": "sessions_send",
            "args": {
                "sessionKey": "agent:main:main",
                "message": "[消息通道·{{from}}] {{message}}"
            }
        }
        result = build_body(template, "你好", "苏苏")
        self.assertEqual(result["tool"], "sessions_send")
        self.assertEqual(result["args"]["message"], "[消息通道·苏苏] 你好")
        self.assertEqual(result["args"]["sessionKey"], "agent:main:main")


class TestResolveToken(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_token_path_returns_none(self):
        self.assertIsNone(resolve_token({}))

    def test_env_token(self):
        old_value = os.environ.get("AGENT_BRIDGE_TEST_TOKEN")
        try:
            os.environ["AGENT_BRIDGE_TEST_TOKEN"] = "env-token"
            result = resolve_token({"token_env": "AGENT_BRIDGE_TEST_TOKEN"})
            self.assertEqual(result, "env-token")
        finally:
            if old_value is None:
                os.environ.pop("AGENT_BRIDGE_TEST_TOKEN", None)
            else:
                os.environ["AGENT_BRIDGE_TEST_TOKEN"] = old_value

    def test_missing_file_returns_none(self):
        self.assertIsNone(resolve_token({"token_path": "/nonexistent/file"}))

    def test_plain_text_token(self):
        token_file = Path(self.tmpdir) / "token.txt"
        token_file.write_text("my-secret-token\n", encoding="utf-8")
        result = resolve_token({"token_path": str(token_file)})
        self.assertEqual(result, "my-secret-token")

    def test_json_string_token(self):
        token_file = Path(self.tmpdir) / "token.json"
        token_file.write_text(json.dumps("json-token"), encoding="utf-8")
        result = resolve_token({"token_path": str(token_file)})
        self.assertEqual(result, "json-token")

    def test_json_object_with_jsonpath(self):
        token_file = Path(self.tmpdir) / "config.json"
        token_file.write_text(json.dumps({"api": {"key": "nested-token"}}), encoding="utf-8")
        result = resolve_token({
            "token_path": str(token_file),
            "token_jsonpath": "api.key"
        })
        self.assertEqual(result, "nested-token")

    def test_jsonpath_deep_nesting(self):
        token_file = Path(self.tmpdir) / "deep.json"
        token_file.write_text(json.dumps({"a": {"b": {"c": "deep-val"}}}), encoding="utf-8")
        result = resolve_token({
            "token_path": str(token_file),
            "token_jsonpath": "a.b.c"
        })
        self.assertEqual(result, "deep-val")

    def test_jsonpath_missing_key_returns_none(self):
        token_file = Path(self.tmpdir) / "partial.json"
        token_file.write_text(json.dumps({"a": "val"}), encoding="utf-8")
        result = resolve_token({
            "token_path": str(token_file),
            "token_jsonpath": "b.c"
        })
        self.assertIsNone(result)

    def test_tilde_expansion(self):
        old_userprofile = os.environ.get("USERPROFILE")
        old_home = os.environ.get("HOME")
        home = Path(self.tmpdir) / "home"
        home.mkdir()
        os.environ["USERPROFILE"] = str(home)
        os.environ["HOME"] = str(home)
        token_file = home / ".test_agent_bridge_token"
        try:
            token_file.write_text("home-token", encoding="utf-8")
            result = resolve_token({"token_path": "~/.test_agent_bridge_token"})
            self.assertEqual(result, "home-token")
        finally:
            token_file.unlink(missing_ok=True)
            if old_userprofile is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old_userprofile
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home


class TestWakeupAgent(unittest.TestCase):
    def test_uses_configured_http_method(self):
        import http.server
        import socket

        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                seen["method"] = self.command
                seen["body"] = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(204)
                self.end_headers()

            def log_message(self, fmt, *args):
                pass

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        try:
            ok, msg, body = wakeup_agent({
                "url": f"http://127.0.0.1:{port}/hook",
                "method": "PUT",
                "body_template": {"message": "{{message}}"},
            }, "hello", "alice")
        finally:
            thread.join(timeout=5)
            server.server_close()

        self.assertTrue(ok, msg)
        self.assertEqual(seen["method"], "PUT")
        self.assertIn(b"hello", seen["body"])


if __name__ == "__main__":
    unittest.main()
