#!/usr/bin/env python3
"""MCP Server — stdio JSON-RPC 2.0 server for Agent Bridge room interaction.

Agent 进程通过 stdio 连接本服务，调用 MCP Tools 实现交互：
- list_rooms, get_current_turn, read_messages, get_agent_pending
- reply_turn（回复当前轮次）, send_message（主动发消息）

协议：JSON-RPC 2.0 over stdin/stdout。
仅依赖 Python 标准库，不引入外部 MCP SDK。

用法：
    python3 core/mcp_server.py --shared-dir ~/.agent-bridge
    # 或通过环境变量 AGENT_BRIDGE_SHARED_DIR
"""

import json
import os
import sys
from pathlib import Path

# ── Local import compat ─────────────────────────────────
_parent = str(Path(__file__).resolve().parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from protocol import (                                    # noqa: E402
    make_message, gen_message_id, gen_correlation_id,
    MSG_KIND_AGENT, MSG_KIND_SYSTEM,
)
from rooms import (                                       # noqa: E402
    room_dir, room_active_file,
    read_room_messages, read_room_state,
    ensure_room, normalize_room,
    append_room_message,
)
from runtime import receive_agent_response                # noqa: E402
from security import (                                    # noqa: E402
    validate_room_id, validate_agent_id,
    agent_in_room, sanitize_message,
)


# ── JSON-RPC 2.0 Constants ──────────────────────────────

JSONRPC_VERSION = "2.0"

# Standard JSON-RPC error codes
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

# Application-level error codes (from -32000 to -32099 reserved)
ERR_ROOM_NOT_FOUND = -32001
ERR_AGENT_NOT_FOUND = -32002
ERR_NO_ACTIVE_TURN = -32003
ERR_TURN_MISMATCH = -32004
ERR_CONFIG_NOT_FOUND = -32005
ERR_VALIDATION = -32006


# ── Config Loading ──────────────────────────────────────

def _resolve_shared_dir(cli_arg=None):
    """解析 shared_dir 路径。

    优先级: CLI 参数 > 环境变量 AGENT_BRIDGE_SHARED_DIR > 默认路径
    """
    if cli_arg:
        return Path(os.path.expandvars(os.path.expanduser(str(cli_arg))))
    env = os.environ.get("AGENT_BRIDGE_SHARED_DIR")
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    return Path.home() / ".agent-bridge"


def load_bridge_config(shared_dir):
    """读取 bridge.yaml 配置。

    支持 YAML（需要 PyYAML）和 JSON 格式。
    返回 dict，失败时返回 None。
    """
    shared = Path(shared_dir)
    candidates = [
        shared / "bridge.yaml",
        shared / "bridge.json",
        Path.home() / ".agent-bridge" / "bridge.yaml",
        Path.home() / ".shared-chat" / "bridge.yaml",
    ]
    for config_path in candidates:
        if not config_path.exists():
            continue
        # 尝试 YAML
        try:
            import yaml
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if isinstance(cfg, dict):
                _log("配置加载成功: {}".format(config_path))
                return cfg
        except ImportError:
            pass
        except Exception as exc:
            _log("YAML 加载失败 {}: {}".format(config_path, exc))
        # JSON fallback
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                _log("配置加载成功 (JSON): {}".format(config_path))
                return cfg
        except Exception as exc:
            _log("JSON 加载失败 {}: {}".format(config_path, exc))
    return None


# ── Logging (stderr, to avoid corrupting stdout JSON-RPC) ──

def _log(msg):
    """输出日志到 stderr，避免干扰 stdout 上的 JSON-RPC 通信。"""
    print("[mcp_server] {}".format(msg), file=sys.stderr, flush=True)


# ── JSON-RPC Helpers ────────────────────────────────────

def _make_response(request_id, result):
    """构造 JSON-RPC 成功响应。"""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "result": result,
    }


