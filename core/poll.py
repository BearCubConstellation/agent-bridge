#!/usr/bin/env python3
"""
Agent Bridge — 通用轮询检查脚本

检测对方 agent 的新消息并通过 webhook 唤醒。
无新消息时零 token 消耗退出。
检测到归档条件时自动归档 active.jsonl。

本模块也可被 server.py 导入，在 UI 内部执行定时轮询。

用法:
    python3 poll.py --config bridge.yaml --agent alice

导入:
    from poll import run_poll
    result = run_poll(config)
"""
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from lock import lock_file, unlock_file

ARCHIVE_MSG_LIMIT = 60
ARCHIVE_IDLE_MINUTES = 30


# ─── 配置 ─────────────────────────────────────────────

def load_config(config_path):
    # 先尝试 yaml，失败再尝试 json
    try:
        import yaml
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        pass
    except (FileNotFoundError, PermissionError):
        print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: invalid config: {e}", file=sys.stderr)
        sys.exit(1)
    # json fallback
    try:
        with open(config_path) as f:
            return json.load(f)
    except (FileNotFoundError, PermissionError):
        print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: invalid config: {e}", file=sys.stderr)
        sys.exit(1)


def resolve_path(p):
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


# ─── 游标 ─────────────────────────────────────────────

def get_cursor_file(shared_dir, agent_id, cursor_type):
    if cursor_type == "timestamp":
        return Path(shared_dir) / f".{agent_id}_ts_cursor"
    return Path(shared_dir) / f".{agent_id}_cursor"


def read_cursor(cursor_file, cursor_type):
    if not cursor_file.exists():
        return None if cursor_type == "timestamp" else 0
    raw = cursor_file.read_text().strip()
    if cursor_type == "timestamp":
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    try:
        return int(raw)
    except ValueError:
        return 0


def write_cursor(cursor_file, cursor_type, value):
    if cursor_type == "timestamp":
        cursor_file.write_text(value.strftime("%Y-%m-%d %H:%M:%S") if value else "")
    else:
        cursor_file.write_text(str(value))


# ─── Webhook 唤醒 ─────────────────────────────────────

def wakeup_agent(wakeup_cfg, message_text, from_agent):
    url = wakeup_cfg.get("url", "")
    if not url:
        return (False, "wakeup URL is empty")

    body = build_body(wakeup_cfg.get("body_template", {}), message_text, from_agent)
    headers = dict(wakeup_cfg.get("headers", {}))
    headers.setdefault("Content-Type", "application/json")

    auth = wakeup_cfg.get("auth", {})
    if auth.get("type") == "bearer":
        token = resolve_token(auth)
        if token:
            headers["Authorization"] = f"Bearer {token}"

    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return (True, f"status={resp.status}")
    except urllib.error.HTTPError as e:
        return (False, f"HTTP {e.code}: {e.read().decode()[:100]}")
    except Exception as e:
        return (False, str(e))


