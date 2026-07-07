#!/usr/bin/env python3
"""Atomic, room-scoped state access for the V2 runtime.

The runtime and callback endpoint can update the same ``state.json`` from
independent threads.  This module provides a very small transaction boundary:
read, mutate and atomically replace the state file while holding the room state
lock.  Network calls must stay outside ``mutate_room_state``.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from lock import file_lock
from protocol import migrate_room_state
from rooms import default_state, room_dir


Mutator = Callable[[dict], Any]


def _state_path(shared_dir, room_id: str) -> Path:
    return room_dir(shared_dir, room_id) / "state.json"


def _load_unlocked(shared_dir, room_id: str, room_cfg=None) -> dict:
    path = _state_path(shared_dir, room_id)
    fallback = default_state(room_cfg or {"id": room_id})
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(raw, dict):
                raise ValueError("state is not an object")
        except Exception:
            # Preserve evidence instead of silently treating a partial write as
            # an empty room.  The next successful write repairs state.json.
            stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
            backup = path.with_name(f"state.json.corrupt-{stamp}")
            try:
                os.replace(path, backup)
            except OSError:
                pass
            raw = {}
    state = migrate_room_state({**fallback, **raw}, room_cfg)
    try:
        state["revision"] = int(state.get("revision", 0))
    except (TypeError, ValueError):
        state["revision"] = 0
    return state


def _atomic_write_unlocked(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        # Best effort only: directory fsync is unavailable on some platforms.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def read_room_state_consistent(shared_dir, room_id: str, room_cfg=None) -> dict:
    """Read a migrated state snapshot while holding the state lock."""
    rdir = room_dir(shared_dir, room_id)
    rdir.mkdir(parents=True, exist_ok=True)
    with file_lock(rdir / ".state.lock"):
        return copy.deepcopy(_load_unlocked(shared_dir, room_id, room_cfg))


def mutate_room_state(
    shared_dir,
    room_id: str,
    room_cfg=None,
    mutator: Optional[Mutator] = None,
) -> Tuple[dict, Any]:
    """Atomically mutate a room state and return ``(state, mutator_result)``.

    ``mutator`` receives the live state dictionary.  Returning ``False`` skips
    persistence, which is useful for read-only decisions.  No callbacks or
    adapter/network calls should be made inside the mutator.
    """
    rdir = room_dir(shared_dir, room_id)
    rdir.mkdir(parents=True, exist_ok=True)
    with file_lock(rdir / ".state.lock"):
        state = _load_unlocked(shared_dir, room_id, room_cfg)
        result = mutator(state) if mutator else None
        if result is not False:
            state["revision"] = int(state.get("revision", 0)) + 1
            _atomic_write_unlocked(_state_path(shared_dir, room_id), state)
        return copy.deepcopy(state), result


def write_room_state_consistent(shared_dir, room_id: str, state: dict, room_cfg=None) -> dict:
    """Replace room state atomically and return the persisted snapshot."""
    def replace(current):
        current.clear()
        current.update(migrate_room_state(dict(state or {}), room_cfg))
        return True

    saved, _ = mutate_room_state(shared_dir, room_id, room_cfg, replace)
    return saved