def _make_error(request_id, code, message, data=None):
    """构造 JSON-RPC 错误响应。"""
    err = {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if data is not None:
        err["error"]["data"] = data
    return err


def _parse_request(line):
    """解析一行 JSON-RPC 请求，返回 (request, error) 之一为 None。"""
    try:
        req = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, _make_error(None, ERR_PARSE, "Parse error: {}".format(exc))
    if not isinstance(req, dict):
        return None, _make_error(None, ERR_INVALID_REQUEST, "Invalid Request: not a JSON object")
    if req.get("jsonrpc") != JSONRPC_VERSION:
        return None, _make_error(req.get("id"), ERR_INVALID_REQUEST,
                                  "Invalid Request: jsonrpc must be '2.0'")
    if "method" not in req:
        return None, _make_error(req.get("id"), ERR_INVALID_REQUEST,
                                  "Invalid Request: missing method")
    return req, None


def _get_param(params, key, default=None, required=False):
    """从 params dict 中提取参数，支持 required 校验。"""
    if not isinstance(params, dict):
        if required:
            raise ValueError("缺少参数: {}".format(key))
        return default
    val = params.get(key, default)
    if required and val is None:
        raise ValueError("缺少必需参数: {}".format(key))
    return val


# ── Tool Implementations ────────────────────────────────

def tool_list_rooms(config, shared_dir):
    """列出所有房间及其状态。

    MCP tool: agent_bridge.list_rooms
    Parameters: none
    """
    rooms_cfg = config.get("rooms", {})
    result = []
    for room_id, room_cfg in rooms_cfg.items():
        try:
            room = normalize_room({**room_cfg, "id": room_id})
            state = read_room_state(shared_dir, room_id, room)
            result.append({
                "id": room_id,
                "name": room.get("name", room_id),
                "status": state.get("status", "unknown"),
                "agents": room.get("order", []),
                "turn_count": state.get("turn_count", 0),
                "max_turns": state.get("max_turns", 50),
                "policy": room.get("policy", "round_robin"),
                "current_turn": state.get("current_turn"),
            })
        except Exception as exc:
            result.append({
                "id": room_id,
                "error": str(exc),
            })
    return {"rooms": result, "count": len(result)}


def tool_get_current_turn(shared_dir, room_id):
    """获取指定房间的当前轮次信息。

    MCP tool: agent_bridge.get_current_turn
    Parameters: room_id (required)
    """
    if not validate_room_id(room_id):
        raise ValueError("无效的房间 ID: {}".format(room_id))
    state = read_room_state(shared_dir, room_id)
    current_turn = state.get("current_turn")
    return {
        "room_id": room_id,
        "status": state.get("status", "unknown"),
        "current_turn": current_turn,
        "waiting_for": state.get("waiting_for", ""),
        "turn_index": state.get("turn_index", 0),
        "round": state.get("round", 0),
        "turn_count": state.get("turn_count", 0),
    }


def tool_read_messages(shared_dir, room_id, limit=100, after=None):
    """读取房间消息。

    MCP tool: agent_bridge.read_messages
    Parameters:
        room_id (required): 房间 ID
        limit (optional): 返回消息数量上限，默认 100
        after (optional): 只返回该时间之后的消息（ISO 格式或 "YYYY-MM-DD HH:MM:SS"）
    """
    if not validate_room_id(room_id):
        raise ValueError("无效的房间 ID: {}".format(room_id))
    try:
        limit = int(limit) if limit is not None else 100
    except (TypeError, ValueError):
        limit = 100
    messages = read_room_messages(shared_dir, room_id, limit=limit * 2)
    # 过滤 after
    if after:
        filtered = []
        for m in messages:
            ts = m.get("ts", "")
            if ts >= after:
                filtered.append(m)
        messages = filtered
    # 截断到 limit
    if len(messages) > limit:
        messages = messages[-limit:]
    return {
        "room_id": room_id,
        "messages": messages,
        "count": len(messages),
    }


def tool_get_agent_pending(shared_dir, room_id, agent_id):
    """检查指定 Agent 是否有待处理消息。

    MCP tool: agent_bridge.get_agent_pending
    Parameters:
        room_id (required): 房间 ID
        agent_id (required): Agent ID
    """
    if not validate_room_id(room_id):
        raise ValueError("无效的房间 ID: {}".format(room_id))
    if not validate_agent_id(agent_id):
        raise ValueError("无效的 Agent ID: {}".format(agent_id))
    state = read_room_state(shared_dir, room_id)
    current_turn = state.get("current_turn")
    pending = False
    turn_info = None
    if current_turn and current_turn.get("agent_id") == agent_id:
        if current_turn.get("state") in ("waiting_response", "delivering"):
            pending = True
            turn_info = {
                "turn_id": current_turn.get("turn_id", ""),
                "correlation_id": current_turn.get("correlation_id", ""),
                "state": current_turn.get("state", ""),
                "started_at": current_turn.get("started_at", ""),
                "timeout_at": current_turn.get("timeout_at", ""),
                "timeout_seconds": current_turn.get("timeout_seconds", 0),
                "input_message_ids": current_turn.get("input_message_ids", []),
            }
    return {
        "room_id": room_id,
        "agent_id": agent_id,
        "has_pending_turn": pending,
        "current_turn": turn_info,
    }


def tool_reply_turn(shared_dir, room_id, agent_id, message, turn_id="", correlation_id=""):
    """回复当前轮次 — Agent 写入回复消息到房间。

    **关键工具**：内部调用 receive_agent_response()（与 HTTP callback 相同逻辑）。

    MCP tool: agent_bridge.reply_turn
    Parameters:
        room_id (required): 房间 ID
        agent_id (required): Agent ID
        message (required): 回复消息文本
        turn_id (optional): 轮次 ID（用于校验）
        correlation_id (optional): 关联 ID（用于校验）
    """
    # 校验
    if not validate_room_id(room_id):
        raise ValueError("无效的房间 ID: {}".format(room_id))
    if not validate_agent_id(agent_id):
        raise ValueError("无效的 Agent ID: {}".format(agent_id))
    sanitize_message(message)

    _log("reply_turn: room={}, agent={}, turn={}, corr={}, msg_len={}".format(
        room_id, agent_id, turn_id, correlation_id, len(message)))

    # 调用核心运行时 — 与 HTTP callback 完全相同的逻辑
    result = receive_agent_response(
        shared_dir=str(shared_dir),
        room_id=room_id,
        agent_id=agent_id,
        message_text=message,
        turn_id=turn_id,
        correlation_id=correlation_id,
        source="mcp_tool",
        meta={"via": "mcp_server"},
    )

    _log("reply_turn 完成: {}".format(json.dumps(result, ensure_ascii=False)))
    return result


def tool_send_message(shared_dir, room_id, agent_id, message):
    """以 Agent 身份向房间发送一条新消息。

    MCP tool: agent_bridge.send_message
    Parameters:
        room_id (required): 房间 ID
        agent_id (required): Agent ID（发送者）
        message (required): 消息文本
    """
    if not validate_room_id(room_id):
        raise ValueError("无效的房间 ID: {}".format(room_id))
    if not validate_agent_id(agent_id):
        raise ValueError("无效的 Agent ID: {}".format(agent_id))
    sanitize_message(message)

    _log("send_message: room={}, agent={}, msg_len={}".format(
        room_id, agent_id, len(message)))

    # 确保房间目录存在
    rdir = room_dir(shared_dir, room_id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "active.jsonl").touch(exist_ok=True)

    # 写入消息
    msg_record = append_room_message(
        shared_dir=str(shared_dir),
        room_id=room_id,
        from_agent=agent_id,
        text=message,
        kind="agent",
        meta={"source": "mcp_server"},
    )

    _log("send_message 完成: msg_id={}".format(msg_record.get("id", "")))
    return {
        "ok": True,
        "message_id": msg_record.get("id", ""),
        "ts": msg_record.get("ts", ""),
        "room_id": room_id,
        "from": agent_id,
    }


# ── Tool Registry & Dispatch ────────────────────────────

# 工具注册表: tool_name -> (handler, param_spec)
# param_spec 定义每个参数的 required/optional/default
TOOL_REGISTRY = {
    "agent_bridge.list_rooms": {
        "handler": lambda ctx, params: tool_list_rooms(ctx["config"], ctx["shared_dir"]),
        "params": {},
    },
    "agent_bridge.get_current_turn": {
        "handler": lambda ctx, params: tool_get_current_turn(
            ctx["shared_dir"],
            _get_param(params, "room_id", required=True),
        ),
        "params": {
            "room_id": {"required": True, "type": "string"},
        },
    },
    "agent_bridge.read_messages": {
        "handler": lambda ctx, params: tool_read_messages(
            ctx["shared_dir"],
            _get_param(params, "room_id", required=True),
            limit=_get_param(params, "limit", 100),
            after=_get_param(params, "after"),
        ),
        "params": {
            "room_id": {"required": True, "type": "string"},
            "limit": {"required": False, "type": "integer"},
            "after": {"required": False, "type": "string"},
        },
    },
    "agent_bridge.get_agent_pending": {
        "handler": lambda ctx, params: tool_get_agent_pending(
            ctx["shared_dir"],
            _get_param(params, "room_id", required=True),
            _get_param(params, "agent_id", required=True),
        ),
        "params": {
            "room_id": {"required": True, "type": "string"},
            "agent_id": {"required": True, "type": "string"},
        },
    },
    "agent_bridge.reply_turn": {
        "handler": lambda ctx, params: tool_reply_turn(
            ctx["shared_dir"],
            _get_param(params, "room_id", required=True),
            _get_param(params, "agent_id", required=True),
            _get_param(params, "message", required=True),
            turn_id=_get_param(params, "turn_id", ""),
            correlation_id=_get_param(params, "correlation_id", ""),
        ),
        "params": {
            "room_id": {"required": True, "type": "string"},
            "agent_id": {"required": True, "type": "string"},
            "message": {"required": True, "type": "string"},
            "turn_id": {"required": False, "type": "string"},
            "correlation_id": {"required": False, "type": "string"},
        },
    },
    "agent_bridge.send_message": {
        "handler": lambda ctx, params: tool_send_message(
            ctx["shared_dir"],
            _get_param(params, "room_id", required=True),
            _get_param(params, "agent_id", required=True),
            _get_param(params, "message", required=True),
        ),
        "params": {
            "room_id": {"required": True, "type": "string"},
            "agent_id": {"required": True, "type": "string"},
            "message": {"required": True, "type": "string"},
        },
    },
}


def list_tools():
    """返回 MCP tools/list 响应。"""
    tools = []
    for name, spec in TOOL_REGISTRY.items():
        param_spec = spec["params"]
        inputs = []
        required_params = []
        for pname, pinfo in param_spec.items():
            prop = {
                "type": pinfo.get("type", "string"),
                "description": pinfo.get("description", ""),
            }
            param_entry = {
                "name": pname,
                "type": pinfo.get("type", "string"),
                "required": pinfo.get("required", False),
            }
            if pinfo.get("required"):
                required_params.append(pname)
            inputs.append(param_entry)
        tools.append({
            "name": name,
            "description": _tool_description(name),
            "inputSchema": {
                "type": "object",
                "properties": {
                    pname: {"type": pinfo.get("type", "string")}
                    for pname, pinfo in param_spec.items()
                },
                "required": [p for p, pi in param_spec.items() if pi.get("required")],
            },
        })
    return tools


def _tool_description(name):
    """返回工具的简要描述。"""
    descriptions = {
        "agent_bridge.list_rooms": "列出所有配置的房间及其状态",
        "agent_bridge.get_current_turn": "获取指定房间的当前轮次信息（state.json 中的 current_turn）",
        "agent_bridge.read_messages": "读取房间的消息记录（active.jsonl），支持 limit/after 参数",
        "agent_bridge.get_agent_pending": "检查指定 Agent 是否有待处理的轮次",
        "agent_bridge.reply_turn": "回复当前轮次 — Agent 将回复消息写入房间（与 HTTP callback 相同逻辑）",
        "agent_bridge.send_message": "以 Agent 身份向房间发送一条新消息",
    }
    return descriptions.get(name, "")


def dispatch_request(context, method, params, request_id):
    """分发 JSON-RPC 请求到对应的 handler。

    Returns JSON-RPC response dict.
    """
    # 内置方法
    if method == "initialize":
        return _make_response(request_id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "agent-bridge-mcp-server",
                "version": "1.0.0",
            },
            "capabilities": {
                "tools": {},
            },
        })

    if method == "tools/list":
        return _make_response(request_id, {
            "tools": list_tools(),
        })

    if method == "tools/call":
        tool_name = _get_param(params, "name", required=False)
        if not tool_name:
            return _make_error(request_id, ERR_INVALID_PARAMS,
                               "缺少 tool name 参数")
        tool_args = _get_param(params, "arguments", default={})
        if not isinstance(tool_args, dict):
            tool_args = {}
        return _call_tool(context, tool_name, tool_args, request_id)

    if method == "notifications/initialized":
        # 通知不需要响应
        return None

    # 允许直接调用（方便调试）
    if method in TOOL_REGISTRY:
        return _call_tool(context, method, params or {}, request_id)

    return _make_error(request_id, ERR_METHOD_NOT_FOUND,
                       "未知方法: {}".format(method))


