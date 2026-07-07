#!/usr/bin/env python3
"""OpenClaw Sessions adapter — delivers via OpenClaw sessions_send tool format.

Specialized HTTP adapter that includes turn_id, correlation_id, and
callback_url in the message payload for session-based communication.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from adapters.base import BaseAdapter, register_adapter      # noqa: E402
from adapters._legacy import render_template                  # noqa: E402
from adapters.native_http import _resolve_token               # noqa: E402
from protocol import (                                        # noqa: E402
    make_delivery_ticket, make_capability,
    RESPONSE_CALLBACK, RESPONSE_PULL_SESSION,
    ADAPTER_OPENCLAW_SESSIONS,
)

# ── OpenClaw 工具名自动探测 ─────────────────────────────
# 不同版本/发行版的 OpenClaw，发消息的工具名不一致。
# 候选清单按优先级排序，扫描时按序探测第一个可用的。
_CANDIDATE_TOOLS = [
    "sessions_send",       # 经典 OpenClaw
    "send_message",        # 较新版 OpenClaw
    "session_send",        # 拼写变体
    "chat",                # 简化接口
    "message_send",
]

# 探测端点（按优先级）
_PROBE_PATHS = ["/tools", "/tools/list", "/api/tools", "/mcp/tools"]


def _extract_base_url(url):
    """从 http://host:port/tools/invoke 提取 http://host:port"""
    from urllib.parse import urlparse
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _looks_like_openclaw_tool_list(raw):
    """启发式判断一个响应是不是工具清单（避免把 HTML 错误页当 JSON）"""
    if not raw:
        return False
    s = raw.lstrip()
    if not (s.startswith("[") or s.startswith("{")):
        return False
    low = s.lower()
    return "tool" in low or "name" in low or "session" in low


