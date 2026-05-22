#!/usr/bin/env python3
"""Agent delivery adapters.

The adapter layer keeps the bridge core from assuming every agent exposes an
HTTP webhook. Existing ``wakeup`` configs are treated as ``native_http``.
"""
import json
import os
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from lock import file_lock
from poll import wakeup_agent


ADAPTER_TYPES = {"native_http", "cli", "stdio_shim", "file_inbox", "manual",
               "openclaw_sessions", "mcp_tool", "file_mailbox"}


def render_template(value, context):
    """Recursively replace ``{{name}}`` placeholders in strings."""
    if isinstance(value, str):
        result = value
        for key, raw in context.items():
            result = result.replace("{{" + key + "}}", "" if raw is None else str(raw))
        return result
    if isinstance(value, dict):
        return {k: render_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, context) for v in value]
    return value


def wakeup_to_adapter(wakeup_cfg):
    wakeup_cfg = wakeup_cfg or {}
    if not wakeup_cfg:
        return {"type": "manual", "config": {}, "auth": {}, "template": {}}
    return {
        "type": "native_http",
        "config": {
            "url": wakeup_cfg.get("url", ""),
            "method": wakeup_cfg.get("method", "POST"),
            "headers": wakeup_cfg.get("headers", {}),
            "retry": wakeup_cfg.get("retry", None),
        },
        "auth": wakeup_cfg.get("auth", {}),
        "template": wakeup_cfg.get("body_template", {"message": "{{message}}"}),
    }


def adapter_to_wakeup(adapter_cfg):
    adapter_cfg = normalize_adapter({"adapter": adapter_cfg})
    if adapter_cfg.get("type") != "native_http":
        return {}
    config = adapter_cfg.get("config", {})
    wakeup = {
        "url": config.get("url", ""),
        "method": config.get("method", "POST"),
        "headers": config.get("headers", {"Content-Type": "application/json"}),
        "body_template": adapter_cfg.get("template", {"message": "{{message}}"}),
    }
    if adapter_cfg.get("auth"):
        wakeup["auth"] = adapter_cfg["auth"]
    if config.get("retry") is not None:
        wakeup["retry"] = config["retry"]
    return wakeup


def normalize_adapter(agent_cfg):
    """Return a normalized adapter config for an agent entry."""
    agent_cfg = agent_cfg or {}
    adapter = dict(agent_cfg.get("adapter") or {})
    if not adapter:
        return wakeup_to_adapter(agent_cfg.get("wakeup") or {})

    adapter_type = adapter.get("type", "manual")
    # Check v2 registry first, then legacy set
    try:
        from adapters.base import get_adapter_class
        if get_adapter_class(adapter_type):
            pass  # Known v2 adapter type, keep it
        elif adapter_type not in ADAPTER_TYPES:
            adapter_type = "manual"
    except ImportError:
        if adapter_type not in ADAPTER_TYPES:
            adapter_type = "manual"
    adapter["type"] = adapter_type
    adapter.setdefault("config", {})
    adapter.setdefault("auth", {})
    adapter.setdefault("template", {"message": "{{message}}"})
    return adapter


def adapter_capability(agent_cfg):
    adapter = normalize_adapter(agent_cfg)
    adapter_type = adapter.get("type", "manual")

    # Delegate to v2 adapter class if registered
    try:
        from adapters.base import get_adapter_class
        cls = get_adapter_class(adapter_type)
        if cls:
            cap = cls().capability(agent_cfg)
            # Merge legacy fields for backward compat
            cap.setdefault("automatic", cap.get("automatic", True))
            cap.setdefault("configured", cap.get("configured", True))
            return cap
    except (ImportError, Exception):
        pass

    config = adapter.get("config", {})
    automatic = adapter_type not in {"manual"}
    configured = True

    if adapter_type == "native_http":
        configured = bool(config.get("url"))
    elif adapter_type in {"cli", "stdio_shim"}:
        configured = bool(config.get("command"))
    elif adapter_type == "file_inbox":
        configured = bool(config.get("path"))
    elif adapter_type == "manual":
        configured = False

    return {
        "type": adapter_type,
        "automatic": automatic and configured,
        "configured": configured,
        "health": "configured" if configured else "manual" if adapter_type == "manual" else "missing_config",
    }


def _command_args(command, context):
    rendered = render_template(command, context)
    if isinstance(rendered, list):
        return [str(part) for part in rendered]
    if isinstance(rendered, str):
        return shlex.split(rendered, posix=(os.name != "nt"))
    return []


def _deliver_cli(adapter, context):
    config = adapter.get("config", {})
    args = _command_args(config.get("command"), context)
    if not args:
        return False, "cli adapter command is empty"

    stdin_template = config.get("stdin", None)
    stdin_text = None
    if stdin_template is not None:
        stdin_text = render_template(stdin_template, context)

    timeout = int(config.get("timeout", 60))
    try:
        completed = subprocess.run(
            args,
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return False, str(exc)

    if completed.returncode == 0:
        return True, "exit=0"
    detail = (completed.stderr or completed.stdout or "").strip()
    return False, f"exit={completed.returncode}: {detail[:200]}"


def _deliver_file_inbox(adapter, context):
    config = adapter.get("config", {})
    path = config.get("path", "")
    if not path:
        return False, "file_inbox path is empty"
    inbox = Path(os.path.expandvars(os.path.expanduser(str(path))))
    inbox.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "from": context.get("from", ""),
        "room": context.get("room", ""),
        "msg": context.get("message", ""),
    }
    template = adapter.get("template")
    if template:
        rendered = render_template(template, context)
        if isinstance(rendered, dict):
            record.update(rendered)
    with file_lock(inbox.parent / ".agent-bridge-inbox.lock"):
        with open(inbox, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True, f"file={inbox}"


def deliver_to_adapter(agent_cfg, message_text, from_agent, context=None):
    """Deliver message text to an agent through its configured adapter.
    Returns (success, detail, response_body).
    """
    adapter = normalize_adapter(agent_cfg)
    context = dict(context or {})
    context.setdefault("message", message_text)
    context.setdefault("from", from_agent)

    adapter_type = adapter.get("type", "manual")
    if adapter_type == "manual":
        return False, "manual adapter cannot be auto-triggered", ""

    if adapter_type == "native_http":
        config = adapter.get("config", {})
        wakeup_cfg = {
            "url": config.get("url", ""),
            "method": config.get("method", "POST"),
            "headers": config.get("headers", {}),
            "auth": adapter.get("auth", {}),
            "body_template": render_template(adapter.get("template", {"message": "{{message}}"}), context),
        }
        if config.get("retry") is not None:
            wakeup_cfg["retry"] = config["retry"]
        return wakeup_agent(wakeup_cfg, message_text, from_agent)

    if adapter_type in {"cli", "stdio_shim"}:
        ok, detail = _deliver_cli(adapter, context)
        return ok, detail, ""

    if adapter_type == "file_inbox":
        ok, detail = _deliver_file_inbox(adapter, context)
        return ok, detail, ""

    return False, f"unsupported adapter type: {adapter_type}", ""
