#!/usr/bin/env python3
"""Security utilities for Agent Bridge v2.

Provides token validation, ID sanitization, and callback authentication.
"""
import os
import re
import hashlib
import hmac
import sys
from pathlib import Path

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from protocol import VALID_ID_RE                              # noqa: E402


# ── ID Validation ───────────────────────────────────────

def validate_room_id(room_id):
    """Return True if room_id matches safe pattern."""
    return bool(room_id and VALID_ID_RE.match(str(room_id)))


def validate_agent_id(agent_id):
    """Return True if agent_id matches safe pattern."""
    return bool(agent_id and VALID_ID_RE.match(str(agent_id)))


# ── Token / Callback Auth ───────────────────────────────

def resolve_token(token_value):
    """Resolve a token value that may be:
    - A plain string
    - An env var reference: ${ENV_VAR}
    - A file path to read token from
    """
    if not token_value:
        return None

    s = str(token_value).strip()

    # Env var reference: ${VAR_NAME}
    env_match = re.match(r'^\$\{(\w+)\}$', s)
    if env_match:
        return os.environ.get(env_match.group(1))

    # Tilde-expand and check if it's a file path
    expanded = os.path.expanduser(os.path.expandvars(s))
    if os.path.isfile(expanded):
        try:
            with open(expanded, 'r') as f:
                return f.read().strip()
        except (OSError, IOError):
            return None

    return s


def verify_callback_token(config, agent_id, provided_token):
    """Verify a callback token for a specific agent.

    Token lookup order:
    1. config['security']['callback_tokens'][agent_id]
    2. config['security']['callback_token'] (global fallback)
    3. No security configured → allow (local-only mode)

    Returns (ok: bool, error: str)
    """
    security = config.get("security") or {}
    tokens = security.get("callback_tokens") or {}

    if not tokens and not security.get("callback_token"):
        # No security configured — local mode, allow all
        return True, ""

    expected = tokens.get(agent_id) or security.get("callback_token")
    if not expected:
        return False, f"no token configured for agent: {agent_id}"

    resolved = resolve_token(expected)
    if not resolved:
        return False, "token resolution failed"

    if not provided_token:
        return False, "missing token"

    # Constant-time comparison
    if not hmac.compare_digest(resolved, provided_token):
        return False, "invalid token"

    return True, ""


def extract_bearer_token(headers):
    """Extract Bearer token from HTTP headers dict."""
    auth = headers.get("Authorization", "") or headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def extract_token_from_request(headers, params):
    """Extract token from Authorization header or query param.

    Priority: Authorization header > ?token= query param
    """
    bearer = extract_bearer_token(headers)
    if bearer:
        return bearer
    return params.get("token", "")


# ── Agent-Room Membership ───────────────────────────────

def agent_in_room(config, room_id, agent_id):
    """Check if agent_id is a member of the specified room.

    Checks both ``room["agents"]`` (explicit membership list) and
    ``room["order"]`` (turn-ordering list, which also implies membership).
    """
    rooms = config.get("rooms") or {}
    room = rooms.get(room_id)
    if not room:
        return False
    agents = room.get("agents") or []
    order = room.get("order") or []
    return agent_id in agents or agent_id in order


# ── Input Sanitization ──────────────────────────────────

def sanitize_message(text, max_length=50000):
    """Sanitize message text. Returns cleaned string or raises ValueError."""
    if not text or not isinstance(text, str):
        raise ValueError("message must be a non-empty string")
    text = text.strip()
    if not text:
        raise ValueError("message must be a non-empty string")
    if len(text) > max_length:
        raise ValueError(f"message exceeds max length ({max_length})")
    # Strip null bytes
    text = text.replace('\x00', '')
    return text
