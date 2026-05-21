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
import copy
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from lock import lock_file, unlock_file

ARCHIVE_MSG_LIMIT = 60
ARCHIVE_IDLE_MINUTES = 30


# ─── 配置 ─────────────────────────────────────────────

def load_config(config_path):
    """读取配置文件。失败时抛出异常，不调用 sys.exit。"""
    errors = []
    # 先尝试 yaml，失败再尝试 json
    try:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        pass
    except (FileNotFoundError, PermissionError) as e:
        errors.append(str(e))
    except Exception as e:
        errors.append(str(e))
    # json fallback
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        errors.append(str(e))
    raise RuntimeError(f"cannot load config {config_path}: {'; '.join(errors)}")


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
    raw = cursor_file.read_text(encoding="utf-8").strip()
    if cursor_type == "timestamp":
        if not raw:
            return None
        try:
            return datetime.strptime(raw.split("|", 1)[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    try:
        return int(raw)
    except ValueError:
        return 0


def write_cursor(cursor_file, cursor_type, value):
    if cursor_type == "timestamp":
        if isinstance(value, tuple):
            ts, line_no = value
            text = f"{ts.strftime('%Y-%m-%d %H:%M:%S')}|{line_no}" if ts else ""
        else:
            text = value.strftime("%Y-%m-%d %H:%M:%S") if value else ""
        cursor_file.write_text(text, encoding="utf-8")
    else:
        cursor_file.write_text(str(value), encoding="utf-8")


def read_cursor_state(cursor_file, cursor_type):
    """Return cursor with tie-break data for internal delivery checks."""
    if cursor_type != "timestamp":
        return read_cursor(cursor_file, cursor_type)
    if not cursor_file.exists():
        return (None, 0)
    raw = cursor_file.read_text(encoding="utf-8").strip()
    if not raw:
        return (None, 0)
    ts_raw, sep, line_raw = raw.partition("|")
    try:
        ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return (None, 0)
    if not sep:
        return (ts, 0)
    try:
        line_no = int(line_raw)
    except ValueError:
        line_no = 0
    return (ts, line_no)


# ─── Webhook 唤醒 ─────────────────────────────────────

def wakeup_agent(wakeup_cfg, message_text, from_agent):
    url = wakeup_cfg.get("url", "")
    if not url:
        return (False, "wakeup URL is empty")
    if not url.startswith(("http://", "https://")):
        return (False, f"unsupported URL scheme: {url}")

    body = build_body(wakeup_cfg.get("body_template", {}), message_text, from_agent)
    method = wakeup_cfg.get("method", "POST").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return (False, f"unsupported HTTP method: {method}")
    headers = dict(wakeup_cfg.get("headers", {}))
    if method != "GET":
        headers.setdefault("Content-Type", "application/json")

    auth = wakeup_cfg.get("auth", {})
    if auth.get("type") == "bearer":
        token = resolve_token(auth)
        if token:
            headers["Authorization"] = f"Bearer {token}"

    payload = None
    if method == "GET":
        if isinstance(body, dict) and body:
            query = urllib.parse.urlencode({
                k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                for k, v in body.items()
            })
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{query}"
    else:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return (True, f"status={resp.status}")
    except urllib.error.HTTPError as e:
        return (False, f"HTTP {e.code}: {e.read().decode(errors='replace')[:100]}")
    except Exception as e:
        return (False, str(e))


def build_body(template, message_text, from_agent):

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
    token_env = auth_cfg.get("token_env")
    if token_env:
        return os.environ.get(token_env) or None

    token_path = auth_cfg.get("token_path")
    jsonpath = auth_cfg.get("token_jsonpath", "")
    if not token_path:
        return None
    p = resolve_path(token_path)
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


# ─── 消息文件 ──────────────────────────────────────────

def parse_jsonl(filepath):
    if not filepath or not filepath.exists():
        return []
    results = []
    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # skip malformed lines
    except OSError:
        pass
    return results


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

    # Acquire BOTH locks to prevent race conditions:
    # .active.lock prevents concurrent writes to active.jsonl
    # .archive.lock prevents concurrent archive operations
    active_lock_path = shared_dir / ".active.lock"
    archive_lock_path = shared_dir / ".archive.lock"
    shared_dir.mkdir(parents=True, exist_ok=True)

    with open(archive_lock_path, "w", encoding="utf-8") as arch_lockf:
        lock_file(arch_lockf)
        try:
            with open(active_lock_path, "w", encoding="utf-8") as act_lockf:
                lock_file(act_lockf)
                try:
                    # Re-check after acquiring lock — another agent may have archived
                    if not active_file.exists():
                        return None

                    history_dir = shared_dir / "history"
                    history_dir.mkdir(parents=True, exist_ok=True)
                    archive_path = _next_archive_path(history_dir)
                    archive_name = archive_path.name
                    try:
                        shutil.move(str(active_file), str(archive_path))
                        # 创建新的空 active.jsonl，避免存在性检查空窗
                        active_file.write_text("", encoding="utf-8")
                        return archive_name
                    except OSError:
                        return None
                finally:
                    unlock_file(act_lockf)
        finally:
            unlock_file(arch_lockf)


def _next_archive_path(history_dir):
    stem = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
    archive_path = history_dir / f"{stem}.jsonl"
    suffix = 1
    while archive_path.exists():
        archive_path = history_dir / f"{stem}_{suffix}.jsonl"
        suffix += 1
    return archive_path


# ─── 核心轮询（可导入） ──────────────────────────────

def _get_retry_count(config, my_agent):
    """Determine retry count from wakeup.retry, env AGENT_BRIDGE_RETRY, or default 1."""
    wakeup_cfg = my_agent.get("wakeup", {})
    retry = wakeup_cfg.get("retry", None)
    if retry is not None:
        try:
            return max(0, int(retry))
        except (TypeError, ValueError):
            return 1
    env_val = os.environ.get("AGENT_BRIDGE_RETRY", "")
    if env_val.strip():
        try:
            return max(0, int(env_val))
        except ValueError:
            pass
    return 1


def _messages_after_cursor(msgs, filter_from, cursor_type, cursor, my_id=""):
    new_msgs = []
    ts_cursor = None
    line_cursor = 0
    if cursor_type == "timestamp":
        ts_cursor, line_cursor = cursor
    for i, msg in enumerate(msgs):
        msg_from = msg.get("from", "")
        if filter_from and msg_from != filter_from:
            continue
        if not filter_from and my_id and msg_from == my_id:
            continue
        line_no = i + 1
        if cursor_type == "line":
            if line_no > cursor:
                new_msgs.append(msg)
            continue

        ts_str = msg.get("ts", "")
        if not ts_str:
            continue
        try:
            msg_ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts_cursor is None or msg_ts > ts_cursor or (msg_ts == ts_cursor and line_no > line_cursor):
            new_msgs.append(msg)
    return new_msgs


def _has_pending_messages(config, shared_dir, msgs):
    for aid, acfg in config.get("agents", {}).items():
        filter_from = acfg.get("filter_from", "")
        cursor_type = acfg.get("cursor", "line")
        cursor_file = get_cursor_file(shared_dir, aid, cursor_type)
        cursor = read_cursor_state(cursor_file, cursor_type)
        if _messages_after_cursor(msgs, filter_from, cursor_type, cursor, aid):
            return True
    return False


def _archive_if_ready(config, shared_dir, active_file, msgs, result):
    if not should_archive(active_file):
        return result
    if _has_pending_messages(config, shared_dir, msgs):
        return result

    name = do_archive(shared_dir)
    result["archived"] = name
    if name:
        for aid, acfg in config.get("agents", {}).items():
            ctype = acfg.get("cursor", "line")
            cf = get_cursor_file(shared_dir, aid, ctype)
            write_cursor(cf, ctype, 0 if ctype == "line" else None)
    else:
        result["ok"] = False
        result["error"] = "archive failed"
    return result


def run_poll(config):
    """
    执行一次轮询。返回 dict:
      ok: bool             — 是否无错误完成
      archived: str|None   — 归档文件名（如有）
      new_msgs: int        — 发现的新消息数
      delivered: bool      — 是否成功通知对方
      error: str           — 错误信息
      to_agent: str        — 被唤醒的本机 agent
      from_agent: str      — 消息来源 agent
      retries: int         — webhook 重试次数
    """
    result = {
        "ok": False,
        "archived": None,
        "new_msgs": 0,
        "delivered": False,
        "error": "",
        "to_agent": "",
        "from_agent": "",
        "retries": 0,
    }

    shared_dir = resolve_path(config.get("shared_dir", "~/.agent-bridge"))
    active_file = shared_dir / "active.jsonl"

    if not active_file.exists():
        result["ok"] = True
        return result

    # ── 消息检查 ──
    agents = config.get("agents", {})
    my_id = config.get("agent_id", "")
    my_agent = agents.get(my_id, {})
    cursor_type = my_agent.get("cursor", "line")
    filter_from = my_agent.get("filter_from", "")

    if not my_id:
        result["error"] = "agent_id must be configured"
        return result

    cursor_file = get_cursor_file(shared_dir, my_id, cursor_type)

    msgs = parse_jsonl(active_file)
    if not msgs:
        result["ok"] = True
        return result

    cursor = read_cursor_state(cursor_file, cursor_type)
    new_msgs = _messages_after_cursor(msgs, filter_from, cursor_type, cursor, my_id)

    result["new_msgs"] = len(new_msgs)
    source_agent = filter_from or ",".join(sorted({m.get("from", "") for m in new_msgs if m.get("from")}))
    result["from_agent"] = source_agent
    result["to_agent"] = my_id

    if not new_msgs:
        result["ok"] = True
        return _archive_if_ready(config, shared_dir, active_file, msgs, result)

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
        delivered, msg = wakeup_agent(wakeup_cfg, combined, source_agent)
        if delivered:
            break
        last_err = msg

    result["retries"] = attempts - 1
    result["delivered"] = delivered

    # Only update cursor on successful delivery
    if delivered:
        if cursor_type == "line":
            write_cursor(cursor_file, cursor_type, len(msgs))
        else:
            latest_ts = new_msgs[-1].get("ts", "")
            try:
                write_cursor(cursor_file, cursor_type,
                             (datetime.strptime(latest_ts, "%Y-%m-%d %H:%M:%S"), len(msgs)))
            except ValueError:
                pass
        result["ok"] = True
        _archive_if_ready(config, shared_dir, active_file, msgs, result)
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
