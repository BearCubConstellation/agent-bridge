#!/usr/bin/env python3
"""游标读写逻辑的单元测试。"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# 将 core/ 加入 import 路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from poll import get_cursor_file, read_cursor, write_cursor


class TestGetCursorFile(unittest.TestCase):
    def test_line_cursor_path(self):
        shared = Path("/tmp/test-shared")
        result = get_cursor_file(shared, "alice", "line")
        self.assertEqual(result, shared / ".alice_cursor")

    def test_timestamp_cursor_path(self):
        shared = Path("/tmp/test-shared")
        result = get_cursor_file(shared, "bob", "timestamp")
        self.assertEqual(result, shared / ".bob_ts_cursor")


class TestReadCursorLine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_returns_zero(self):
        cursor_file = Path(self.tmpdir) / ".alice_cursor"
        result = read_cursor(cursor_file, "line")
        self.assertEqual(result, 0)

    def test_read_valid_line_number(self):
        cursor_file = Path(self.tmpdir) / ".alice_cursor"
        cursor_file.write_text("42")
        result = read_cursor(cursor_file, "line")
        self.assertEqual(result, 42)

    def test_read_invalid_content_returns_zero(self):
        cursor_file = Path(self.tmpdir) / ".alice_cursor"
        cursor_file.write_text("not_a_number")
        result = read_cursor(cursor_file, "line")
        self.assertEqual(result, 0)

    def test_read_empty_file_returns_zero(self):
        cursor_file = Path(self.tmpdir) / ".alice_cursor"
        cursor_file.write_text("")
        result = read_cursor(cursor_file, "line")
        self.assertEqual(result, 0)


class TestReadCursorTimestamp(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_returns_none(self):
        cursor_file = Path(self.tmpdir) / ".bob_ts_cursor"
        result = read_cursor(cursor_file, "timestamp")
        self.assertIsNone(result)

    def test_read_valid_timestamp(self):
        cursor_file = Path(self.tmpdir) / ".bob_ts_cursor"
        cursor_file.write_text("2025-05-15 13:30:00")
        result = read_cursor(cursor_file, "timestamp")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2025)
        self.assertEqual(result.month, 5)
        self.assertEqual(result.hour, 13)

    def test_read_invalid_timestamp_returns_none(self):
        cursor_file = Path(self.tmpdir) / ".bob_ts_cursor"
        cursor_file.write_text("not-a-timestamp")
        result = read_cursor(cursor_file, "timestamp")
        self.assertIsNone(result)

    def test_read_empty_file_returns_none(self):
        cursor_file = Path(self.tmpdir) / ".bob_ts_cursor"
        cursor_file.write_text("")
        result = read_cursor(cursor_file, "timestamp")
        self.assertIsNone(result)


class TestWriteCursor(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_line_cursor(self):
        cursor_file = Path(self.tmpdir) / ".alice_cursor"
        write_cursor(cursor_file, "line", 42)
        self.assertEqual(cursor_file.read_text().strip(), "42")

    def test_write_timestamp_cursor(self):
        cursor_file = Path(self.tmpdir) / ".bob_ts_cursor"
        ts = datetime(2025, 5, 15, 13, 30, 0)
        write_cursor(cursor_file, "timestamp", ts)
        self.assertEqual(cursor_file.read_text().strip(), "2025-05-15 13:30:00")

    def test_write_none_timestamp(self):
        cursor_file = Path(self.tmpdir) / ".bob_ts_cursor"
        write_cursor(cursor_file, "timestamp", None)
        self.assertEqual(cursor_file.read_text().strip(), "")

    def test_roundtrip_line(self):
        cursor_file = Path(self.tmpdir) / ".alice_cursor"
        write_cursor(cursor_file, "line", 100)
        result = read_cursor(cursor_file, "line")
        self.assertEqual(result, 100)

    def test_roundtrip_timestamp(self):
        cursor_file = Path(self.tmpdir) / ".bob_ts_cursor"
        ts = datetime(2025, 12, 25, 8, 0, 0)
        write_cursor(cursor_file, "timestamp", ts)
        result = read_cursor(cursor_file, "timestamp")
        self.assertEqual(result, ts)


if __name__ == "__main__":
    unittest.main()
