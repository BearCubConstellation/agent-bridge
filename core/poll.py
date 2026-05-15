#!/usr/bin/env python3
"""
Agent Bridge — 通用轮询检查脚本

检测对方 agent 的新消息并通过 webhook 唤醒。
无新消息时零 token 消耗退出。
检测到归档条件时自动归档 active.jsonl。

用法:
    python3 poll.py --config bridge.yaml --agent alice  # 一次检查（由 cron/launchd 每 3 分钟调度）

配置格式 (bridge.yaml):
    shared_dir: ~/.agent-bridge
    agent_id: alice       # 本机 agent 标识
    agents:
      alice:
        id: alice
        display_name: "Alice"
        color: "#ff6b6b"
        cursor: line              # line | timestamp
        filter_from: bob          # 只处理谁的消息
        wakeup:
          url: "http://127.0.0.1:8644/webhooks/agent-reply"
          method: POST
          headers:
            Content-Type: application/json
          body_template:
            message: "{{message}}"
      bob:
        id: bob
        display_name: "Bob"
        color: "#4ecdc4"
        cursor: timestamp
        filter_from: alice
        wakeup:
          url: "http://127.0.0.1:18789/tools/invoke"
          method: POST
          auth:
            type: bearer
            token_path: ~/.bob/config.json
            token_jsonpath: api_key
          headers:
            Content-Type: application/json
          body_template:
            tool: sessions_send
            args:
              sessionKey: agent:main:main
              message: "{{message}}"
"""
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path


ARCHIVE_MSG_LIMIT = 60          # 超过此条数归档
ARCHIVE_IDLE_MINUTES = 30       # 超过此空闲分钟归档


def load_config(config_path):
    """Load YAML config. Returns {} on any error."""
    try:
        try:
            import yaml
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except ImportError:
            import json as _json
            with open(config_path) as f:
                return _json.load(f)
    except (FileNotFoundError, PermissionError):
        print(f"ERROR: config file not found or not readable: {config_path}", file=sys.stderr)
        sys.exit(1)
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        print(f"ERROR: invalid config file: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: failed to load config: {e}", file=sys.stderr)
        sys.exit(1)


def resolve_path(p):
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


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


def wakeup_agent(wakeup_cfg, message_text, from_agent):
    url = wakeup_cfg.get("url", "")
    if not url:
        print("ERROR: wakeup URL is empty", file=sys.stderr)
        return False

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
            print(f"OK: delivered to {url} (status={resp.status})")
            return True
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code} from {url}: {e.read().decode()[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False


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
        print(f"WARN: token file not found: {p}", file=sys.stderr)
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


def parse_jsonl(filepath):
    """Parse a JSONL file into a list of dicts. Returns [] on error."""
    msgs = []
    if not filepath or not filepath.exists():
        return msgs
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (OSError, PermissionError) as e:
        print(f"WARN: cannot read {filepath}: {e}", file=sys.stderr)
    return msgs


def should_archive(active_file):
    """检查是否满足归档条件。"""
    msgs = parse_jsonl(active_file)
    if not msgs:
        return False

    if len(msgs) >= ARCHIVE_MSG_LIMIT:
        return True

    # 检查最后一条消息的时间
    last_ts = msgs[-1].get("ts", "")
    if not last_ts:
        return False
    try:
        last_dt = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False

    now = datetime.now()
    if now - last_dt > timedelta(minutes=ARCHIVE_IDLE_MINUTES):
        return True

    return False


def do_archive(shared_dir):
    """将 active.jsonl 移动到 history/ 目录。"""
    active_file = shared_dir / "active.jsonl"
    if not active_file.exists():
        return

    history_dir = shared_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d_%H%M")
    archive_name = f"{now}.jsonl"
    archive_path = history_dir / archive_name

    try:
        shutil.move(str(active_file), str(archive_path))
        print(f"Archived: {active_file.name} → {archive_name} ({archive_path.stat().st_size} bytes)")
    except OSError as e:
        print(f"WARN: archive failed: {e}", file=sys.stderr)


def check_for_messages(config):
    """Main check + archive logic."""
    shared_dir = resolve_path(config.get("shared_dir", "~/.agent-bridge"))
    active_file = shared_dir / "active.jsonl"

    if not active_file.exists():
        sys.exit(0)

    # ── 归档检查 ──
    if should_archive(active_file):
        do_archive(shared_dir)
        # 归档后没有 active 文件了，直接退出
        sys.exit(0)

    # ── 消息检查 ──
    agents = config.get("agents", {})
    my_id = config.get("agent_id", "")
    my_agent = agents.get(my_id, {})
    cursor_type = my_agent.get("cursor", "line")
    filter_from = my_agent.get("filter_from", "")

    if not my_id or not filter_from:
        print("ERROR: agent_id and filter_from must be configured", file=sys.stderr)
        sys.exit(1)

    cursor_file = get_cursor_file(shared_dir, my_id, cursor_type)
    cursor = read_cursor(cursor_file, cursor_type)

    msgs = parse_jsonl(active_file)
    if not msgs:
        sys.exit(0)

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

    if not new_msgs:
        sys.exit(0)

    # Update cursor
    if cursor_type == "line":
        write_cursor(cursor_file, cursor_type, len(msgs))
    else:
        latest_ts = new_msgs[-1].get("ts", "")
        try:
            write_cursor(cursor_file, cursor_type,
                         datetime.strptime(latest_ts, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            pass

    combined = "\n".join(m.get("msg", "") for m in new_msgs)
    wakeup_cfg = my_agent.get("wakeup", {})
    if not wakeup_cfg:
        print("ERROR: no wakeup configuration for agent", my_id, file=sys.stderr)
        sys.exit(1)

    success = wakeup_agent(wakeup_cfg, combined, filter_from)
    sys.exit(0 if success else 1)


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Agent Bridge — poll for new messages and auto-archive"
    )
    parser.add_argument("--config", "-c", required=True,
                        help="Path to bridge.yaml")
    parser.add_argument("--agent", "-a",
                        help="Agent ID to run as (overrides config top-level agent_id)")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.agent:
        config["agent_id"] = args.agent

    check_for_messages(config)


if __name__ == "__main__":
    main()