def probe_openclaw_tool(url, auth_cfg=None, timeout=5):
    """探测目标 OpenClaw 实例支持哪个发消息工具。

    返回工具名字符串；探测失败返回 None（调用方应回退到默认 sessions_send）。
    """
    base = _extract_base_url(url)
    # 1) 先用 /tools 端点拉清单（最权威）
    headers = {"Content-Type": "application/json"}
    if auth_cfg and auth_cfg.get("type") == "bearer":
        token = _resolve_token(auth_cfg)
        if token:
            headers["Authorization"] = f"Bearer {token}"

    for path in _PROBE_PATHS:
        try:
            req = urllib.request.Request(base + path, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(65536).decode("utf-8", errors="replace")
            if not _looks_like_openclaw_tool_list(raw):
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # 归一化成名字列表
            names = set()
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("tools") or data.get("data") or data.get("result") or []
                if isinstance(items, dict):
                    items = items.get("tools", [])
            else:
                items = []
            for item in items:
                if isinstance(item, dict):
                    n = item.get("name") or item.get("tool") or item.get("id")
                    if n:
                        names.add(str(n).lower())
                elif isinstance(item, str):
                    names.add(item.lower())
            # 匹配候选清单
            for cand in _CANDIDATE_TOOLS:
                if cand in names:
                    return cand
            # 工具列表拿到了但没有已知候选 → 列表里随便挑一个含 "send" / "message" / "session" / "chat" 的
            for n in sorted(names):
                if any(k in n for k in ("send", "message", "session", "chat")):
                    return n
        except Exception:
            continue

    # 2) /tools 拉不到 → 用 /tools/invoke 逐个试探（最贵，最后兜底）
    return None


@register_adapter
class OpenClawSessionsAdapter(BaseAdapter):
    """Deliver messages via OpenClaw sessions_send tool format."""

    type: str = "openclaw_sessions"

    # ── capability ───────────────────────────────────────

    def capability(self, agent_cfg: dict) -> dict:
        cfg = self.normalize_config(agent_cfg)
        url = cfg.get("url", "")
        sessions_key = cfg.get("sessions_key", "") or cfg.get("sessionsKey", "")
        configured = bool(url and sessions_key)
        return make_capability(
            adapter_type=ADAPTER_OPENCLAW_SESSIONS,
            configured=configured,
            automatic=True,
            wake_modes=["http"],
            response_modes=["callback", "pull_session"],
            requires_callback_url=True,
            supports_active_push=True,
            health="configured" if configured else "missing_config",
        )

    # ── wake ─────────────────────────────────────────────

    def wake(self, delivery_request: dict) -> dict:
        """Build OpenClaw sessions_send payload and POST it."""
        agent_id = delivery_request.get("agent_id", "")
        message = delivery_request.get("message", "")
        turn_id = delivery_request.get("turn_id", "")
        correlation_id = delivery_request.get("correlation_id", "")
        callback_url = delivery_request.get("callback_url", "")
        room_id = delivery_request.get("room_id", "")
        from_agents = delivery_request.get("from", "")

        adapter_cfg = delivery_request.get("adapter", {})
        cfg = adapter_cfg.get("config", {})
        sessions_key = cfg.get("sessions_key", "") or cfg.get("sessionsKey", "")
        url = cfg.get("url", "")
        method = (cfg.get("method", "POST")).upper()
        headers = dict(cfg.get("headers", {}))
        timeout = int(cfg.get("timeout", 60))
        auth = adapter_cfg.get("auth", {})

        # Build message template with session-specific fields
        message_template = adapter_cfg.get("message_template", {})
        context = {
            "message": message,
            "from": from_agents,
            "agent_id": agent_id,
            "room_id": room_id,
            "turn_id": turn_id,
            "correlation_id": correlation_id,
            "callback_url": callback_url,
            "sessions_key": sessions_key,
        }

        # Build sessions_send tool format payload
        rendered = render_template(message_template, context) if message_template else {}

        # Construct the OpenClaw /tools/invoke payload with args wrapper
        # Build message with embedded callback instructions
        callback_instruction = ""
        if callback_url and turn_id:
            callback_instruction = (
                f"\n\n[Agent Bridge 回写指令]\n"
                f"room_id={room_id}\n"
                f"turn_id={turn_id}\n"
                f"correlation_id={correlation_id}\n"
                f"callback_url={callback_url}\n"
                f"\n你必须在生成最终回复后写回 Agent Bridge。\n"
                f"优先调用 MCP tool: agent_bridge.reply_turn。\n"
                f"如果无法调用 MCP，请 POST 到 callback_url，请求体：\n"
                f'{{"turn_id": "{turn_id}", "correlation_id": "{correlation_id}", "message": "你的回复内容"}}\n'
            )
        enriched_message = message + callback_instruction

        args_payload = {
            "sessionKey": sessions_key,
            "turn_id": turn_id,
            "correlation_id": correlation_id,
            "callback_url": callback_url,
            "message": enriched_message,
            "from": from_agents,
            "room_id": room_id,
        }
        # Merge rendered template fields into args
        if isinstance(rendered, dict):
            args_payload.update(rendered)

        # 工具名：优先用配置里的，没有就回退到经典 sessions_send
        tool_name = cfg.get("tool", "") or adapter_cfg.get("tool", "") or "sessions_send"

        if not url:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                response_mode=RESPONSE_CALLBACK,
                error="openclaw_sessions adapter: url is empty",
            )

        # Prepare headers
        headers.setdefault("Content-Type", "application/json")

        # Auth: bearer token
        if auth.get("type") == "bearer":
            token = _resolve_token(auth)
            if token:
                headers["Authorization"] = f"Bearer {token}"

        def _do_invoke(t_name):
            payload_body = {"tool": t_name, "args": args_payload}
            payload = json.dumps(payload_body, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            return urllib.request.urlopen(req, timeout=timeout)

        # 第一次尝试：用配置/默认工具名
        try:
            with _do_invoke(tool_name) as resp:
                raw = resp.read(65536).decode("utf-8", errors="replace")
                return make_delivery_ticket(
                    ok=True,
                    delivery_request=delivery_request,
                    adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                    response_mode=RESPONSE_CALLBACK,
                    detail=f"status={resp.status} tool={tool_name}",
                    raw_response=raw,
                )
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")[:300]
            # 关键容错：404 + "Tool not available" → 自动探测可用工具并重试一次
            if e.code == 404 and "tool not available" in err_body.lower():
                detected = probe_openclaw_tool(url, auth, timeout=5)
                if detected and detected != tool_name:
                    try:
                        with _do_invoke(detected) as resp:
                            raw = resp.read(65536).decode("utf-8", errors="replace")
                        return make_delivery_ticket(
                            ok=True,
                            delivery_request=delivery_request,
                            adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                            response_mode=RESPONSE_CALLBACK,
                            detail=f"status={resp.status} tool={detected} (auto-detected, was {tool_name})",
                            raw_response=raw,
                        )
                    except urllib.error.HTTPError as e2:
                        err_body = e2.read().decode(errors="replace")[:200]
                        return make_delivery_ticket(
                            ok=False,
                            delivery_request=delivery_request,
                            adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                            response_mode=RESPONSE_CALLBACK,
                            error=(
                                f"HTTP {e2.code}: {err_body}  "
                                f"(已自动探测工具名 {detected} 但仍失败；OpenClaw 似乎不支持任何已知发消息工具)"
                            ),
                        )
                    except Exception as e2:
                        return make_delivery_ticket(
                            ok=False,
                            delivery_request=delivery_request,
                            adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                            response_mode=RESPONSE_CALLBACK,
                            error=f"auto-detected tool '{detected}' but invoke failed: {e2}",
                        )
                else:
                    return make_delivery_ticket(
                        ok=False,
                        delivery_request=delivery_request,
                        adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                        response_mode=RESPONSE_CALLBACK,
                        error=(
                            f"HTTP 404: 工具 '{tool_name}' 不可用，且自动探测未找到替代工具。"
                            f"请确认 OpenClaw 服务正在运行，或检查其支持的工具列表。"
                        ),
                    )
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                response_mode=RESPONSE_CALLBACK,
                error=f"HTTP {e.code}: {err_body}",
            )
        except Exception as e:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_OPENCLAW_SESSIONS,
                response_mode=RESPONSE_CALLBACK,
                error=str(e),
            )

    # ── normalize_config ─────────────────────────────────

    def normalize_config(self, agent_cfg: dict) -> dict:
        """Handle new-style adapter config and legacy wakeup with tool/sessionsKey."""
        agent_cfg = agent_cfg or {}
        adapter = agent_cfg.get("adapter")
        if adapter and isinstance(adapter, dict):
            cfg = dict(adapter.get("config", {}))
            cfg["type"] = adapter.get("type", ADAPTER_OPENCLAW_SESSIONS)
            cfg["message_template"] = adapter.get("message_template", {})
            cfg["auth"] = adapter.get("auth", {})
            return cfg

        # Legacy wakeup config with tool/sessionsKey
        wakeup = agent_cfg.get("wakeup") or {}
        if not wakeup:
            return {"type": "manual"}

        return {
            "type": ADAPTER_OPENCLAW_SESSIONS,
            "url": wakeup.get("url", ""),
            "method": wakeup.get("method", "POST"),
            "headers": wakeup.get("headers", {}),
            "sessions_key": wakeup.get("sessionsKey", wakeup.get("sessions_key", "")),
            "tool": wakeup.get("tool", "sessions_send"),
            "message_template": wakeup.get("message_template", {}),
            "auth": wakeup.get("auth", {}),
            "retry": wakeup.get("retry"),
        }