def _call_tool(context, tool_name, tool_args, request_id):
    """调用一个 MCP tool 并返回 JSON-RPC 响应。"""
    if tool_name not in TOOL_REGISTRY:
        return _make_error(request_id, ERR_METHOD_NOT_FOUND,
                           "未知工具: {}".format(tool_name),
                           data={"available_tools": list(TOOL_REGISTRY.keys())})

    spec = TOOL_REGISTRY[tool_name]
    handler = spec["handler"]

    try:
        # 校验必需参数
        for pname, pinfo in spec.get("params", {}).items():
            if pinfo.get("required") and pname not in tool_args:
                return _make_error(request_id, ERR_INVALID_PARAMS,
                                   "缺少必需参数: {}".format(pname))
        result = handler(context, tool_args)
        if result is None:
            result = {}
        # MCP tools/call 响应格式: content 数组
        return _make_response(request_id, {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, indent=2),
                }
            ],
        })
    except ValueError as exc:
        _log("参数校验错误: {}".format(exc))
        return _make_error(request_id, ERR_VALIDATION, str(exc))
    except FileNotFoundError as exc:
        _log("文件未找到: {}".format(exc))
        return _make_error(request_id, ERR_ROOM_NOT_FOUND, str(exc))
    except Exception as exc:
        _log("工具执行异常: {}".format(exc))
        import traceback
        traceback.print_exc(file=sys.stderr)
        return _make_error(request_id, ERR_INTERNAL, "内部错误: {}".format(exc))


