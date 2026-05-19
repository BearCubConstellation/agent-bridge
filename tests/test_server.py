#!/usr/bin/env python3
"""server.py API 端点单元测试。

使用 http.server.HTTPServer + urllib 进行真实 HTTP 测试，
不 mock，确保端到端正确性。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch
from pathlib import Path

# 将 core/ 和 ui/ 加入 import 路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))

from server import BridgeHandler, PollManager, discover_local_agents, find_shared_dir, read_bridge


def get_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestServerBase(unittest.TestCase):
    """启动真实 HTTPServer 进行端到端测试。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-test-"))
        # 创建一个初始配置
        cfg = {
            "shared_dir": str(cls.tmpdir),
            "agent_id": "alice",
            "agents": {
                "alice": {
                    "id": "alice",
                    "display_name": "Alice",
                    "color": "#ff6b6b",
                    "cursor": "line",
                    "filter_from": "bob",
                    "wakeup": {"url": "", "method": "POST", "body_template": {"message": "{{message}}"}},
                },
                "bob": {
                    "id": "bob",
                    "display_name": "Bob",
                    "color": "#4ecdc4",
                    "cursor": "line",
                    "filter_from": "alice",
                    "wakeup": {"url": "", "method": "POST", "body_template": {"message": "{{message}}"}},
                },
            },
        }
        import yaml
        cfg_path = cls.tmpdir / "bridge.yaml"
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True)

        cls.port = get_free_port()
        BridgeHandler.shared_dir = str(cls.tmpdir)
        BridgeHandler.poll_manager = PollManager(str(cls.tmpdir), interval=9999)
        import http.server
        cls.server = http.server.HTTPServer(("127.0.0.1", cls.port), BridgeHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.2)  # 等服务器就绪
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _get(self, path):
        req = urllib.request.Request(f"{self.base}{path}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read().decode()
            finally:
                e.close()

    def _post(self, path, data):
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{self.base}{path}", data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            finally:
                e.close()

    def _put(self, path, data):
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{self.base}{path}", data=body,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            finally:
                e.close()


class TestDefaultConfig(unittest.TestCase):
    def test_read_bridge_creates_webui_editable_defaults(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-default-"))
        try:
            cfg, config_path = read_bridge(tmpdir)
            self.assertEqual(cfg["shared_dir"], str(tmpdir))
            self.assertEqual(cfg["agent_id"], "alice")
            self.assertIn("alice", cfg["agents"])
            self.assertIn("bob", cfg["agents"])
            self.assertEqual(cfg["agents"]["alice"]["filter_from"], "bob")
            self.assertEqual(cfg["agents"]["bob"]["filter_from"], "alice")
            self.assertEqual(cfg["agents"]["alice"]["wakeup"]["url"], "")
            self.assertEqual(config_path, tmpdir / "bridge.yaml")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_discover_local_agents_uses_known_local_configs(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-discover-"))
        home = tmpdir / "home"
        shared = tmpdir / "shared"
        try:
            home.mkdir()
            shared.mkdir()
            (shared / "active.jsonl").write_text(
                json.dumps({"from": "momo", "msg": "hello"}) + "\n",
                encoding="utf-8",
            )
            (home / ".hermes").mkdir()
            (home / ".hermes" / "config.yaml").write_text(
                "platforms:\n"
                "  webhook:\n"
                "    extra:\n"
                "      host: 127.0.0.1\n"
                "      port: 8644\n"
                "      routes:\n"
                "        agent-reply: {}\n",
                encoding="utf-8",
            )
            (home / ".openclaw").mkdir()
            (home / ".openclaw" / "openclaw.json").write_text("{}", encoding="utf-8")
            (home / ".codex").mkdir()

            with patch("pathlib.Path.home", return_value=home):
                agents = discover_local_agents(shared)

            ids = {a["id"] for a in agents}
            self.assertIn("momo", ids)
            self.assertIn("hermes", ids)
            self.assertIn("openclaw", ids)
            self.assertIn("codex", ids)
            hermes = next(a for a in agents if a["id"] == "hermes")
            self.assertEqual(hermes["wakeup"]["url"], "http://127.0.0.1:8644/webhooks/agent-reply")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestServeStatic(TestServerBase):
    """静态文件服务 + 路径遍历防护。"""

    def test_serve_index_html(self):
        """GET / 应返回 200 + HTML。"""
        req = urllib.request.Request(f"{self.base}/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            content_type = resp.headers.get("Content-Type", "")
            self.assertIn("text/html", content_type)

    def test_path_traversal_blocked(self):
        """GET /../../../etc/passwd 应被拒绝。"""
        try:
            req = urllib.request.Request(f"{self.base}/../../../etc/passwd")
            with urllib.request.urlopen(req, timeout=5) as resp:
                # 如果返回 200 说明漏洞存在
                self.fail(f"Path traversal not blocked! Status={resp.status}")
        except urllib.error.HTTPError as e:
            try:
                self.assertIn(e.code, (403, 404))
            finally:
                e.close()

    def test_nonexistent_static(self):
        """GET /nonexistent.css 应返回 404。"""
        try:
            req = urllib.request.Request(f"{self.base}/nonexistent.css")
            urllib.request.urlopen(req, timeout=5)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            try:
                self.assertEqual(e.code, 404)
            finally:
                e.close()


class TestConfigAPI(TestServerBase):
    """配置 API 端点。"""

    def test_get_config(self):
        """GET /api/config 返回配置。"""
        status, data = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("agents", data)
        self.assertEqual(len(data["agents"]), 2)

    def test_discover_agents_endpoint(self):
        """GET /api/agents/discover 返回本机发现的 Agent。"""
        status, data = self._get("/api/agents/discover")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("agents", data)
        ids = {a["id"] for a in data["agents"]}
        self.assertIn("alice", ids)
        self.assertIn("bob", ids)
        for a in data["agents"]:
            if a["id"] in ("alice", "bob"):
                self.assertTrue(a["configured"])

    def test_update_config_full(self):
        """PUT /api/config/full 保存完整配置。"""
        new_config = {
            "shared_dir": str(self.tmpdir),
            "agent_id": "alice",
            "agents": [
                {
                    "id": "alice",
                    "display_name": "Alice Updated",
                    "color": "#ff0000",
                    "cursor": "line",
                    "filter_from": "bob",
                    "wakeup": {"url": "", "method": "POST", "body_template": {"message": "{{message}}"}},
                },
            ],
        }
        status, data = self._put("/api/config/full", new_config)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        # 验证保存成功
        status2, data2 = self._get("/api/config")
        self.assertTrue(data2["ok"])
        self.assertEqual(data2["agents"][0]["display_name"], "Alice Updated")
        self.assertEqual(data2["agents"][0]["color"], "#ff0000")

    def test_update_config_full_empty_agents(self):
        """PUT /api/config/full 传入空 agents 列表清空配置。"""
        new_config = {
            "shared_dir": str(self.tmpdir),
            "agents": [],
        }
        status, data = self._put("/api/config/full", new_config)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["saved_agents"], [])

    def test_update_config_full_invalid_id(self):
        """PUT /api/config/full 拒绝非法 ID。"""
        new_config = {
            "agents": [{"id": "../../evil", "display_name": "evil"}],
        }
        status, data = self._put("/api/config/full", new_config)
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("invalid ID", data["error"])

    def test_update_config_empty_body(self):
        """POST /api/config 空 body 返回错误。"""
        req = urllib.request.Request(
            f"{self.base}/api/config", data=b"",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
                self.assertFalse(data["ok"])
            finally:
                e.close()


class TestMessageAPI(TestServerBase):
    """消息 API 端点。"""

    def test_send_message(self):
        """POST /api/send 发送消息。"""
        # 先恢复有效配置
        self._put("/api/config/full", {
            "shared_dir": str(self.tmpdir),
            "agent_id": "alice",
            "agents": [
                {
                    "id": "alice",
                    "display_name": "Alice",
                    "color": "#ff6b6b",
                    "cursor": "line",
                    "filter_from": "bob",
                    "wakeup": {"url": "", "method": "POST", "body_template": {"message": "{{message}}"}},
                },
                {
                    "id": "bob",
                    "display_name": "Bob",
                    "color": "#4ecdc4",
                    "cursor": "line",
                    "filter_from": "alice",
                    "wakeup": {"url": "", "method": "POST", "body_template": {"message": "{{message}}"}},
                },
            ],
        })

        status, data = self._post("/api/send", {
            "agent_id": "alice",
            "text": "Hello from test!",
        })
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["agent_id"], "alice")
        self.assertEqual(data["chars"], 16)

    def test_send_message_unknown_agent(self):
        """POST /api/send 拒绝未知 agent_id。"""
        status, data = self._post("/api/send", {
            "agent_id": "eve",
            "text": "Hack!",
        })
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("unknown", data["error"])

    def test_send_message_empty_body(self):
        """POST /api/send 空 body 返回错误。"""
        status, data = self._post("/api/send", {})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])

    def test_get_messages(self):
        """GET /api/messages 返回消息列表。"""
        status, data = self._get("/api/messages")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIsInstance(data.get("messages", []), list)


class TestArchiveAPI(TestServerBase):
    """归档 API 端点。"""

    def test_archive_no_active(self):
        """POST /api/archive 无 active.jsonl 时报错。"""
        # 清空 active.jsonl
        active = self.tmpdir / "active.jsonl"
        if active.exists():
            active.unlink()
        status, data = self._post("/api/archive", {})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])


