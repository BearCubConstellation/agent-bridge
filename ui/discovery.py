#!/usr/bin/env python3
"""
Agent Bridge — Agent 发现模块

导出: discover_local_agents() — 扫描本地已安装的 AI Agent。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # ui/ itself for intra-package imports
from send import validate_agent_id
from poll import parse_jsonl
from adapters import adapter_capability, adapter_to_wakeup, wakeup_to_adapter

from config import (
    _agent_source_dir,
    _apply_bearer_secret_ref,
    _read_yaml_file,
    read_bridge,
)


def _classify_conn_error(detail, url):
    """Convert low-level connection errors into short UI messages."""
    d = detail.lower()
    if "10061" in d or "connection refused" in d or "errno 111" in d:
        return "Connection refused: target service is not running or the port is wrong"
    if "timed out" in d or "10060" in d or "errno 110" in d:
        return "Connection timed out: check the target address and network"
    if "name or service not known" in d or "11001" in d or "getaddrinfo" in d:
        return "Host name could not be resolved"
    if detail.startswith("HTTP "):
        code = detail.split("HTTP ", 1)[1].split(":")[0].strip()
        return f"HTTP error {code}"
    return detail


def _probe_http_reachable(url, timeout=10):
    try:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"status={resp.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def _default_wakeup():
    return {
        "url": "",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body_template": {"message": "{{message}}"},
    }


def _discovered_agent(agent_id, display_name, kind, source, details="", wakeup=None, adapter_override=None):
    item = {
        "id": agent_id,
        "display_name": display_name,
        "kind": kind,
        "source_dir": _agent_source_dir(source),
        "details": details,
    }
    if adapter_override:
        item["adapter"] = adapter_override
        item["wakeup"] = adapter_to_wakeup(adapter_override) if adapter_override.get("type") == "native_http" else {}
    elif wakeup:
        item["wakeup"] = wakeup
        item["adapter"] = wakeup_to_adapter(wakeup)
    else:
        item["adapter"] = {"type": "manual", "config": {}, "auth": {}, "template": {}}
    item["capability"] = adapter_capability(item)
    item["health"] = item["capability"]["health"]
    return item


def discover_local_agents(shared_dir, include_bridge_config=True):
    """Return likely local AI agents without modifying bridge.yaml.

    The scan is intentionally shallow: it checks known per-user config
    locations and bridge message history, avoiding broad filesystem walks.
    """
    shared = Path(shared_dir)
    home = Path.home()
    found = {}

    def add(item):
        if validate_agent_id(item["id"]):
            found.setdefault(item["id"], item)

    # Agents already participating in the current bridge conversation.
    for m in parse_jsonl(shared / "active.jsonl"):
        aid = str(m.get("from", "")).strip()
        if aid:
            add(_discovered_agent(
                aid,
                aid.capitalize(),
                "Bridge 消息",
                shared / "active.jsonl",
                "active.jsonl 中出现过的发送者",
            ))

    if include_bridge_config:
        cfg, cfg_path = read_bridge(shared)
        for key, agent in cfg.get("agents", {}).items():
            aid = agent.get("id", key)
            item = _discovered_agent(
                aid,
                agent.get("display_name", aid),
                "Bridge 配置",
                cfg_path,
                "当前 bridge.yaml 中已配置",
                agent.get("wakeup", {}),
            )
            if agent.get("sample"):
                item["sample"] = True
                item["details"] = "示例 Agent，尚未连接真实程序"
            add(item)

    # Hermes Agent.
    hermes_config = home / ".hermes" / "config.yaml"
    if hermes_config.exists():
        cfg = _read_yaml_file(hermes_config)
        webhook = ((cfg.get("platforms") or {}).get("webhook") or {})
        extra = webhook.get("extra") or {}
        host = extra.get("host", "127.0.0.1")
        port = extra.get("port", 8644)
        routes = extra.get("routes") or {}
        route = "agent-reply" if "agent-reply" in routes else (next(iter(routes), "agent-reply"))
        wakeup = {
            "url": f"http://{host}:{port}/webhooks/{route}",
            "method": "POST",
            "body_template": {"message": "{{message}}"},
        }
        secret_source = ""
        raw_secret = webhook.get("secret") or ""
        secret_source = _apply_bearer_secret_ref(wakeup, raw_secret)
        route_cfg = routes.get(route, {}) if isinstance(routes.get(route), dict) else {}
        if not secret_source:
            raw_secret = route_cfg.get("secret", "")
            secret_source = _apply_bearer_secret_ref(wakeup, raw_secret)
        details = "检测到 ~/.hermes/config.yaml"
        if secret_source == "literal":
            details += "；secret 为明文配置，未自动导入"
        add(_discovered_agent(
            "hermes",
            "Hermes Agent",
            "Hermes",
            hermes_config,
            details,
            wakeup,
        ))
    elif (home / ".hermes").exists():
        add(_discovered_agent("hermes", "Hermes Agent", "Hermes", home / ".hermes", "检测到 ~/.hermes 目录"))

    # OpenClaw.
    openclaw_config = home / ".openclaw" / "openclaw.json"
    if openclaw_config.exists() or (home / ".openclaw").exists():
        auth_cfg = {"type": "bearer", "token_path": str(home / ".openclaw" / "openclaw.json")}
        # 根据实际认证模式推断 JSONPath
        if openclaw_config.exists():
            try:
                oc_data = json.loads(openclaw_config.read_text(encoding="utf-8"))
                oc_auth = (oc_data.get("gateway") or {}).get("auth") or {}
                mode = oc_auth.get("mode", "")
                if mode in ("token", "password"):
                    auth_cfg["token_jsonpath"] = f"gateway.auth.{mode}"
                elif oc_auth.get("password"):
                    auth_cfg["token_jsonpath"] = "gateway.auth.password"
                elif oc_auth.get("token"):
                    auth_cfg["token_jsonpath"] = "gateway.auth.token"
            except Exception:
                pass
        if "token_jsonpath" not in auth_cfg:
            auth_cfg["token_jsonpath"] = "gateway.auth.token"

        add(_discovered_agent(
            "openclaw",
            "OpenClaw",
            "OpenClaw",
            openclaw_config if openclaw_config.exists() else home / ".openclaw",
            "检测到 OpenClaw 本地配置",
            {
                "url": "http://127.0.0.1:18789/tools/invoke",
                "method": "POST",
                "auth": auth_cfg,
                "body_template": {
                    "tool": "sessions_send",
                    "args": {"sessionKey": "agent:main:main", "message": "{{message}}"},
                },
            },
            adapter_override={
                "type": "openclaw_sessions",
                "config": {
                    "url": "http://127.0.0.1:18789/tools/invoke",
                    "sessions_key": "agent:main:main",
                    "timeout": 60,
                },
                "auth": auth_cfg,
                "response": {"mode": "callback", "timeout_seconds": 180},
            },
        ))

    known_dirs = [
        ("claude-code", "Claude Code", "Claude", home / ".claude"),
        ("codex", "Codex", "Codex", home / ".codex"),
        ("gemini", "Gemini CLI", "Gemini", home / ".gemini"),
        ("qwen", "Qwen Code", "Qwen", home / ".qwen"),
    ]
    for aid, name, kind, path in known_dirs:
        if path.exists():
            add(_discovered_agent(aid, name, kind, path, f"检测到 {path.name} 目录"))

    return sorted(found.values(), key=lambda x: (x["kind"].lower(), x["id"].lower()))
