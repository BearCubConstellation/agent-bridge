#!/usr/bin/env python3
"""
Agent Bridge — 后台轮询管理模块

导出: PollManager 类。
"""
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # ui/ itself for intra-package imports
from poll import do_archive, parse_jsonl, load_config as load_poll_config
from scheduler import get_scheduler

from config import BRIDGE_FILENAME, DEFAULT_POLL_INTERVAL


class PollManager:
    """Manage the background polling thread."""

    def __init__(self, shared_dir, interval=DEFAULT_POLL_INTERVAL):
        self.shared_dir = shared_dir
        self.interval = interval
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.last_result = {"ok": True, "new_msgs": 0, "delivered": False,
                            "archived": None, "error": "", "to_agent": "", "from_agent": ""}
        self.last_run = None
        self.running = False
        self.history = []  # [(timestamp, result_dict), ...]
        self.MAX_HISTORY = 100

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="poll-worker")
        self._thread.start()
        self.running = True

    def stop(self):
        self._stop.set()
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def poll_now(self):
        """Run one polling cycle synchronously."""
        return self._do_poll()

    def is_running(self):
        return self.running and self._thread is not None and self._thread.is_alive()

    def _loop(self):
        while not self._stop.is_set():
            self._do_poll()
            self._stop.wait(self.interval)

    def _do_poll(self):
        """Run one poll cycle — V2 Scheduler-only (no V1 fallback)."""
        config_path = Path(self.shared_dir) / BRIDGE_FILENAME
        if not config_path.exists():
            return self.last_result

        config = load_poll_config(str(config_path))
        sched = get_scheduler()

        # Ensure Scheduler is running; start it if not
        if not sched.is_running:
            sched.set_config(config)
            sched.start()
            import logging
            logging.info("[PollManager] Scheduler was not running, auto-started. "
                         "is_running=%s", sched.is_running)

        if not sched.is_running:
            # Scheduler failed to start — log error and return
            result = {"ok": False, "new_msgs": 0, "delivered": False,
                      "archived": None, "rooms": {},
                      "error": "Scheduler failed to start — V2-only mode, no V1 fallback"}
            with self._lock:
                self.last_result = result
                self.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.history.append((self.last_run, dict(result)))
                if len(self.history) > self.MAX_HISTORY:
                    self.history = self.history[-self.MAX_HISTORY:]
            return result

        # V2-only path: legacy archive scan + scheduler scan
        result = {"ok": True, "new_msgs": 0, "delivered": False,
                  "archived": None, "rooms": {}}
        try:
            # Legacy archive check (shared active.jsonl only)
            shared = Path(self.shared_dir)
            active = shared / "active.jsonl"
            if active.exists():
                msgs = parse_jsonl(active)
                if len(msgs) > 200:
                    name = do_archive(shared)
                    if name:
                        result["archived"] = name
        except Exception:
            pass
        try:
            sched.set_config(config)
            enqueued = sched.scan_running_rooms(config)
            result["scheduler_scan"] = enqueued
        except Exception:
            pass

        with self._lock:
            self.last_result = result
            self.last_run = now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.history.append((now, dict(result)))
            if len(self.history) > self.MAX_HISTORY:
                self.history = self.history[-self.MAX_HISTORY:]
        return result

    def get_status(self):
        with self._lock:
            return {
                "running": self.is_running(),
                "interval": self.interval,
                "last_run": self.last_run,
                "last_result": dict(self.last_result),
            }

    def get_history(self, limit=50):
        with self._lock:
            return [{"ts": ts, **r} for ts, r in self.history[-limit:]]
