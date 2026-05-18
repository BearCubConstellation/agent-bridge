#!/usr/bin/env python3
"""归档逻辑的单元测试。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from poll import should_archive, do_archive, parse_jsonl


class TestShouldArchive(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.active = Path(self.tmpdir) / "active.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_msgs(self, count, ts=None):
        """写 count 条消息到 active.jsonl。"""
        if ts is None:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        for i in range(count):
            lines.append(json.dumps({"ts": ts, "from": "alice", "msg": f"msg {i}"}))
        self.active.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_empty_file_returns_false(self):
        self.active.write_text("", encoding="utf-8")
        self.assertFalse(should_archive(self.active))

    def test_missing_file_returns_false(self):
        self.assertFalse(should_archive(Path(self.tmpdir) / "nonexistent.jsonl"))

    def test_under_limit_returns_false(self):
        self._write_msgs(10)
        self.assertFalse(should_archive(self.active))

    def test_at_limit_returns_true(self):
        self._write_msgs(60)
        self.assertTrue(should_archive(self.active))

    def test_over_limit_returns_true(self):
        self._write_msgs(100)
        self.assertTrue(should_archive(self.active))

    def test_idle_over_30min_returns_true(self):
        old_ts = (datetime.now() - timedelta(minutes=31)).strftime("%Y-%m-%d %H:%M:%S")
        self._write_msgs(5, ts=old_ts)
        self.assertTrue(should_archive(self.active))

    def test_recent_msgs_under_limit_returns_false(self):
        self._write_msgs(5)
        self.assertFalse(should_archive(self.active))

    def test_no_ts_field_returns_false(self):
        lines = [json.dumps({"from": "alice", "msg": f"msg {i}"}) for i in range(5)]
        self.active.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertFalse(should_archive(self.active))


class TestDoArchive(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.active = Path(self.tmpdir) / "active.jsonl"
        # 写 60 条消息
        lines = [json.dumps({"ts": "2025-05-15 12:00:00", "from": "a", "msg": f"m{i}"})
                 for i in range(60)]
        self.active.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_archive_moves_file_to_history(self):
        result = do_archive(Path(self.tmpdir))
        self.assertIsNotNone(result)
        self.assertTrue(result.endswith(".jsonl"))
        history_dir = Path(self.tmpdir) / "history"
        self.assertTrue(history_dir.exists())
        archived_files = list(history_dir.glob("*.jsonl"))
        self.assertEqual(len(archived_files), 1)

    def test_archive_recreates_empty_active(self):
        do_archive(Path(self.tmpdir))
        self.assertTrue(self.active.exists())
        self.assertEqual(self.active.read_text(encoding="utf-8"), "")

    def test_archive_returns_name(self):
        result = do_archive(Path(self.tmpdir))
        self.assertIsNotNone(result)
        self.assertRegex(result, r"\d{4}-\d{2}-\d{2}_\d{6}_\d{6}\.jsonl")

    def test_archive_no_active_file(self):
        os.remove(self.active)
        result = do_archive(Path(self.tmpdir))
        self.assertIsNone(result)

    def test_archive_preserves_content(self):
        result = do_archive(Path(self.tmpdir))
        history_dir = Path(self.tmpdir) / "history"
        archived = history_dir / result
        msgs = parse_jsonl(archived)
        self.assertEqual(len(msgs), 60)


class TestParseJsonl(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_valid_file(self):
        f = Path(self.tmpdir) / "test.jsonl"
        lines = [
            json.dumps({"ts": "2025-01-01 00:00:00", "from": "a", "msg": "hello"}),
            json.dumps({"ts": "2025-01-01 00:00:01", "from": "b", "msg": "world"}),
        ]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = parse_jsonl(f)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["msg"], "hello")
        self.assertEqual(result[1]["from"], "b")

    def test_parse_empty_file(self):
        f = Path(self.tmpdir) / "test.jsonl"
        f.write_text("", encoding="utf-8")
        result = parse_jsonl(f)
        self.assertEqual(result, [])

    def test_parse_nonexistent_file(self):
        result = parse_jsonl(Path(self.tmpdir) / "nope.jsonl")
        self.assertEqual(result, [])

    def test_parse_skips_blank_lines(self):
        f = Path(self.tmpdir) / "test.jsonl"
        f.write_text('\n{"a":1}\n\n{"b":2}\n', encoding="utf-8")
        result = parse_jsonl(f)
        self.assertEqual(len(result), 2)

    def test_parse_none_path(self):
        result = parse_jsonl(None)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