class TestPollAPI(TestServerBase):
    """轮询 API 端点。"""

    def test_poll_status(self):
        """GET /api/poll 返回状态。"""
        status, data = self._get("/api/poll")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("running", data)

    def test_poll_now(self):
        """POST /api/poll/now 立即轮询。"""
        status, data = self._post("/api/poll/now", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("result", data)

    def test_status_endpoint(self):
        """GET /api/status 返回状态。"""
        status, data = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])


class TestHistoryAPI(TestServerBase):
    """历史记录 API 端点。"""

    def test_history_nonexistent_file(self):
        """GET /api/history/nonexistent.jsonl 返回 404。"""
        try:
            req = urllib.request.Request(f"{self.base}/api/history/nonexistent.jsonl")
            urllib.request.urlopen(req, timeout=5)
            self.fail("Expected 404")
        except urllib.error.HTTPError as e:
            try:
                self.assertEqual(e.code, 404)
            finally:
                e.close()

    def test_history_rejects_non_jsonl(self):
        """GET /api/history/../../etc/passwd 应被拒绝。"""
        try:
            req = urllib.request.Request(
                f"{self.base}/api/history/../../../etc/passwd"
            )
            urllib.request.urlopen(req, timeout=5)
            self.fail("Expected error")
        except urllib.error.HTTPError as e:
            try:
                self.assertIn(e.code, (400, 403, 404))
            finally:
                e.close()

    def test_history_valid_archive(self):
        """GET /api/history/<file>.jsonl 返回消息。"""
        # 创建一个历史文件
        history_dir = self.tmpdir / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        archive = history_dir / "2026-01-01_1200.jsonl"
        msgs = [
            {"ts": "2026-01-01 12:00:00", "from": "alice", "msg": "test"},
            {"ts": "2026-01-01 12:00:01", "from": "bob", "msg": "reply"},
        ]
        with open(archive, "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

        status, data = self._get("/api/history/2026-01-01_1200.jsonl")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
