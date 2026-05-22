#!/usr/bin/env python3
"""Tests for core/storage.py — JSONL and JSON file operations."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from storage import (
    append_jsonl,
    read_jsonl,
    read_jsonl_no_line,
    write_json,
    read_json,
)


class TestAppendReadJsonl(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-storage-"))
        self.jsonl_path = self.tmpdir / "test.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_append_creates_file(self):
        record = {"id": 1, "msg": "hello"}
        result = append_jsonl(self.jsonl_path, record)
        self.assertEqual(result, record)
        self.assertTrue(self.jsonl_path.exists())

    def test_append_writes_valid_json_line(self):
        record = {"id": 1, "msg": "hello"}
        append_jsonl(self.jsonl_path, record)
        lines = self.jsonl_path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed, record)

    def test_read_jsonl_returns_records(self):
        records = [
            {"id": 1, "msg": "hello"},
            {"id": 2, "msg": "world"},
        ]
        for r in records:
            append_jsonl(self.jsonl_path, r)

        results = read_jsonl(self.jsonl_path)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], 1)
        self.assertEqual(results[0]["msg"], "hello")
        self.assertEqual(results[1]["id"], 2)
        self.assertEqual(results[1]["msg"], "world")

    def test_read_jsonl_adds_line_numbers(self):
        append_jsonl(self.jsonl_path, {"id": 1})
        append_jsonl(self.jsonl_path, {"id": 2})
        results = read_jsonl(self.jsonl_path)
        self.assertEqual(results[0]["_line"], 1)
        self.assertEqual(results[1]["_line"], 2)

    def test_read_jsonl_no_line_does_not_add_line(self):
        append_jsonl(self.jsonl_path, {"id": 1})
        results = read_jsonl_no_line(self.jsonl_path)
        self.assertEqual(len(results), 1)
        self.assertNotIn("_line", results[0])

    def test_read_jsonl_missing_file_returns_empty(self):
        nonexistent = self.tmpdir / "nonexistent.jsonl"
        results = read_jsonl(nonexistent)
        self.assertEqual(results, [])

    def test_read_jsonl_no_line_missing_returns_empty(self):
        nonexistent = self.tmpdir / "nonexistent.jsonl"
        results = read_jsonl_no_line(nonexistent)
        self.assertEqual(results, [])

    def test_read_jsonl_skips_empty_lines(self):
        self.jsonl_path.write_text("\n\n\n", encoding="utf-8")
        results = read_jsonl(self.jsonl_path)
        self.assertEqual(results, [])

    def test_read_jsonl_skips_invalid_json(self):
        self.jsonl_path.write_text('{"valid": 1}\nnot json\n{"valid": 2}\n', encoding="utf-8")
        results = read_jsonl(self.jsonl_path)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["valid"], 1)
        self.assertEqual(results[1]["valid"], 2)

    def test_unicode_preservation(self):
        record = {"msg": "你好世界 🌍"}
        append_jsonl(self.jsonl_path, record)
        results = read_jsonl_no_line(self.jsonl_path)
        self.assertEqual(results[0]["msg"], "你好世界 🌍")

    def test_append_creates_parent_directories(self):
        nested = self.tmpdir / "deep" / "nested" / "data.jsonl"
        append_jsonl(nested, {"id": 1})
        self.assertTrue(nested.exists())


class TestWriteReadJson(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agent-bridge-storage-"))
        self.json_path = self.tmpdir / "state.json"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_and_read_roundtrip(self):
        data = {"status": "running", "count": 42, "items": ["a", "b"]}
        write_json(self.json_path, data)
        result = read_json(self.json_path)
        self.assertEqual(result, data)

    def test_write_creates_parent_directories(self):
        nested = self.tmpdir / "deep" / "state.json"
        write_json(nested, {"ok": True})
        self.assertTrue(nested.exists())
        result = read_json(nested)
        self.assertEqual(result, {"ok": True})

    def test_read_json_missing_file_returns_empty_dict(self):
        result = read_json(self.tmpdir / "nonexistent.json")
        self.assertEqual(result, {})

    def test_read_json_missing_file_with_fallback(self):
        result = read_json(self.tmpdir / "nonexistent.json", fallback=None)
        # When fallback is None, code returns {} (empty dict)
        self.assertEqual(result, {})

    def test_read_json_missing_file_with_custom_fallback(self):
        result = read_json(self.tmpdir / "nonexistent.json", fallback=[])
        self.assertEqual(result, [])

    def test_read_json_empty_file_returns_empty_dict(self):
        self.json_path.write_text("", encoding="utf-8")
        result = read_json(self.json_path)
        self.assertEqual(result, {})

    def test_read_json_invalid_content_returns_empty_dict(self):
        self.json_path.write_text("not valid json {{{", encoding="utf-8")
        result = read_json(self.json_path)
        self.assertEqual(result, {})

    def test_read_json_invalid_with_fallback(self):
        self.json_path.write_text("invalid", encoding="utf-8")
        result = read_json(self.json_path, fallback={"error": True})
        self.assertEqual(result, {"error": True})

    def test_write_json_pretty_printed(self):
        write_json(self.json_path, {"a": 1})
        content = self.json_path.read_text(encoding="utf-8")
        self.assertIn("\n", content)
        self.assertIn("  ", content)

    def test_write_overwrite(self):
        write_json(self.json_path, {"a": 1})
        write_json(self.json_path, {"b": 2})
        result = read_json(self.json_path)
        self.assertEqual(result, {"b": 2})


if __name__ == "__main__":
    unittest.main()
