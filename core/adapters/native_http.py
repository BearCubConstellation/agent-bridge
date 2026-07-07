#!/usr/bin/env python3
"""Native HTTP adapter — delivers messages via HTTP webhook.

Supports bearer token auth with env-var, file-path, and jsonpath resolution.
Uses urllib.request to avoid third-party dependencies.
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
from protocol import (                                        # noqa: E402
    make_delivery_ticket, make_capability,
    RESPONSE_CALLBACK, ADAPTER_NATIVE_HTTP,
)


# ── 通用回写指令注入（提炼自 openclaw_sessions） ──────────
# 让任何 HTTP-only Agent 收到消息后，都知道怎么把回复送回房间。
# 不依赖 Agent 自带 MCP 客户端 — 它只要能按指令 POST 或写文件即可。

def build_callback_instruction(room_id, turn_id, correlation_id, callback_url):
    """构造一段追加到消息正文末尾的回写说明。

    Returns "" 当缺少必要字段（如尚未进入 turn），不追加。
    """
    if not (callback_url and turn_id):
        return ""
    return (
        f"\n\n[Agent Bridge 回写指令]\n"
        f"room_id={room_id}\n"
        f"turn_id={turn_id}\n"
        f"correlation_id={correlation_id}\n"
        f"callback_url={callback_url}\n"
        f"\n你必须在生成最终回复后，把回复写回 Agent Bridge（任选其一）：\n"
        f"  1. 【推荐】POST 到上面的 callback_url，请求体 JSON：\n"
        f'     {{"turn_id": "{turn_id}", "correlation_id": "{correlation_id}", "message": "你的回复内容"}}\n'
        f"  2. 调用 Agent Bridge 的 MCP tool: agent_bridge.reply_turn\n"
        f"     （参数 room_id / agent_id / message / turn_id）\n"
        f"  3. 写文件到房间的 file_outbox 目录\n"
        f"\n注意：直接在本地会话里回答，对方 Agent 收不到。\n"
    )


def _resolve_path(p):
    """Expand env vars and user home in a path string."""
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def _resolve_token(auth_cfg):
    """Resolve a bearer token from auth config.

    Supports:
      - token_env: read from environment variable
      - token_path [+ token_jsonpath]: read from file, optionally extract
        a nested value via dot-separated jsonpath
    """
    token_env = auth_cfg.get("token_env")
    if token_env:
        return os.environ.get(token_env) or None

    token_path = auth_cfg.get("token_path")
    jsonpath = auth_cfg.get("token_jsonpath", "")
    if not token_path:
        return None
    p = _resolve_path(token_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return p.read_text(encoding="utf-8").strip()
    if jsonpath:
        parts = jsonpath.split(".")
        val = data
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part, "")
            else:
                return None
        return str(val) if val else None
    return str(data) if isinstance(data, str) else json.dumps(data, ensure_ascii=False)


@register_adapter
class NativeHttpAdapter(BaseAdapter):
    """Deliver messages to an agent via HTTP(S) webhook."""

    type: str = "native_http"

    # ── capability ───────────────────────────────────────

    def capability(self, agent_cfg: dict) -> dict:
        cfg = self.normalize_config(agent_cfg)
        url = cfg.get("url", "")
        return make_capability(
            adapter_type=ADAPTER_NATIVE_HTTP,
            configured=bool(url),
            automatic=bool(url),
            wake_modes=["http"],
            response_modes=["callback"],
            health="configured" if url else "missing_config",
        )

    # ── wake ─────────────────────────────────────────────

    def wake(self, delivery_request: dict) -> dict:
        """Render body_template with context, make HTTP request, return ticket.

        通用回写指令注入：所有 HTTP Agent 收到的消息都自带回写说明，
        让它们知道如何把回复送回房间（POST callback / MCP / file outbox）。
        这是从 openclaw_sessions 提炼的通用能力，让任何 HTTP-only Agent
        都能无需专属适配器就接入 Agent Bridge。
        """
        agent_id = delivery_request.get("agent_id", "")
        message = delivery_request.get("message", "")
        turn_id = delivery_request.get("turn_id", "")
        correlation_id = delivery_request.get("correlation_id", "")
        callback_url = delivery_request.get("callback_url", "")
        room_id = delivery_request.get("room_id", "")
        from_agents = delivery_request.get("from", "")

        # Resolve adapter config from delivery_request room context
        # The caller is expected to pass adapter config in delivery_request
        adapter_cfg = delivery_request.get("adapter", {})
        cfg = adapter_cfg.get("config", {})
        template = adapter_cfg.get("template", {"message": "{{message}}"})
        auth = adapter_cfg.get("auth", {})

        # 是否注入回写指令（默认 true，可在 agent config 里用 inject_callback=false 关掉）
        inject_callback = cfg.get("inject_callback", True)
        if inject_callback:
            instruction = build_callback_instruction(
                room_id, turn_id, correlation_id, callback_url
            )
            if instruction:
                message = message + instruction

        url = cfg.get("url", "")
        method = cfg.get("method", "POST").upper()
        headers = dict(cfg.get("headers", {}))
        timeout = int(cfg.get("timeout", 60))

        if not url:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_NATIVE_HTTP,
                response_mode=RESPONSE_CALLBACK,
                error="native_http adapter: url is empty",
            )

        # Build template context
        context = {
            "message": message,
            "from": from_agents,
            "agent_id": agent_id,
            "room_id": room_id,
            "turn_id": turn_id,
            "correlation_id": correlation_id,
            "callback_url": callback_url,
        }

        # Render body from template
        body = render_template(template, context)

        # Prepare headers
        if method != "GET":
            headers.setdefault("Content-Type", "application/json")

        # Auth: bearer token
        if auth.get("type") == "bearer":
            token = _resolve_token(auth)
            if token:
                headers["Authorization"] = f"Bearer {token}"

        # Build request
        payload = None
        if method == "GET":
            if isinstance(body, dict) and body:
                query = urllib.parse.urlencode({
                    k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    for k, v in body.items()
                })
                sep = "&" if urllib.parse.urlparse(url).query else "?"
                url = f"{url}{sep}{query}"
        else:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=payload, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(65536).decode("utf-8", errors="replace")
                return make_delivery_ticket(
                    ok=True,
                    delivery_request=delivery_request,
                    adapter_type=ADAPTER_NATIVE_HTTP,
                    response_mode=RESPONSE_CALLBACK,
                    detail=f"status={resp.status}",
                    raw_response=raw,
                )
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")[:200]
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_NATIVE_HTTP,
                response_mode=RESPONSE_CALLBACK,
                error=f"HTTP {e.code}: {err_body}",
            )
        except Exception as e:
            return make_delivery_ticket(
                ok=False,
                delivery_request=delivery_request,
                adapter_type=ADAPTER_NATIVE_HTTP,
                response_mode=RESPONSE_CALLBACK,
                error=str(e),
            )

    # ── normalize_config ─────────────────────────────────

    def normalize_config(self, agent_cfg: dict) -> dict:
        """Extract adapter config from new-style adapter or legacy wakeup."""
        agent_cfg = agent_cfg or {}
        adapter = agent_cfg.get("adapter")
        if adapter and isinstance(adapter, dict):
            cfg = dict(adapter.get("config", {}))
            cfg["type"] = adapter.get("type", ADAPTER_NATIVE_HTTP)
            cfg["template"] = adapter.get("template", {"message": "{{message}}"})
            cfg["auth"] = adapter.get("auth", {})
            return cfg

        # Legacy wakeup config
        wakeup = agent_cfg.get("wakeup") or {}
        if not wakeup:
            return {"type": "manual"}

        return {
            "type": ADAPTER_NATIVE_HTTP,
            "url": wakeup.get("url", ""),
            "method": wakeup.get("method", "POST"),
            "headers": wakeup.get("headers", {}),
            "template": wakeup.get("body_template", {"message": "{{message}}"}),
            "auth": wakeup.get("auth", {}),
            "retry": wakeup.get("retry"),
        }
