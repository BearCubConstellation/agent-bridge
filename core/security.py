#!/usr/bin/env python3
"""Security utilities for Agent Bridge V2."""
from __future__ import annotations

import hmac
import os
import re
import sys
from pathlib import Path

_parent = str(Path(__file__).resolve().parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from protocol import VALID_ID_RE


def validate_room_id(room_id):
    return bool(room_id and VALID_ID_RE.match(str(room_id)))


def validate_agent_id(agent_id):
    return bool(agent_id and VALID_ID_RE.match(str(agent_id)))


def is_loopback_host(host):
    value = str(host or "").strip().lower().strip("[]")
    return value in {"127.0.0.1", "::1", "localhost"}


def resolve_token(token_value):
    """Resolve a plain token, ``${ENV_VAR}``, or token-file reference."""
    if not token_value:
        return None
    value = str(token_value).strip()
    env_match = re.match(r"^\$\{(\w+)\}$", value)
    if env_match:
        return os.environ.get(env_match.group(1))
    expanded = os.path.expanduser(os.path.expandvars(value))
    if os.path.isfile(expanded):
        try:
            with open(expanded, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return None
    return value


def _configured_token(security, key, agent_id=""):
    if key == "callback":
        tokens = security.get("callback_tokens") or {}
        return tokens.get(agent_id) or security.get("callback_token")
    return security.get("mcp_token")


def _verify(expected, provided, missing_message="missing token"):
    resolved = resolve_token(expected)
    if not resolved:
        return False, "token resolution failed"
    if not provided:
        return False, missing_message
    if not hmac.compare_digest(str(resolved), str(provided)):
        return False, "invalid token"
    return True, ""


def verify_callback_token(config, agent_id, provided_token, allow_unauthenticated=True):
    """Verify a callback token.

    Unauthenticated callbacks remain available only when the caller explicitly
    identifies the request as local-only.  The HTTP server passes ``False`` for
    non-loopback binds and refuses to start without configured credentials.
    """
    security = (config or {}).get("security") or {}
    expected = _configured_token(security, "callback", agent_id)
    if not expected:
        if allow_unauthenticated:
            return True, ""
        return False, "callback token is required for non-local bind"
    return _verify(expected, provided_token)


def verify_mcp_token(config, provided_token, allow_unauthenticated=False):
    """Verify the token protecting HTTP MCP endpoints."""
    security = (config or {}).get("security") or {}
    expected = _configured_token(security, "mcp")
    if not expected:
        if allow_unauthenticated:
            return True, ""
        return False, "mcp token is required for non-local bind"
    return _verify(expected, provided_token)


def validate_network_exposure(config, host):
    """Return a startup error when a public bind has no required tokens."""
    if is_loopback_host(host):
        return ""
    security = (config or {}).get("security") or {}
    callbacks = security.get("callback_token") or security.get("callback_tokens")
    mcp_token = security.get("mcp_token")
    missing = []
    if not callbacks:
        missing.append("security.callback_token or security.callback_tokens")
    if not mcp_token:
        missing.append("security.mcp_token")
    if missing:
        return "non-loopback bind requires " + " and ".join(missing)
    return ""


def extract_bearer_token(headers):
    auth = (headers or {}).get("Authorization", "") or (headers or {}).get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def extract_token_from_request(headers, params=None, allow_query=False):
    """Extract an Authorization bearer token.

    Query-string credentials are intentionally disabled by default because they
    leak into logs, browser history and proxy telemetry.
    """
    bearer = extract_bearer_token(headers)
    if bearer:
        return bearer
    if allow_query:
        return (params or {}).get("token", "")
    return ""


def agent_in_room(config, room_id, agent_id):
    room = ((config or {}).get("rooms") or {}).get(room_id)
    if not room:
        return False
    return agent_id in (room.get("agents") or []) or agent_id in (room.get("order") or [])


def sanitize_message(text, max_length=50000):
    if not text or not isinstance(text, str):
        raise ValueError("message must be a non-empty string")
    text = text.strip().replace("\x00", "")
    if not text:
        raise ValueError("message must be a non-empty string")
    if len(text) > max_length:
        raise ValueError("message exceeds max length ({})".format(max_length))
    return text
