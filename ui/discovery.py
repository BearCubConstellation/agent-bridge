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

# 顺带引入 OpenClaw 工具探测能力（让扫描时就能选定正确的工具名）
try:
    from adapters.openclaw_sessions import probe_openclaw_tool  # noqa: E402
except Exception:
    probe_openclaw_tool = None
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

        # 主动探测 OpenClaw 服务：选端口 + 探测可用工具名
        # 默认端口候选（不同发行版/版本可能用不同端口）
        openclaw_url = "http://127.0.0.1:18789/tools/invoke"
        detected_tool = "sessions_send"  # 默认值（最经典）
        probe_note = ""

        if probe_openclaw_tool and _probe_http_reachable("http://127.0.0.1:18789/", timeout=2):
            # 服务在跑 → 探测工具名
            try:
                t = probe_openclaw_tool("http://127.0.0.1:18789/tools/invoke", auth_cfg, timeout=4)
                if t:
                    detected_tool = t
                    probe_note = f"（已自动探测到工具：{t}）"
            except Exception:
                pass
        elif probe_openclaw_tool:
            # 18789 没响应，试几个其他常见端口
            for alt_port in (8765, 3000, 8080):
                if _probe_http_reachable(f"http://127.0.0.1:{alt_port}/", timeout=1):
                    openclaw_url = f"http://127.0.0.1:{alt_port}/tools/invoke"
                    try:
                        t = probe_openclaw_tool(openclaw_url, auth_cfg, timeout=3)
                        if t:
                            detected_tool = t
                            probe_note = f"（端口 {alt_port}，工具 {t}）"
                    except Exception:
                        pass
                    break
            else:
                probe_note = "（未检测到 OpenClaw 服务运行，添加后请先启动 OpenClaw 再测试）"

        add(_discovered_agent(
            "openclaw",
            "OpenClaw",
            "OpenClaw",
            openclaw_config if openclaw_config.exists() else home / ".openclaw",
            f"检测到 OpenClaw 本地配置{probe_note}",
            {
                "url": openclaw_url,
                "method": "POST",
                "auth": auth_cfg,
                "body_template": {
                    "tool": detected_tool,
                    "args": {"sessionKey": "agent:main:main", "message": "{{message}}"},
                },
            },
            adapter_override={
                "type": "openclaw_sessions",
                "config": {
                    "url": openclaw_url,
                    "sessions_key": "agent:main:main",
                    "tool": detected_tool,
                    "timeout": 60,
                },
                "auth": auth_cfg,
                "response": {"mode": "callback", "timeout_seconds": 180},
            },
        ))

    # MCP-capable Agent：Claude Code / OpenCode / Codex / Cursor / Gemini / Qwen
    # 这类 Agent 自带 MCP client，无需 HTTP webhook — 扫描即用，零配置接入。
    mcp_capable_agents = [
        ("claude-code", "Claude Code", "Coding", home / ".claude",      "claude.json"),
        ("opencode",    "OpenCode",    "Coding", home / ".opencode",    "config.json"),
        ("codex",       "Codex",       "Coding", home / ".codex",       None),
        ("cursor",      "Cursor",      "Coding", home / ".cursor",      "mcp.json"),
        ("gemini",      "Gemini CLI",  "General",home / ".gemini",      None),
        ("qwen",        "Qwen Code",   "Coding", home / ".qwen",        None),
    ]
    for aid, name, kind, path, _config_file in mcp_capable_agents:
        if path.exists():
            add(_discovered_agent(
                aid,
                name,
                kind,
                path,
                f"检测到 {path.name} — MCP 接入（无需 HTTP 配置）",
                wakeup=None,  # 不生成 HTTP webhook 配置
                adapter_override={
                    "type": "mcp_tool",  # 标记走 MCP 接入
                    "config": {},
                    "response": {"mode": "mcp_tool", "timeout_seconds": 300},
                },
            ))

    return sorted(found.values(), key=lambda x: (x["kind"].lower(), x["id"].lower()))
