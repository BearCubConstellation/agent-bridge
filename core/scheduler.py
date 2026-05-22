#!/usr/bin/env python3
"""Scheduler for Agent Bridge v2.

Manages an in-memory queue of room_ids that need processing.
A background worker thread pulls room_ids from the queue and
delegates to ``runtime.run_room_step(config, room_id)``.

The scheduler does NOT read ``bridge.yaml`` — the config dict is
passed in from the outside (typically the server or CLI entry-point).
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from protocol import ROOM_RUNNING  # noqa: E402

logger = logging.getLogger(__name__)

# Default interval (seconds) between worker sweeps when the queue is empty.
_IDLE_INTERVAL = 2.0


class Scheduler:
    """In-memory scheduler that drives room processing in a worker thread.

    Usage::

        sched = Scheduler()
        sched.start()
        sched.schedule_room("room-1")
        ...
        sched.stop()
    """

    def __init__(self, idle_interval: float = _IDLE_INTERVAL):
        # ── Internal state ───────────────────────────────
        self._queue: set = set()           # deduplicated room_ids
        self._lock = threading.Lock()      # protects _queue
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._idle_interval = idle_interval

    # ── Public API ───────────────────────────────────────

    def schedule_room(self, room_id: str) -> bool:
        """Add *room_id* to the processing queue.

        Returns ``True`` if this is a new entry (was not already queued),
        ``False`` if it was already present.
        """
        with self._lock:
            if room_id in self._queue:
                logger.debug("schedule_room: %s already queued", room_id)
                return False
            self._queue.add(room_id)
            logger.info("schedule_room: %s enqueued (queue size=%d)",
                        room_id, len(self._queue))
            return True

    def tick_room(self, config: dict, room_id: str) -> dict:
        """Execute one processing step for *room_id* immediately.

        This bypasses the queue and calls ``runtime.run_room_step``
        synchronously in the current thread.  Useful for manual triggers
        or API-initiated ticks.

        Returns the result dict from ``runtime.run_room_step``.
        """
        logger.info("tick_room: executing step for %s", room_id)
        return self._run_step(config, room_id)

    def start(self) -> None:
        """Start the background worker thread.

        If the worker is already running this is a no-op.
        """
        if self._worker is not None and self._worker.is_alive():
            logger.warning("start: worker already running")
            return

        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="scheduler-worker",
            daemon=True,
        )
        self._worker.start()
        logger.info("start: worker thread started")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker thread to stop and wait for it to finish.

        *timeout* is the maximum seconds to wait for a clean shutdown.
        """
        self._stop_event.set()
        if self._worker is not None and self._worker.is_alive():
            logger.info("stop: waiting for worker (timeout=%.1fs)", timeout)
            self._worker.join(timeout=timeout)
            if self._worker.is_alive():
                logger.warning("stop: worker did not exit within timeout")
            else:
                logger.info("stop: worker exited cleanly")
        self._worker = None

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the worker thread is alive."""
        return self._worker is not None and self._worker.is_alive()

    @property
    def queue_size(self) -> int:
        """Return the number of room_ids currently in the queue."""
        with self._lock:
            return len(self._queue)

    @property
    def queued_rooms(self) -> list:
        """Return a snapshot list of currently queued room_ids."""
        with self._lock:
            return list(self._queue)

    # ── Scan / discovery ─────────────────────────────────

    def scan_running_rooms(self, config: dict) -> int:
        """Scan all rooms in *config* and enqueue those with status
        ``ROOM_RUNNING``.

        This is a "catch-up" mechanism: after a restart or periodic
        health-check, call this to ensure no running room is left
        unscheduled.

        Returns the number of newly enqueued rooms.
        """
        rooms = config.get("rooms", {})
        if not rooms:
            logger.debug("scan_running_rooms: no rooms in config")
            return 0

        enqueued = 0
        for room_id, room_cfg in rooms.items():
            status = ""
            # Try to read status from room_cfg directly
            if isinstance(room_cfg, dict):
                status = room_cfg.get("status", "")

            # If status not in config, try to read from disk state
            if not status:
                status = self._read_room_status_from_disk(config, room_id)

            if status == ROOM_RUNNING:
                if self.schedule_room(room_id):
                    enqueued += 1

        if enqueued:
            logger.info("scan_running_rooms: enqueued %d running rooms",
                        enqueued)
        return enqueued

    # ── Worker loop ──────────────────────────────────────

    def _worker_loop(self) -> None:
        """Main loop run in the background thread.

        Continuously pops room_ids from the queue and processes them
        until ``_stop_event`` is set.
        """
        logger.info("worker: loop started")
        while not self._stop_event.is_set():
            room_id = self._pop_next()
            if room_id is None:
                # Queue empty — sleep briefly and try again
                self._stop_event.wait(self._idle_interval)
                continue

            try:
                # NOTE: config is NOT stored in the scheduler.
                # The worker loop currently requires the config to have
                # been injected via set_config() or passed externally.
                # For the autonomous worker path we read the stored config.
                config = getattr(self, "_config", {})
                if config:
                    self._run_step(config, room_id)
                else:
                    logger.warning(
                        "worker: no config available, skipping %s. "
                        "Call set_config() or use tick_room() instead.",
                        room_id,
                    )
            except Exception:
                logger.exception("worker: error processing room %s",
                                 room_id)
        logger.info("worker: loop ended")

    def set_config(self, config: dict) -> None:
        """Store a reference to the application config dict.

        The background worker uses this when pulling items from the
        queue autonomously.  The caller must call this before (or
        shortly after) ``start()``.
        """
        self._config = config

    # ── Internal helpers ─────────────────────────────────

    def _pop_next(self):
        """Pop and return the next room_id from the queue, or ``None``."""
        with self._lock:
            if not self._queue:
                return None
            # Pop an arbitrary element (set pop)
            return self._queue.pop()

    @staticmethod
    def _run_step(config: dict, room_id: str) -> dict:
        """Import runtime and execute one step.

        The import is deferred so that ``core/runtime.py`` does not need
        to exist at module-load time (it may be created by another task).
        """
        try:
            from runtime import run_room_step  # noqa: E402
        except ImportError:
            # runtime.py not yet available — return a placeholder result
            logger.warning(
                "scheduler: runtime module not available, "
                "skipping run_room_step for %s", room_id,
            )
            return {
                "ok": False,
                "room_id": room_id,
                "error": "runtime module not available",
            }

        return run_room_step(config, room_id)

    @staticmethod
    def _read_room_status_from_disk(config: dict, room_id: str) -> str:
        """Best-effort read of a room's status from its state.json on disk."""
        try:
            shared_dir = Path(
                os.path.expandvars(
                    os.path.expanduser(
                        str(config.get("shared_dir", "~/.agent-bridge"))
                    )
                )
            )
            state_path = shared_dir / "rooms" / room_id / "state.json"
            if state_path.exists():
                import json
                state = json.loads(state_path.read_text(encoding="utf-8"))
                return state.get("status", "")
        except Exception:
            pass
        return ""


# ── Module-level convenience singleton ───────────────────

_default_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    """Return the module-level Scheduler singleton."""
    global _default_scheduler
    if _default_scheduler is None:
        _default_scheduler = Scheduler()
    return _default_scheduler


# ── CLI smoke test ───────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    sched = Scheduler(idle_interval=1.0)
    sched.set_config({"rooms": {}})

    print("Starting scheduler...")
    sched.start()

    sched.schedule_room("room-a")
    sched.schedule_room("room-b")
    sched.schedule_room("room-a")  # duplicate, should be no-op

    print(f"Queue size: {sched.queue_size}")
    print(f"Queued rooms: {sched.queued_rooms}")

    # Let worker run briefly (runtime not available, so steps are skipped)
    time.sleep(3)

    print("Stopping scheduler...")
    sched.stop()
    print("Done.")
