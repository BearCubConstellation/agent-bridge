#!/usr/bin/env python3
"""EventBus — append and read room-scoped events.

Every room stores its event stream in ``events.jsonl`` inside the room
directory.  This module provides the thin helpers that other core
modules (and adapters) use to emit / query events.
"""
import sys
from pathlib import Path

# ── Local package imports (script-friendly) ───────────
_parent = str(Path(__file__).resolve().parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from protocol import make_event, gen_event_id       # noqa: E402
from storage import append_jsonl, read_jsonl         # noqa: E402
from rooms import room_dir, append_room_log          # noqa: E402


# ── Helpers ───────────────────────────────────────────

def _events_path(shared_dir, room_id):
    """Return the Path to ``events.jsonl`` for a room."""
    return room_dir(shared_dir, room_id) / "events.jsonl"


# ── Public API ────────────────────────────────────────

def emit_event(shared_dir, room_id, event_type,
               actor="", turn_id="", correlation_id="",
               message_id="", meta=None):
    """Create an event, persist it to events.jsonl and log to runtime.log.

    Parameters
    ----------
    shared_dir : str | Path
        Root shared directory (e.g. ``~/.agent-bridge``).
    room_id : str
        The room identifier.
    event_type : str
        One of the ``EVT_*`` constants from ``protocol``.
    actor : str
        Agent or entity that triggered the event.
    turn_id, correlation_id, message_id : str
        Optional correlation keys.
    meta : dict | None
        Arbitrary extra payload.

    Returns
    -------
    dict
        The fully-formed event record (with ``id`` and ``ts``).
    """
    event = make_event(
        room=room_id,
        event_type=event_type,
        actor=actor,
        turn_id=turn_id,
        correlation_id=correlation_id,
        message_id=message_id,
        meta=meta,
    )

    # 1) Persist to events.jsonl
    path = _events_path(shared_dir, room_id)
    append_jsonl(path, event)

    # 2) Human-readable log entry
    summary = f"[{event_type}]"
    if actor:
        summary += f" actor={actor}"
    if turn_id:
        summary += f" turn={turn_id}"

    append_room_log(
        shared_dir,
        room_id,
        event=event_type,
        message=summary,
        level="info",
        agent_id=actor,
        meta={
            "event_id": event["id"],
            "turn_id": turn_id,
            "correlation_id": correlation_id,
            "message_id": message_id,
        },
    )

    return event


def read_events(shared_dir, room_id, limit=500):
    """Read the most recent events for a room.

    Parameters
    ----------
    shared_dir : str | Path
        Root shared directory.
    room_id : str
        The room identifier.
    limit : int
        Maximum number of events to return (from the tail).

    Returns
    -------
    list[dict]
        Event records, newest-last order (as written on disk).
    """
    path = _events_path(shared_dir, room_id)
    events = read_jsonl(path)
    if limit and len(events) > int(limit):
        events = events[-int(limit):]
    return events
