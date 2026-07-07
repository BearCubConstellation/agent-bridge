#!/usr/bin/env python3
"""Concurrent, room-serial scheduler for Agent Bridge V2.

Different rooms are dispatched through a small worker pool.  A room itself is
never executed concurrently: work requested while a room is in flight is
coalesced into one follow-up step.  Deadline timers wake waiting turns at their
actual timeout instead of relying only on a coarse polling sweep.
"""
from __future__ import annotations

import collections
import concurrent.futures
import heapq
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_parent = str(Path(__file__).resolve().parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from protocol import ROOM_RUNNING

logger = logging.getLogger(__name__)
_IDLE_INTERVAL = 1.0
_DEFAULT_WORKERS = 4


class Scheduler:
    """In-memory room scheduler with bounded cross-room parallelism."""

    def __init__(self, idle_interval=_IDLE_INTERVAL, max_workers=_DEFAULT_WORKERS):
        self._queue = collections.deque()
        self._queued = set()
        self._inflight = set()
        self._reschedule = set()
        self._deadlines = {}
        self._timers = []
        self._sequence = 0
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._worker = None
        self._executor = None
        self._idle_interval = float(idle_interval)
        self._max_workers = max(1, int(max_workers))
        self._config = {}

    def set_config(self, config):
        """Store the current configuration for autonomous queued work."""
        with self._condition:
            self._config = dict(config or {})

    def start(self):
        if self.is_running:
            return
        self._stop_event.clear()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="agent-bridge-room",
        )
        self._worker = threading.Thread(target=self._worker_loop, name="scheduler-dispatch", daemon=True)
        self._worker.start()
        logger.info("scheduler started with %d room workers", self._max_workers)

    def stop(self, timeout=5.0):
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        self._worker = None
        executor, self._executor = self._executor, None
        if executor:
            executor.shutdown(wait=False)

    @property
    def is_running(self):
        return bool(self._worker and self._worker.is_alive())

    @property
    def queue_size(self):
        with self._condition:
            return len(self._queued) + len(self._inflight)

    @property
    def queued_rooms(self):
        with self._condition:
            return list(self._queue)

    def _enqueue_locked(self, room_id):
        if room_id in self._queued:
            return False
        self._queued.add(room_id)
        self._queue.append(room_id)
        self._deadlines.pop(room_id, None)
        return True

    def schedule_room(self, room_id):
        """Request an immediate step; duplicate requests are coalesced."""
        if not room_id:
            return False
        with self._condition:
            if room_id in self._inflight:
                self._reschedule.add(room_id)
                return False
            added = self._enqueue_locked(room_id)
            if added:
                self._condition.notify()
            return added

    def schedule_room_at(self, room_id, deadline_epoch):
        """Request a step at a Unix timestamp, replacing later deadlines."""
        if not room_id:
            return False
        try:
            deadline = float(deadline_epoch)
        except (TypeError, ValueError):
            return False
        with self._condition:
            existing = self._deadlines.get(room_id)
            if existing is not None and existing <= deadline:
                return False
            self._sequence += 1
            self._deadlines[room_id] = deadline
            heapq.heappush(self._timers, (deadline, self._sequence, room_id))
            self._condition.notify()
            return True

    def tick_room(self, config, room_id):
        """Execute one direct step for administration/testing."""
        return self._run_step(config, room_id)

    def scan_running_rooms(self, config):
        """Enqueue all rooms that remain running after startup/recovery."""
        enqueued = 0
        for room_id, room_cfg in (config or {}).get("rooms", {}).items():
            status = room_cfg.get("status", "") if isinstance(room_cfg, dict) else ""
            if not status:
                status = self._read_room_status_from_disk(config, room_id)
            if status == ROOM_RUNNING and self.schedule_room(room_id):
                enqueued += 1
        return enqueued

    def _promote_due_timers_locked(self):
        now = time.time()
        while self._timers and self._timers[0][0] <= now:
            deadline, _seq, room_id = heapq.heappop(self._timers)
            if self._deadlines.get(room_id) != deadline:
                continue
            self._deadlines.pop(room_id, None)
            if room_id in self._inflight:
                self._reschedule.add(room_id)
            else:
                self._enqueue_locked(room_id)

    def _take_next_room(self):
        with self._condition:
            while not self._stop_event.is_set():
                self._promote_due_timers_locked()
                if self._queue:
                    room_id = self._queue.popleft()
                    self._queued.discard(room_id)
                    if room_id in self._inflight:
                        self._reschedule.add(room_id)
                        continue
                    self._inflight.add(room_id)
                    return room_id, dict(self._config)

                wait_for = self._idle_interval
                while self._timers and self._deadlines.get(self._timers[0][2]) != self._timers[0][0]:
                    heapq.heappop(self._timers)
                if self._timers:
                    wait_for = max(0.0, min(wait_for, self._timers[0][0] - time.time()))
                self._condition.wait(wait_for)
            return None, None

    def _worker_loop(self):
        while not self._stop_event.is_set():
            room_id, config = self._take_next_room()
            if room_id is None:
                continue
            if not config:
                logger.warning("scheduler has no config; skipping %s", room_id)
                self._finish_room(room_id, None)
                continue
            if not self._executor:
                self._finish_room(room_id, None)
                continue
            future = self._executor.submit(self._run_step, config, room_id)
            future.add_done_callback(lambda done, rid=room_id: self._finish_room(rid, done))

    def _finish_room(self, room_id, future):
        if future is not None:
            try:
                future.result()
            except Exception:
                logger.exception("scheduler step failed for room %s", room_id)
        with self._condition:
            self._inflight.discard(room_id)
            if room_id in self._reschedule:
                self._reschedule.discard(room_id)
                self._enqueue_locked(room_id)
            self._condition.notify()

    @staticmethod
    def _run_step(config, room_id):
        from runtime import run_room_step
        return run_room_step(config, room_id)

    @staticmethod
    def _read_room_status_from_disk(config, room_id):
        try:
            shared_dir = Path(os.path.expandvars(os.path.expanduser(str((config or {}).get("shared_dir", "~/.agent-bridge")))))
            state_path = shared_dir / "rooms" / room_id / "state.json"
            if state_path.exists():
                return json.loads(state_path.read_text(encoding="utf-8")).get("status", "")
        except Exception:
            pass
        return ""


_default_scheduler = None


def get_scheduler():
    global _default_scheduler
    if _default_scheduler is None:
        _default_scheduler = Scheduler()
    return _default_scheduler
