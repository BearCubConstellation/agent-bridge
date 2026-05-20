#!/usr/bin/env python3
"""
Agent Bridge — 通用消息发送工具

向共享对话文件追加一条消息。
从 bridge.yaml 中读取 agent_id 和 shared_dir。

用法:
    python3 send.py "消息内容"                    # 从默认路径读取 bridge.yaml
    python3 send.py --bridge path.yaml "内容"
    python3 send.py --agent alice "内容"
    echo "消息" | python3 send.py                  # 管道输入
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from lock import file_lock
from rooms import append_room_message, validate_room_id

DEFAULT_BRIDGE_CANDIDATES = [
    Path.home() / ".agent-bridge" / "bridge.yaml",
    Path.home() / ".shared-chat" / "bridge.yaml",
]


def load_bridge_config(config_path=None):
    """读取 bridge.yaml。无参时自动搜索默认路径。"""
    if config_path:
        paths = [Path(config_path)]
    else:
        paths = DEFAULT_BRIDGE_CANDIDATES

    for p in paths:
        if p.exists():
            # 先尝试 yaml，失败再尝试 json
            try:
                import yaml
                with open(p, encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except ImportError:
                pass
            except Exception:
                pass
            # yaml 不可用或解析失败，尝试 json fallback
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
    return {}


def find_active_jsonl(shared_dir_str):
    """从 shared_dir 定位 active.jsonl。支持 ~ 和空值。"""
    if not shared_dir_str:
        for d in [Path.home() / ".agent-bridge", Path.home() / ".shared-chat"]:
            if (d / "active.jsonl").exists():
                return d / "active.jsonl"
        return Path.home() / ".agent-bridge" / "active.jsonl"
    sd = Path(os.path.expanduser(shared_dir_str))
    return sd / "active.jsonl"


def validate_agent_id(aid):
    """Agent ID 只能包含字母、数字、下划线、连字符。"""
    if not aid or not re.match(r'^[a-zA-Z0-9_-]+$', aid):
        return False
    return True


class InvalidAgentError(ValueError):
    """Raised when agent ID validation fails."""
    pass


def send(agent_id, active_file, text, quiet=False):
    """向 active.jsonl 追加一条消息。"""
    if not validate_agent_id(agent_id):
        raise InvalidAgentError(
            f"Invalid agent ID '{agent_id}'. Use only letters, numbers, hyphens, underscores."
        )

    active_file.parent.mkdir(parents=True, exist_ok=True)
    msg = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "from": agent_id,
        "msg": text,
    }
    lock_path = active_file.parent / ".active.lock"
    with file_lock(lock_path):
        with open(active_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    if not quiet:
        print(f"[{agent_id}] Message sent ({len(text)} chars)")


def main():
    parser = argparse.ArgumentParser(description="Agent Bridge — send a message")
    parser.add_argument("message", nargs="?", help="Message text")
    parser.add_argument("--bridge", "-b", dest="bridge_config",
                        help="Path to bridge.yaml (default: auto-search)")
    parser.add_argument("--agent", "-a", help="Override agent ID")
    parser.add_argument("--dir", "-d", help="Override shared directory")
    parser.add_argument("--room", help="Write to rooms/<room>/active.jsonl instead of legacy active.jsonl")
    parser.add_argument("--to", help="Optional target agent ID for room messages")
    parser.add_argument("--kind", default="agent", help="Optional room message kind")

    args = parser.parse_args()
    cfg = load_bridge_config(args.bridge_config)

    # Agent ID 优先级: CLI > 环境变量 > bridge.yaml.agent_id
    agent_id = args.agent
    if not agent_id:
        agent_id = os.environ.get("AGENT_ID")
    if not agent_id:
        agent_id = cfg.get("agent_id", "")
    if not agent_id:
        # 最后 fallback: 只有一个 agent 时用它的 id
        agent_dict = cfg.get("agents", {})
        if len(agent_dict) == 1:
            agent_id = next(iter(agent_dict.keys()))
        elif len(agent_dict) > 1:
            print(
                "Error: multiple agents configured. Use --agent to specify which one to send as.",
                file=sys.stderr,
            )
            sys.exit(1)
    if not agent_id:
        print(
            "Error: cannot determine agent ID. Set --agent, AGENT_ID, "
            "or add agent_id to bridge.yaml",
            file=sys.stderr,
        )
        sys.exit(1)

    # shared_dir 优先级: CLI > bridge.yaml > 自动 fallback
    shared_dir = args.dir or cfg.get("shared_dir", "")
    text = args.message
    if not text:
        text = sys.stdin.read().strip()
    if not text:
        print("Usage: send.py <message>  (or pipe via stdin)", file=sys.stderr)
        sys.exit(1)

    if args.room:
        if not validate_room_id(args.room):
            print("Error: invalid room ID", file=sys.stderr)
            sys.exit(1)
        root = Path(os.path.expandvars(os.path.expanduser(shared_dir or str(Path.home() / ".agent-bridge"))))
        append_room_message(root, args.room, agent_id, text, to_agent=args.to or "", kind=args.kind)
        print(f"[{agent_id}] Room message sent to {args.room} ({len(text)} chars)")
        return

    active_file = find_active_jsonl(shared_dir)
    send(agent_id, active_file, text)


if __name__ == "__main__":
    main()
