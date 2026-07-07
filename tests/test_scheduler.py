#!/usr/bin/env python3
"""Tests for the V2 room scheduler."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from scheduler import Scheduler, get_scheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler(idle_interval=0.02, max_workers=2)

    def tearDown(self):
        self.scheduler.stop(timeout=1.0)

    def test_schedule_room_deduplicates(self):
        self.assertTrue(self.scheduler.schedule_room("room-a"))
        self.assertFalse(self.scheduler.schedule_room("room-a"))
        self.assertEqual(1, self.scheduler.queue_size)

    def test_tick_room_delegates_to_runtime_step(self):
        expected = {"ok": True, "room_id": "room-x"}
        with mock.patch.object(self.scheduler, "_run_step", return_value=expected) as run_step:
            self.assertEqual(expected, self.scheduler.tick_room({"rooms": {}}, "room-x"))
        run_step.assert_called_once()

    def test_scan_running_rooms_enqueues_only_running(self):
        count = self.scheduler.scan_running_rooms({"rooms": {"a": {"status": "running"}, "b": {"status": "paused"}}})
        self.assertEqual(1, count)
        self.assertEqual(["a"], self.scheduler.queued_rooms)

    def test_different_rooms_run_concurrently(self):
        slow_started = threading.Event()
        fast_done = threading.Event()
        release = threading.Event()

        def step(_config, room_id):
            if room_id == "slow":
                slow_started.set()
                release.wait(1)
            else:
                fast_done.set()
            return {"ok": True}

        self.scheduler.set_config({"rooms": {"slow": {}, "fast": {}}})
        with mock.patch("runtime.run_room_step", side_effect=step):
            self.scheduler.start()
            self.scheduler.schedule_room("slow")
            self.assertTrue(slow_started.wait(0.5))
            self.scheduler.schedule_room("fast")
            self.assertTrue(fast_done.wait(0.5))
            release.set()


class SchedulerSingletonTests(unittest.TestCase):
    def test_get_scheduler_returns_singleton(self):
        self.assertIs(get_scheduler(), get_scheduler())


if __name__ == "__main__":
    unittest.main()
