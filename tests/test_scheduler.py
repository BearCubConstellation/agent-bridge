#!/usr/bin/env python3
"""Tests for core/scheduler.py — in-memory room processing queue."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from scheduler import Scheduler, get_scheduler


class TestSchedulerBasic(unittest.TestCase):
    """Test basic scheduler operations."""

    def setUp(self):
        self.scheduler = Scheduler(idle_interval=0.1)

    def tearDown(self):
        if self.scheduler.is_running:
            self.scheduler.stop(timeout=2.0)

    def test_schedule_room_adds_to_queue(self):
        result = self.scheduler.schedule_room("room-a")
        self.assertTrue(result)
        self.assertEqual(self.scheduler.queue_size, 1)

    def test_schedule_room_dedup(self):
        self.scheduler.schedule_room("room-a")
        result = self.scheduler.schedule_room("room-a")
        self.assertFalse(result)
        self.assertEqual(self.scheduler.queue_size, 1)

    def test_schedule_multiple_rooms(self):
        self.scheduler.schedule_room("room-a")
        self.scheduler.schedule_room("room-b")
        self.scheduler.schedule_room("room-c")
        self.assertEqual(self.scheduler.queue_size, 3)

    def test_queued_rooms_returns_list(self):
        self.scheduler.schedule_room("room-a")
        self.scheduler.schedule_room("room-b")
        rooms = self.scheduler.queued_rooms
        self.assertIsInstance(rooms, list)
        self.assertEqual(len(rooms), 2)
        self.assertIn("room-a", rooms)
        self.assertIn("room-b", rooms)

    def test_queue_size_initially_zero(self):
        self.assertEqual(self.scheduler.queue_size, 0)

    def test_queued_rooms_initially_empty(self):
        self.assertEqual(self.scheduler.queued_rooms, [])


class TestSchedulerLifecycle(unittest.TestCase):
    """Test start/stop lifecycle."""

    def setUp(self):
        self.scheduler = Scheduler(idle_interval=0.1)

    def tearDown(self):
        if self.scheduler.is_running:
            self.scheduler.stop(timeout=2.0)

    def test_start_starts_worker(self):
        self.assertFalse(self.scheduler.is_running)
        self.scheduler.start()
        self.assertTrue(self.scheduler.is_running)

    def test_stop_stops_worker(self):
        self.scheduler.start()
        self.assertTrue(self.scheduler.is_running)
        self.scheduler.stop(timeout=2.0)
        self.assertFalse(self.scheduler.is_running)

    def test_start_idempotent(self):
        self.scheduler.start()
        self.assertTrue(self.scheduler.is_running)
        # Second start should be a no-op
        self.scheduler.start()
        self.assertTrue(self.scheduler.is_running)
        self.scheduler.stop()

    def test_stop_when_not_running(self):
        self.assertFalse(self.scheduler.is_running)
        self.scheduler.stop(timeout=1.0)
        self.assertFalse(self.scheduler.is_running)

    def test_worker_processes_queue(self):
        """Worker should drain the queue when running."""
        self.scheduler.set_config({"rooms": {}})
        self.scheduler.schedule_room("room-a")
        self.scheduler.schedule_room("room-b")
        self.assertEqual(self.scheduler.queue_size, 2)

        self.scheduler.start()
        # Give worker time to drain
        time.sleep(1.0)
        self.scheduler.stop()

        # Queue should be empty (or mostly empty) after worker drains it
        size = self.scheduler.queue_size
        self.assertLess(size, 2,
                        f"Expected queue to drain, but {size} items remain")


class TestSchedulerTickRoom(unittest.TestCase):
    """Test tick_room bypasses the queue."""

    def setUp(self):
        self.scheduler = Scheduler(idle_interval=0.1)

    def tearDown(self):
        if self.scheduler.is_running:
            self.scheduler.stop(timeout=2.0)

    def test_tick_room_calls_run_room_step(self):
        mock_run = mock.MagicMock(return_value={"ok": True, "room_id": "room-x"})
        config = {"rooms": {"room-x": {"status": "running"}}}
        with mock.patch.object(self.scheduler, "_run_step", mock_run):
            result = self.scheduler.tick_room(config, "room-x")
        mock_run.assert_called_once_with(config, "room-x")
        self.assertEqual(result, {"ok": True, "room_id": "room-x"})

    def test_tick_room_does_not_affect_queue(self):
        mock_run = mock.MagicMock(return_value={"ok": True})
        config = {"rooms": {}}
        self.scheduler.schedule_room("room-a")
        self.assertEqual(self.scheduler.queue_size, 1)
        with mock.patch.object(self.scheduler, "_run_step", mock_run):
            self.scheduler.tick_room(config, "room-x")
        # Queue should be unchanged
        self.assertEqual(self.scheduler.queue_size, 1)

    def test_tick_room_runtime_not_available(self):
        """When runtime.run_room_step cannot be imported, returns error."""
        # We need to make the import fail. Monkey-patch sys.modules to simulate.
        with mock.patch.dict("sys.modules", {"runtime": None}):
            result = self.scheduler.tick_room({}, "room-x")
        self.assertFalse(result["ok"])
        self.assertIn("runtime module not available", result["error"])


class TestSchedulerScanRunningRooms(unittest.TestCase):
    """Test scan_running_rooms enqueues rooms with running status."""

    def setUp(self):
        self.scheduler = Scheduler(idle_interval=0.1)

    def tearDown(self):
        if self.scheduler.is_running:
            self.scheduler.stop(timeout=2.0)

    def test_scan_enqueues_running_rooms(self):
        config = {
            "rooms": {
                "room-a": {"status": "running"},
                "room-b": {"status": "paused"},
                "room-c": {"status": "running"},
            }
        }
        count = self.scheduler.scan_running_rooms(config)
        self.assertEqual(count, 2)
        self.assertEqual(self.scheduler.queue_size, 2)
        rooms = self.scheduler.queued_rooms
        self.assertIn("room-a", rooms)
        self.assertIn("room-c", rooms)

    def test_scan_no_running_rooms(self):
        config = {
            "rooms": {
                "room-a": {"status": "paused"},
                "room-b": {"status": "archived"},
            }
        }
        count = self.scheduler.scan_running_rooms(config)
        self.assertEqual(count, 0)
        self.assertEqual(self.scheduler.queue_size, 0)

    def test_scan_empty_config(self):
        count = self.scheduler.scan_running_rooms({})
        self.assertEqual(count, 0)

    def test_scan_dedup(self):
        config = {"rooms": {"room-a": {"status": "running"}}}
        self.scheduler.schedule_room("room-a")  # already queued
        count = self.scheduler.scan_running_rooms(config)
        self.assertEqual(count, 0)  # no new rooms, already queued


class TestSchedulerSetConfig(unittest.TestCase):
    """Test set_config stores config for background worker."""

    def setUp(self):
        self.scheduler = Scheduler(idle_interval=0.1)

    def tearDown(self):
        if self.scheduler.is_running:
            self.scheduler.stop(timeout=2.0)

    def test_set_config_stores_config(self):
        config = {"rooms": {"r1": {"status": "running"}}}
        self.scheduler.set_config(config)
        self.assertEqual(self.scheduler._config, config)

    def test_worker_uses_config(self):
        """Worker with config should call _run_step, not skip."""
        config = {"rooms": {}}
        self.scheduler.set_config(config)
        self.scheduler.schedule_room("room-x")

        with mock.patch.object(self.scheduler, "_run_step") as mock_step:
            mock_step.return_value = {"ok": True}
            self.scheduler.start()
            time.sleep(0.5)
            self.scheduler.stop()
            mock_step.assert_called_with(config, "room-x")


class TestGetScheduler(unittest.TestCase):
    """Test module-level get_scheduler singleton."""

    def test_returns_same_instance(self):
        s1 = get_scheduler()
        s2 = get_scheduler()
        self.assertIs(s1, s2)

    def test_returns_scheduler_instance(self):
        s = get_scheduler()
        self.assertIsInstance(s, Scheduler)


if __name__ == "__main__":
    unittest.main()