# ── Main Loop ───────────────────────────────────────────

def run_server(shared_dir, config):
    """主循环：从 stdin 读取 JSON-RPC 请求，向 stdout 写入响应。

    使用行分隔协议，每行一个完整的 JSON 对象。
    """
    context = {
        "shared_dir": shared_dir,
        "config": config,
    }

    _log("MCP Server 启动完成，shared_dir={}".format(shared_dir))
    _log("已加载 {} 个房间, {} 个 Agent".format(
        len(config.get("rooms", {})),
        len(config.get("agents", {})),
    ))
    _log("可用工具: {}".format(", ".join(sorted(TOOL_REGISTRY.keys()))))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        # 解析请求
        request, parse_error = _parse_request(line)
        if parse_error:
            _write_response(parse_error)
            continue

        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id")

        # 分发
        try:
            response = dispatch_request(context, method, params, request_id)
        except Exception as exc:
            _log("分发异常: {}".format(exc))
            import traceback
            traceback.print_exc(file=sys.stderr)
            response = _make_error(request_id, ERR_INTERNAL, "内部错误: {}".format(exc))

        # 通知（无 id）不响应
        if response is None:
            continue

        _write_response(response)


def _write_response(response):
    """将 JSON-RPC 响应写入 stdout。"""
    try:
        payload = json.dumps(response, ensure_ascii=False)
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        _log("stdout 管道断开，退出")
        sys.exit(0)


