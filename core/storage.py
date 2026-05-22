#!/usr/bin/env python3
"""General storage utilities — JSONL append/read, lock helpers.

Thin wrapper around pathlib + json that all other core modules use.
"""
import json
import os
from pathlib import Path

# Local import (same package)
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lock import file_lock


def resolve_path(p):
    """Expand ~, env vars, and return a Path."""
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def ensure_parent(path):
    """Create parent directories if needed."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def append_jsonl(path, record):
    """Append one JSON record as a line to a JSONL file.  Thread-safe via lock."""
    path = Path(path)
    ensure_parent(path)
    lock_path = path.parent / f".{path.name}.lock"
    with file_lock(lock_path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_jsonl(path):
    """Read all JSON records from a JSONL file.  Returns list of dicts."""
    path = Path(path)
    if not path.exists():
        return []
    results = []
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    record["_line"] = i + 1
                    results.append(record)
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return results


def read_jsonl_no_line(path):
    """Read JSONL without adding _line (for compatibility with poll.py parse_jsonl)."""
    path = Path(path)
    if not path.exists():
        return []
    results = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return results


def write_json(path, data):
    """Write a JSON file atomically (lock + overwrite)."""
    path = Path(path)
    ensure_parent(path)
    lock_path = path.parent / f".{path.stem}.lock"
    with file_lock(lock_path):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path, fallback=None):
    """Read a JSON file, return fallback on failure."""
    path = Path(path)
    if not path.exists():
        return fallback if fallback is not None else {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return fallback if fallback is not None else {}
