#!/usr/bin/env python3
"""run_poll reliability regression tests."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from poll import run_poll


class TestRunPollReliability(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.active = self.tmpdir / "active.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_messages(self, messages):
        with open(self.active, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def _config(self, cursor="line", filter_from="bob"):
        return {
            "shared_dir": str(self.tmpdir),
            "agent_id": "alice",
            "agents": {
                "alice": {
                    "id": "alice",
                    "cursor": cursor,
                    "filter_from": filter_from,
                    "wakeup": {
                        "url": "http://127.0.0.1:1/hook",
                        "method": "POST",
                        "body_template": {"message": "{{message}}"},
                    },
                }
            },
        }

    def test_archives_only_after_pending_messages_are_delivered(self):
        self._write_messages([
            {"ts": "2025-01-01 00:00:00", "from": "bob", "msg": f"m{i}"}
            for i in range(60)
        ])

        with mock.patch("poll.wakeup_agent", return_value=(True, "status=204")) as wakeup:
            result = run_poll(self._config())

        self.assertTrue(result["ok"])
        self.assertTrue(result["delivered"])
        self.assertIsNotNone(result["archived"])
        self.assertEqual(result["to_agent"], "alice")
        wakeup.assert_called_once()
        self.assertEqual(self.active.read_text(encoding="utf-8"), "")

    def test_timestamp_cursor_keeps_same_second_later_lines(self):
        ts = "2025-01-01 00:00:00"
        self._write_messages([
            {"ts": ts, "from": "bob", "msg": "old"},
            {"ts": ts, "from": "bob", "msg": "new"},
        ])
        (self.tmpdir / ".alice_ts_cursor").write_text(f"{ts}|1", encoding="utf-8")

        with mock.patch("poll.wakeup_agent", return_value=(True, "status=204")) as wakeup:
            result = run_poll(self._config(cursor="timestamp"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["new_msgs"], 1)
        self.assertIn("new", wakeup.call_args.args[1])
        self.assertNotIn("old", wakeup.call_args.args[1])

    def test_empty_filter_receives_all_other_agents(self):
        self._write_messages([
            {"ts": "2025-01-01 00:00:00", "from": "alice", "msg": "mine"},
            {"ts": "2025-01-01 00:00:01", "from": "bob", "msg": "from bob"},
            {"ts": "2025-01-01 00:00:02", "from": "carol", "msg": "from carol"},
        ])

        with mock.patch("poll.wakeup_agent", return_value=(True, "status=204")) as wakeup:
            result = run_poll(self._config(filter_from=""))

        self.assertTrue(result["ok"])
        self.assertEqual(result["new_msgs"], 2)
        delivered_text = wakeup.call_args.args[1]
        self.assertNotIn("mine", delivered_text)
        self.assertIn("from bob", delivered_text)
        self.assertIn("from carol", delivered_text)


if __name__ == "__main__":
    unittest.main()