def _parse_args():
    """解析命令行参数（手动，避免引入 argparse 开销）。"""
    shared_dir = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("--shared-dir", "-s") and i + 1 < len(args):
            shared_dir = args[i + 1]
            i += 2
        elif args[i].startswith("--shared-dir="):
            shared_dir = args[i].split("=", 1)[1]
            i += 1
        elif args[i] in ("--help", "-h"):
            print("用法: python3 core/mcp_server.py [--shared-dir PATH]")
            print("  --shared-dir, -s  共享目录路径 (默认: $AGENT_BRIDGE_SHARED_DIR 或 ~/.agent-bridge)")
            print("  --help, -h         显示帮助")
            sys.exit(0)
        else:
            i += 1
    return shared_dir


def main():
    """入口函数。"""
    cli_shared_dir = _parse_args()
    shared_dir = _resolve_shared_dir(cli_shared_dir)

    _log("共享目录: {}".format(shared_dir))

    # 加载配置
    config = load_bridge_config(shared_dir)
    if config is None:
        _log("错误: 无法加载 bridge.yaml，请确认文件存在于 {}".format(shared_dir))
        # 尝试创建一个最小配置
        config = {
            "shared_dir": str(shared_dir),
            "rooms": {},
            "agents": {},
            "server": {"host": "127.0.0.1", "port": 7899},
        }
        _log("使用空配置运行（无房间）")

    # 确保 shared_dir 一致
    if "shared_dir" not in config:
        config["shared_dir"] = str(shared_dir)

    try:
        run_server(shared_dir, config)
    except KeyboardInterrupt:
        _log("收到中断信号，退出")
        sys.exit(0)
    except Exception as exc:
        _log("服务器异常: {}".format(exc))
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