def build_body(template, message_text, from_agent):
    import copy

    def _sub(val):
        if isinstance(val, str):
            return val.replace("{{message}}", message_text).replace("{{from}}", from_agent)
        if isinstance(val, dict):
            return {k: _sub(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_sub(item) for item in val]
        return val

    return _sub(copy.deepcopy(template))


def resolve_token(auth_cfg):
    token_path = auth_cfg.get("token_path")
    jsonpath = auth_cfg.get("token_jsonpath", "")
    if not token_path:
        return None
    p = resolve_path(token_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return p.read_text().strip()
    if jsonpath:
        parts = jsonpath.split(".")
        val = data
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part, "")
            else:
                return None
        return str(val) if val else None
    return str(data) if isinstance(data, str) else json.dumps(data)


# ─── 消息文件 ──────────────────────────────────────────

def parse_jsonl(filepath):
    if not filepath or not filepath.exists():
        return []
    try:
        with open(filepath) as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


# ─── 归档 ──────────────────────────────────────────────

def should_archive(active_file):
    msgs = parse_jsonl(active_file)
    if not msgs:
        return False
    if len(msgs) >= ARCHIVE_MSG_LIMIT:
        return True
    last_ts = msgs[-1].get("ts", "")
    if not last_ts:
        return False
    try:
        last_dt = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    if datetime.now() - last_dt > timedelta(minutes=ARCHIVE_IDLE_MINUTES):
        return True
    return False


def do_archive(shared_dir):
    active_file = shared_dir / "active.jsonl"
    if not active_file.exists():
        return None

    # Use a separate lock file to prevent race conditions between agents
    lock_path = shared_dir / ".archive.lock"
    shared_dir.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lockf:
        lock_file(lockf)
        try:
            # Re-check after acquiring lock — another agent may have archived
            if not active_file.exists():
                return None

            history_dir = shared_dir / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now().strftime("%Y-%m-%d_%H%M")
            archive_name = f"{now}.jsonl"
            archive_path = history_dir / archive_name
            try:
                shutil.move(str(active_file), str(archive_path))
                return archive_name
            except OSError:
                return None
        finally:
            unlock_file(lockf)


# ─── 核心轮询（可导入） ──────────────────────────────

def _get_retry_count(config, my_agent):
    """Determine retry count from wakeup.retry, env AGENT_BRIDGE_RETRY, or default 1."""
    wakeup_cfg = my_agent.get("wakeup", {})
    retry = wakeup_cfg.get("retry", None)
    if retry is not None:
        return int(retry)
    env_val = os.environ.get("AGENT_BRIDGE_RETRY", "")
    if env_val.strip():
        try:
            return int(env_val)
        except ValueError:
            pass
    return 1


def run_poll(config):
    """
    执行一次轮询。返回 dict:
      ok: bool             — 是否无错误完成
      archived: str|None   — 归档文件名（如有）
      new_msgs: int        — 发现的新消息数
      delivered: bool      — 是否成功通知对方
      error: str           — 错误信息
      to_agent: str        — 消息发给了谁
      retries: int         — webhook 重试次数
    """
    result = {
        "ok": False,
        "archived": None,
        "new_msgs": 0,
        "delivered": False,
        "error": "",
        "to_agent": "",
        "retries": 0,
    }

    shared_dir = resolve_path(config.get("shared_dir", "~/.agent-bridge"))
    active_file = shared_dir / "active.jsonl"

    if not active_file.exists():
        result["ok"] = True
        return result

    # ── 归档 ──
    if should_archive(active_file):
        name = do_archive(shared_dir)
        result["ok"] = True
        result["archived"] = name
        result["error"] = "" if name else "archive failed"
        return result

    # ── 消息检查 ──
    agents = config.get("agents", {})
    my_id = config.get("agent_id", "")
    my_agent = agents.get(my_id, {})
    cursor_type = my_agent.get("cursor", "line")
    filter_from = my_agent.get("filter_from", "")

    if not my_id or not filter_from:
        result["error"] = "agent_id and filter_from must be configured"
        return result

    cursor_file = get_cursor_file(shared_dir, my_id, cursor_type)
    cursor = read_cursor(cursor_file, cursor_type)

    msgs = parse_jsonl(active_file)
    if not msgs:
        result["ok"] = True
        return result

    new_msgs = []
    for i, msg in enumerate(msgs):
        if msg.get("from") != filter_from:
            continue
        if cursor_type == "line":
            if i + 1 > cursor:
                new_msgs.append(msg)
        else:
            ts_str = msg.get("ts", "")
            if not ts_str:
                continue
            try:
                msg_ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if cursor is None or msg_ts > cursor:
                new_msgs.append(msg)

    result["new_msgs"] = len(new_msgs)

    if not new_msgs:
        result["ok"] = True
        return result

    # ── 唤醒（先尝试 webhook，再更新游标） ──
    combined = "\n".join(m.get("msg", "") for m in new_msgs)
    wakeup_cfg = my_agent.get("wakeup", {})
    if not wakeup_cfg:
        result["error"] = f"no wakeup configuration for {my_id}"
        return result

    max_retries = _get_retry_count(config, my_agent)
    delivered = False
    last_err = ""
    attempts = 0

    for attempt in range(1 + max_retries):
        attempts += 1
        delivered, msg = wakeup_agent(wakeup_cfg, combined, filter_from)
        if delivered:
            break
        last_err = msg

    result["retries"] = attempts - 1
    result["delivered"] = delivered
    result["to_agent"] = filter_from

    # Only update cursor on successful delivery
    if delivered:
        if cursor_type == "line":
            write_cursor(cursor_file, cursor_type, len(msgs))
        else:
            latest_ts = new_msgs[-1].get("ts", "")
            try:
                write_cursor(cursor_file, cursor_type,
                             datetime.strptime(latest_ts, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass
        result["ok"] = True
    else:
        result["ok"] = False
        result["error"] = last_err

    return result


# ─── CLI 入口 ─────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agent Bridge — poll")
    parser.add_argument("--config", "-c", required=True, help="Path to bridge.yaml")
    parser.add_argument("--agent", "-a",
                        help="Agent ID (overrides config top-level agent_id)")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.agent:
        config["agent_id"] = args.agent

    result = run_poll(config)
    if result["archived"]:
        print(f"Archived: {result['archived']}")
    elif result["delivered"]:
        print(f"Delivered {result['new_msgs']} message(s) to {result['to_agent']}")
    elif result["error"]:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
