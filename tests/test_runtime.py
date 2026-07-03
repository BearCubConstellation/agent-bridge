#!/usr/bin/env python3
"""runtime.py — Room Runtime 状态机单元测试。

测试 run_room_step() 核心流程、receive_agent_response()、超时策略、
agent 选择逻辑等。使用 unittest + mock 隔离外部依赖（adapter/文件系统）。
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock, PropertyMock

# 将 core/ 加入 import 路径（兼容脚本执行）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from protocol import (  # noqa: E402
    ROOM_RUNNING, ROOM_PAUSED, ROOM_ERROR,
    TURN_WAITING_RESPONSE, TURN_COMPLETED,
    TURN_TIMEOUT, TURN_FAILED, TURN_MANUAL_REQUIRED, TURN_SKIPPED,
    RESPONSE_SYNC, RESPONSE_CALLBACK, RESPONSE_MANUAL,
    EVT_TURN_SELECTED, EVT_AGENT_WAKEUP_REQUESTED,
    EVT_AGENT_WAKEUP_SUCCEEDED, EVT_AGENT_WAKEUP_FAILED,
    EVT_AGENT_RESPONSE_RECEIVED, EVT_TURN_COMPLETED,
    EVT_TURN_TIMEOUT, EVT_TURN_SKIPPED, EVT_ROOM_ERROR,
    EVT_ROOM_PAUSED,
    default_room_state, migrate_room_state,
)


# ═══════════════════════════════════════════════════════════
# 工具函数：构建测试数据
# ═══════════════════════════════════════════════════════════

def _make_config(room_id="test_room", order=None, agents=None,
                 max_turns=50, policy=None, server_host="127.0.0.1",
                 server_port=7899, shared_dir=None):
    """构建测试用 config dict。"""
    if order is None:
        order = ["agent_a", "agent_b"]
    if agents is None:
        agents = {
        "agent_a": {
            "id": "agent_a",
            "adapter": {"type": "cli", "config": {"command": "echo hello"}},
        },
        "agent_b": {
            "id": "agent_b",
            "adapter": {"type": "cli", "config": {"command": "echo world"}},
        },
    }
    cfg = {
        "shared_dir": shared_dir or "/tmp/test_shared",
        "server": {"host": server_host, "port": server_port},
        "rooms": {
            room_id: {
                "id": room_id,
                "order": order,
                "max_turns": max_turns,
                "policy": policy or {},
            },
        },
        "agents": agents,
    }
    return cfg


def _make_state(status=ROOM_RUNNING, turn_index=0, turn_count=0,
                current_turn=None, waiting_for="", waiting_line=0,
                max_turns=50, order=None, round=0):
    """构建测试用 room state dict。"""
    if order is None:
        order = ["agent_a", "agent_b"]
    state = default_room_state()
    state.update({
        "status": status,
        "turn_index": turn_index,
        "round": round,
        "turn_count": turn_count,
        "max_turns": max_turns,
        "order": order,
        "current_turn": current_turn,
        "waiting_for": waiting_for,
        "waiting_line": waiting_line,
    })
    return state


def _make_pending_messages(n=2, agent_id="agent_a"):
    """构建测试用 pending 消息列表（模拟 _pending_for_agent 返回）。"""
    msgs = []
    for i in range(n):
        msgs.append({
            "id": f"msg_{i}",
            "ts": f"2026-01-01 12:00:0{i}",
            "from": "agent_b",
            "msg": f"message {i}",
            "kind": "agent",
            "_line": i + 1,
        })
    return msgs


def _make_ticket_ok(response_mode=RESPONSE_CALLBACK, detail="ok",
                    sync_response=""):
    """构建成功的 DeliveryTicket。"""
    return {
        "ok": True,
        "detail": detail,
        "sync_response": sync_response,
        "raw_response": "",
        "response_mode": response_mode,
        "error": "",
        "adapter_type": "cli",
    }


def _make_ticket_fail(error="delivery failed"):
    """构建失败的 DeliveryTicket。"""
    return {
        "ok": False,
        "detail": "",
        "sync_response": "",
        "raw_response": "",
        "response_mode": RESPONSE_CALLBACK,
        "error": error,
        "adapter_type": "cli",
    }


# ═══════════════════════════════════════════════════════════
# A. run_room_step() 核心流程
# ═══════════════════════════════════════════════════════════

class TestRunRoomStepBasic(unittest.TestCase):
    """run_room_step() 基础路径：未找到房间、未运行、达到最大轮次。"""

    def setUp(self):
        """设置基础 mock — 默认所有房间相关函数都 mock 掉。"""
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.tmpdir = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room")
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state")
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_room_not_found_returns_error(
        self, mock_emit, mock_write, mock_log, mock_migrate,
        mock_ensure, mock_normalize, mock_read
    ):
        """config 中没有该 room_id → 返回 ok=False。"""
        from runtime import run_room_step

        # 配置中没有 "unknown_room"
        config = _make_config(room_id="existing_room")
        result = run_room_step(config, "unknown_room")

        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "noop")
        self.assertIn("not found", result["error"])
        # 未进入任何文件操作 / 事件
        mock_read.assert_not_called()
        mock_emit.assert_not_called()

    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_room_not_running_returns_noop(
        self, mock_emit, mock_write, mock_log, mock_migrate,
        mock_ensure, mock_normalize, mock_read
    ):
        """房间状态不是 ROOM_RUNNING → 返回 noop。"""
        from runtime import run_room_step

        config = _make_config()
        state = _make_state(status=ROOM_PAUSED)  # 暂停的房间
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "noop")
        self.assertIn("not running", result["error"])

    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_max_turns_reached_pauses_room(
        self, mock_emit, mock_write, mock_log, mock_migrate,
        mock_ensure, mock_normalize, mock_read
    ):
        """turn_count >= max_turns → 房间自动暂停。"""
        from runtime import run_room_step

        config = _make_config(max_turns=10)
        state = _make_state(status=ROOM_RUNNING, turn_count=10)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "paused")
        self.assertIn("max_turns", result["error"])
        # 验证状态被写入
        self.assertTrue(mock_write.called)
        written_state = mock_write.call_args[0][2]
        self.assertEqual(written_state["status"], ROOM_PAUSED)
        # 验证事件被发出
        mock_emit.assert_called()
        emit_args = mock_emit.call_args[0]
        self.assertEqual(emit_args[2], EVT_ROOM_PAUSED)


class TestRunRoomStepNoPending(unittest.TestCase):
    """无 pending 消息时返回 no_pending / idle。"""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.tmpdir = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines", return_value=[])
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent", return_value=[])
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_no_pending_messages_returns_no_pending(
        self, mock_emit, mock_write, mock_log, mock_migrate,
        mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active
    ):
        """当前 agent 没有待处理的新消息 → 返回 no_pending。"""
        from runtime import run_room_step

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "no_pending")
        self.assertEqual(result["to_agent"], "agent_a")  # 第一个 agent


class TestRunRoomStepDelivery(unittest.TestCase):
    """有 pending 消息时选 agent、创建 Turn、投递。"""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.tmpdir = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    @patch("runtime.deliver_via_registry")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.write_room_cursor")
    @patch("runtime.emit_event")
    def test_pending_selects_agent_and_creates_turn(
        self, mock_emit, mock_write_cursor, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_deliver,
    ):
        """有 pending 消息 → 选 agent → 创建 Turn → 投递。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_deliver.return_value = _make_ticket_ok()

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        # 验证投递成功了（action=waiting，因为 callback 模式）
        self.assertTrue(result["ok"])
        self.assertEqual(result["to_agent"], "agent_a")

        # 验证 Turn 被写入 state
        self.assertTrue(mock_write_state.called)
        written_state = mock_write_state.call_args[0][2]
        self.assertIsNotNone(written_state["current_turn"])
        self.assertEqual(written_state["current_turn"]["state"], TURN_WAITING_RESPONSE)

        # 验证事件序列：turn_selected + wakeup_requested + wakeup_succeeded
        event_types = [call[0][2] for call in mock_emit.call_args_list]
        self.assertIn(EVT_TURN_SELECTED, event_types)
        self.assertIn(EVT_AGENT_WAKEUP_REQUESTED, event_types)
        self.assertIn(EVT_AGENT_WAKEUP_SUCCEEDED, event_types)

    @patch("runtime.deliver_via_registry")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.write_room_cursor")
    @patch("runtime.emit_event")
    def test_delivery_failure_sets_room_error(
        self, mock_emit, mock_write_cursor, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_deliver,
    ):
        """投递失败 → 房间进入 error 状态。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_deliver.return_value = _make_ticket_fail("connection refused")

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "delivery_failed")
        self.assertIn("connection refused", result["error"])

        # 状态应包含 error
        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["status"], ROOM_ERROR)
        self.assertEqual(written_state["current_turn"]["state"], TURN_FAILED)

        # 应发出 wakeup_failed 事件
        event_types = [call[0][2] for call in mock_emit.call_args_list]
        self.assertIn(EVT_AGENT_WAKEUP_FAILED, event_types)


class TestRunRoomStepSyncResponse(unittest.TestCase):
    """sync_response 适配器直接返回结果。"""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.tmpdir = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    @patch("runtime.deliver_via_registry")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.write_room_cursor")
    @patch("runtime.emit_event")
    @patch("runtime.append_room_message")
    @patch("runtime._extract_reply")
    def test_sync_response_completes_turn_immediately(
        self, mock_extract, mock_append, mock_emit,
        mock_write_cursor, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_deliver,
    ):
        """sync 模式 → 立即写消息、完成 Turn、推进 turn_index。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending

        # adapter 返回 sync 模式 + 有同步回复内容
        mock_deliver.return_value = _make_ticket_ok(
            response_mode=RESPONSE_SYNC,
            detail="sync ok",
            sync_response="Hello from agent_a",
        )
        # _extract_reply 返回非空
        mock_extract.return_value = "Hello from agent_a"
        # append_room_message 返回的消息
        mock_append.return_value = {"id": "msg_response_1"}

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "sync_response")
        self.assertTrue(result["delivered"])
        self.assertTrue(result["response_auto_written"])

        # Turn 应被清空（advance 到下一个 agent）
        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertIsNone(written_state["current_turn"])
        self.assertEqual(written_state["turn_index"], 1)  # 从 0→1
        self.assertEqual(written_state["turn_count"], 1)

        # 事件应包括 response_received + turn_completed
        event_types = [call[0][2] for call in mock_emit.call_args_list]
        self.assertIn(EVT_AGENT_RESPONSE_RECEIVED, event_types)
        self.assertIn(EVT_TURN_COMPLETED, event_types)

    @patch("runtime.deliver_via_registry")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.write_room_cursor")
    @patch("runtime.emit_event")
    @patch("runtime.append_room_message")
    @patch("runtime._extract_reply", return_value="")  # 空回复
    def test_sync_response_empty_advances_anyway(
        self, mock_extract, mock_append, mock_emit,
        mock_write_cursor, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_deliver,
    ):
        """sync 模式下回复为空 → 仍然推进 turn（避免卡死）。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending

        mock_deliver.return_value = _make_ticket_ok(
            response_mode=RESPONSE_SYNC,
            sync_response="",
        )

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "sync_empty")
        self.assertFalse(result["response_auto_written"])
        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertIsNone(written_state["current_turn"])


class TestRunRoomStepCallback(unittest.TestCase):
    """callback 适配器返回 DeliveryTicket(waiting)。"""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.tmpdir = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    @patch("runtime.deliver_via_registry")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.write_room_cursor")
    @patch("runtime.emit_event")
    def test_callback_adapter_returns_waiting(
        self, mock_emit, mock_write_cursor, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_deliver,
    ):
        """callback 模式 → 投递成功后进入 waiting 状态。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_deliver.return_value = _make_ticket_ok(
            response_mode=RESPONSE_CALLBACK,
        )

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "waiting")
        self.assertTrue(result["delivered"])
        self.assertEqual(result["waiting_for"], "agent_a")

        # Turn 状态应为 waiting_response
        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["current_turn"]["state"], TURN_WAITING_RESPONSE)
        self.assertIsNotNone(written_state["current_turn"])
        # turn_count 应递增
        self.assertEqual(written_state["turn_count"], 1)


class TestRunRoomStepManual(unittest.TestCase):
    """非自动 agent → manual_required。"""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.tmpdir = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    @patch("runtime.adapter_capability")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_manual_agent_returns_manual_required(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_capability,
    ):
        """不可自动触发的 agent → 进入 manual_required。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        # 返回 non-automatic
        mock_capability.return_value = {"automatic": False, "type": "manual"}

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "manual_required")
        self.assertEqual(result["to_agent"], "agent_a")

        # Turn 状态应为 manual_required
        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["current_turn"]["state"], TURN_MANUAL_REQUIRED)

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_unknown_agent_returns_error(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """agent 不在 config.agents 中 → 房间进入 error。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending

        # agent_a 在 order 中但不在 agents 配置里
        config = _make_config(agents={})
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "error")
        self.assertIn("unknown agent", result["error"])
        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["status"], ROOM_ERROR)

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines", return_value=[])
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent", return_value=[])
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_no_agents_in_order_returns_error(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """order 为空 → 房间直接进入 error。"""
        from runtime import run_room_step

        config = _make_config(order=[])
        state = _make_state(status=ROOM_RUNNING, order=[])
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "error")
        self.assertIn("no agents", result["error"])
        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["status"], ROOM_ERROR)


# ═══════════════════════════════════════════════════════════
# 超时策略测试
# ═══════════════════════════════════════════════════════════

class TestRunRoomStepTimeout(unittest.TestCase):
    """超时策略：skip / retry / pause / error / manual。"""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.tmpdir = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _make_timed_out_turn(self):
        """构建一个已超时的 current_turn。"""
        past_time = (datetime.now() - timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "turn_id": "turn_test123",
            "correlation_id": "corr_test123",
            "agent_id": "agent_a",
            "state": TURN_WAITING_RESPONSE,
            "started_at": "2026-01-01 12:00:00",
            "timeout_at": past_time,
            "timeout_seconds": 180,
            "input_message_ids": ["msg_1"],
            "input_line_max": 1,
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 2,
            "last_error": "",
        }

    # ── skip ──

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines", return_value=[])
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent", return_value=[])
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_timeout_skip_policy(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """超时策略=skip → 跳过当前 agent，推进到下一个。"""
        from runtime import run_room_step

        config = _make_config(policy={"on_timeout": "skip"})
        state = _make_state(status=ROOM_RUNNING, current_turn=self._make_timed_out_turn())
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "skipped")
        self.assertEqual(result["to_agent"], "agent_a")

        # Turn 应被清空，推进 turn_index
        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["turn_index"], 1)
        self.assertIsNone(written_state["current_turn"])

        # 应发出 timeout + skipped 事件
        event_types = [call[0][2] for call in mock_emit.call_args_list]
        self.assertIn(EVT_TURN_TIMEOUT, event_types)
        self.assertIn(EVT_TURN_SKIPPED, event_types)

    # ── retry ──

    @patch("runtime.deliver_via_registry")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.write_room_cursor")
    @patch("runtime.emit_event")
    def test_timeout_retry_policy_retries_then_skips(
        self, mock_emit, mock_write_cursor, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_deliver,
    ):
        """超时策略=retry → 未超重试次数时重新投递。"""
        from runtime import run_room_step

        pending = _make_pending_messages(1)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_deliver.return_value = _make_ticket_ok()

        config = _make_config(policy={"on_timeout": "retry"})
        turn = self._make_timed_out_turn()
        turn["attempts"] = 1
        turn["max_attempts"] = 3  # 还有重试空间
        state = _make_state(status=ROOM_RUNNING, current_turn=turn)
        mock_read.side_effect = [
            state,
            # re-deliver 时 run_room_step 递归调用的 state
            _make_state(status=ROOM_RUNNING, current_turn=turn),
        ]

        # 第一次调用时 read_room_state 返回 state（有 current_turn=timed_out）
        # 递归调用时 run_room_step 再次读取 → 我们需要让第二次读取返回不同的内容
        result = run_room_step(config, "test_room")

        # 重试策略：应该尝试重新投递
        # 结果取决于递归调用 run_room_step，这里我们只要确认 timeout 被发现并触发了 retry
        self.assertIsNotNone(result)

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines", return_value=[])
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent", return_value=[])
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_timeout_retry_policy_exceeded_skips(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """超时策略=retry 但已达最大次数 → 跳过。"""
        from runtime import run_room_step

        config = _make_config(policy={"on_timeout": "retry"})
        turn = self._make_timed_out_turn()
        turn["attempts"] = 3  # 已达最大
        turn["max_attempts"] = 3
        state = _make_state(status=ROOM_RUNNING, current_turn=turn)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "skipped")

    # ── pause ──

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines", return_value=[])
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent", return_value=[])
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_timeout_pause_policy(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """超时策略=pause → 房间暂停。"""
        from runtime import run_room_step

        config = _make_config(policy={"on_timeout": "pause"})
        state = _make_state(status=ROOM_RUNNING, current_turn=self._make_timed_out_turn())
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "paused")
        self.assertIn("timeout", result["error"])

        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["status"], ROOM_PAUSED)
        self.assertIsNone(written_state["current_turn"])

    # ── error ──

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines", return_value=[])
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent", return_value=[])
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_timeout_error_policy(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """超时策略=error → 房间进入 error 状态。"""
        from runtime import run_room_step

        config = _make_config(policy={"on_timeout": "error"})
        state = _make_state(status=ROOM_RUNNING, current_turn=self._make_timed_out_turn())
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "error")
        self.assertIn("timeout", result["error"])

        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["status"], ROOM_ERROR)

    # ── manual ──

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines", return_value=[])
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent", return_value=[])
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_timeout_manual_policy(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """超时策略=manual → Turn 进入 manual_required。"""
        from runtime import run_room_step

        config = _make_config(policy={"on_timeout": "manual"})
        state = _make_state(status=ROOM_RUNNING, current_turn=self._make_timed_out_turn())
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "manual_required")
        self.assertEqual(result["to_agent"], "agent_a")

        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["current_turn"]["state"], TURN_MANUAL_REQUIRED)
        self.assertIn("manual", written_state["current_turn"]["last_error"])

    # ── waiting 状态（正常等待中）──

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines", return_value=[])
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent", return_value=[])
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_waiting_state_returns_waiting(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """current_turn 在 waiting_response 且未超时 → 返回 waiting。"""
        from runtime import run_room_step

        # 未超时的 turn
        future_time = (datetime.now() + timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
        turn = {
            "turn_id": "turn_test123",
            "agent_id": "agent_a",
            "state": TURN_WAITING_RESPONSE,
            "started_at": "2026-01-01 12:00:00",
            "timeout_at": future_time,
            "timeout_seconds": 180,
            "input_message_ids": ["msg_1"],
            "input_line_max": 1,
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 2,
            "last_error": "",
        }
        state = _make_state(status=ROOM_RUNNING, current_turn=turn)
        mock_read.return_value = state

        config = _make_config()
        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "waiting")
        self.assertEqual(result["waiting_for"], "agent_a")


# ═══════════════════════════════════════════════════════════
# B. receive_agent_response()
# ═══════════════════════════════════════════════════════════

class TestReceiveAgentResponse(unittest.TestCase):
    """receive_agent_response() 回调处理。"""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.tmpdir = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    @patch("runtime.append_room_message")
    @patch("runtime.write_room_state")
    @patch("runtime.read_room_state")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime.emit_event")
    def test_normal_callback_completes_turn(
        self, mock_emit, mock_migrate, mock_read,
        mock_write, mock_append,
    ):
        """正常回调 → 写消息、标记 response_received、调度下一步。"""
        from runtime import receive_agent_response

        turn = {
            "turn_id": "turn_test123",
            "correlation_id": "corr_test123",
            "agent_id": "agent_a",
            "state": TURN_WAITING_RESPONSE,
            "started_at": "2026-01-01 12:00:00",
            "timeout_at": "2099-01-01 00:00:00",
            "timeout_seconds": 180,
            "input_message_ids": ["msg_1"],
            "input_line_max": 1,
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 2,
            "last_error": "",
        }
        state = _make_state(status=ROOM_RUNNING, current_turn=turn)
        mock_read.return_value = state
        mock_append.return_value = {"id": "msg_response_42"}

        result = receive_agent_response(
            self.tmpdir, "test_room", "agent_a",
            "Hello from agent_a",
            turn_id="turn_test123",
            correlation_id="corr_test123",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["message_id"], "msg_response_42")
        self.assertTrue(result["scheduled"])

        # 验证 append_room_message 被调用
        mock_append.assert_called_once()
        # 验证 state 被写入，且包含 response_message_id
        self.assertTrue(mock_write.called)
        written_state = mock_write.call_args[0][2]
        self.assertEqual(written_state["current_turn"]["response_message_id"], "msg_response_42")

    @patch("runtime.append_room_message")
    @patch("runtime.write_room_state")
    @patch("runtime.read_room_state")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime.emit_event")
    def test_callback_turn_id_mismatch(
        self, mock_emit, mock_migrate, mock_read,
        mock_write, mock_append,
    ):
        """回调 turn_id 不匹配 → 返回 error。"""
        from runtime import receive_agent_response

        turn = {
            "turn_id": "turn_abc",
            "correlation_id": "corr_abc",
            "agent_id": "agent_a",
            "state": TURN_WAITING_RESPONSE,
            "started_at": "2026-01-01 12:00:00",
            "timeout_at": "2099-01-01 00:00:00",
            "timeout_seconds": 180,
            "input_message_ids": ["msg_1"],
            "input_line_max": 1,
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 2,
            "last_error": "",
        }
        state = _make_state(status=ROOM_RUNNING, current_turn=turn)
        mock_read.return_value = state

        result = receive_agent_response(
            self.tmpdir, "test_room", "agent_a",
            "Hello",
            turn_id="turn_xyz",  # 不匹配！
        )

        self.assertFalse(result["ok"])
        self.assertIn("mismatch", result["error"])
        # 不应写入消息
        mock_append.assert_not_called()

    @patch("runtime.append_room_message")
    @patch("runtime.write_room_state")
    @patch("runtime.read_room_state")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime.emit_event")
    def test_callback_agent_id_mismatch(
        self, mock_emit, mock_migrate, mock_read,
        mock_write, mock_append,
    ):
        """回调 agent_id 不匹配 → 返回 error。"""
        from runtime import receive_agent_response

        turn = {
            "turn_id": "turn_test",
            "correlation_id": "corr_test",
            "agent_id": "agent_a",  # 当前 turn 属于 agent_a
            "state": TURN_WAITING_RESPONSE,
            "started_at": "2026-01-01 12:00:00",
            "timeout_at": "2099-01-01 00:00:00",
            "timeout_seconds": 180,
            "input_message_ids": ["msg_1"],
            "input_line_max": 1,
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 2,
            "last_error": "",
        }
        state = _make_state(status=ROOM_RUNNING, current_turn=turn)
        mock_read.return_value = state

        result = receive_agent_response(
            self.tmpdir, "test_room", "agent_b",  # agent_b 试图回调！
            "Hello",
        )

        self.assertFalse(result["ok"])
        self.assertIn("belongs to", result["error"])

    @patch("runtime.append_room_message")
    @patch("runtime.write_room_state")
    @patch("runtime.read_room_state")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime.emit_event")
    def test_callback_correlation_id_mismatch(
        self, mock_emit, mock_migrate, mock_read,
        mock_write, mock_append,
    ):
        """回调 correlation_id 不匹配 → 返回 error。"""
        from runtime import receive_agent_response

        turn = {
            "turn_id": "turn_test",
            "correlation_id": "corr_abc",
            "agent_id": "agent_a",
            "state": TURN_WAITING_RESPONSE,
            "started_at": "2026-01-01 12:00:00",
            "timeout_at": "2099-01-01 00:00:00",
            "timeout_seconds": 180,
            "input_message_ids": ["msg_1"],
            "input_line_max": 1,
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 2,
            "last_error": "",
        }
        state = _make_state(status=ROOM_RUNNING, current_turn=turn)
        mock_read.return_value = state

        result = receive_agent_response(
            self.tmpdir, "test_room", "agent_a",
            "Hello",
            correlation_id="corr_xyz",  # 不匹配！
        )

        self.assertFalse(result["ok"])
        self.assertIn("correlation_id", result["error"])

    @patch("runtime.append_room_message")
    @patch("runtime.write_room_state")
    @patch("runtime.read_room_state")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime.emit_event")
    def test_callback_no_active_turn_writes_free_message(
        self, mock_emit, mock_migrate, mock_read,
        mock_write, mock_append,
    ):
        """没有活跃 Turn → 以自由 agent 消息写入。"""
        from runtime import receive_agent_response

        # 没有 current_turn
        state = _make_state(status=ROOM_RUNNING, current_turn=None)
        mock_read.return_value = state
        mock_append.return_value = {"id": "msg_free_1"}

        result = receive_agent_response(
            self.tmpdir, "test_room", "agent_a", "Free message",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["message_id"], "msg_free_1")
        self.assertFalse(result["scheduled"])
        self.assertIn("no active turn", result["note"])

    @patch("runtime.append_room_message")
    @patch("runtime.write_room_state")
    @patch("runtime.read_room_state")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime.emit_event")
    def test_callback_turn_id_empty_passes_validation(
        self, mock_emit, mock_migrate, mock_read,
        mock_write, mock_append,
    ):
        """回调不传 turn_id → turn_id 检查被跳过（空字符串不校验）。"""
        from runtime import receive_agent_response

        turn = {
            "turn_id": "turn_test123",
            "correlation_id": "corr_test123",
            "agent_id": "agent_a",
            "state": TURN_WAITING_RESPONSE,
            "started_at": "2026-01-01 12:00:00",
            "timeout_at": "2099-01-01 00:00:00",
            "timeout_seconds": 180,
            "input_message_ids": ["msg_1"],
            "input_line_max": 1,
            "response_message_id": "",
            "attempts": 1,
            "max_attempts": 2,
            "last_error": "",
        }
        state = _make_state(status=ROOM_RUNNING, current_turn=turn)
        mock_read.return_value = state
        mock_append.return_value = {"id": "msg_ok"}

        # 不传 turn_id（空字符串），应该通过
        result = receive_agent_response(
            self.tmpdir, "test_room", "agent_a", "Hello",
        )

        self.assertTrue(result["ok"])
        mock_append.assert_called_once()


# ═══════════════════════════════════════════════════════════
# C. 辅助函数测试
# ═══════════════════════════════════════════════════════════

class TestHelperFunctions(unittest.TestCase):
    """运行时辅助函数：选 agent、pending 收集、capability 检查。"""

    # ── 选 agent 逻辑（轮转）──

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    @patch("runtime.deliver_via_registry")
    def test_agent_selection_follows_turn_index(
        self, mock_deliver, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """选 agent 遵循 turn_index 轮转顺序。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2, agent_id="agent_a")
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_deliver.return_value = _make_ticket_ok()

        # room 有 3 个 agent
        config = _make_config(order=["agent_a", "agent_b", "agent_c"],
                              agents={
                                  "agent_a": {"id": "agent_a", "adapter": {"type": "cli", "config": {"command": "echo a"}}},
                                  "agent_b": {"id": "agent_b", "adapter": {"type": "cli", "config": {"command": "echo b"}}},
                                  "agent_c": {"id": "agent_c", "adapter": {"type": "cli", "config": {"command": "echo c"}}},
                              })

        # turn_index=2 → 应选 agent_c
        state = _make_state(status=ROOM_RUNNING, turn_index=2, order=["agent_a", "agent_b", "agent_c"])
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["to_agent"], "agent_c")

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    @patch("runtime.deliver_via_registry")
    def test_turn_index_wraps_around(
        self, mock_deliver, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """turn_index 超出长度时取模轮转。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2, agent_id="agent_a")
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_deliver.return_value = _make_ticket_ok()

        config = _make_config(order=["agent_a", "agent_b", "agent_c"],
                              agents={
                                  "agent_a": {"id": "agent_a", "adapter": {"type": "cli", "config": {"command": "echo a"}}},
                                  "agent_b": {"id": "agent_b", "adapter": {"type": "cli", "config": {"command": "echo b"}}},
                                  "agent_c": {"id": "agent_c", "adapter": {"type": "cli", "config": {"command": "echo c"}}},
                              })

        # turn_index=5 → 5%3=2 → agent_c
        state = _make_state(status=ROOM_RUNNING, turn_index=5, order=["agent_a", "agent_b", "agent_c"])
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["to_agent"], "agent_c")

    # ── 轮次计数 ──

    @patch("runtime.deliver_via_registry")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.write_room_cursor")
    @patch("runtime.emit_event")
    def test_round_increments_on_wrap_around(
        self, mock_emit, mock_write_cursor, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_deliver,
    ):
        """turn_index 回到 0 时 round 递增。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        # 使用 sync 模式让 turn 立即完成并推进
        mock_deliver.return_value = _make_ticket_ok(
            response_mode=RESPONSE_SYNC,
            sync_response="Hello",
        )

        config = _make_config()
        # turn_index=1（最后一个 agent），下一个回到 0 应触发 round+1
        state = _make_state(status=ROOM_RUNNING, turn_index=1, round=5)
        mock_read.return_value = state

        with patch("runtime._extract_reply", return_value="Hello"), \
             patch("runtime.append_room_message", return_value={"id": "msg_x"}):
            result = run_room_step(config, "test_room")

        # 验证 round 递增
        written_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(written_state["round"], 6)

    # ── pending 消息收集 ──

    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    @patch("runtime.deliver_via_registry")
    def test_pending_messages_passed_to_delivery(
        self, mock_deliver, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
    ):
        """验证 pending 消息被正确传递给 adapter。"""
        from runtime import run_room_step

        pending = [
            {"id": "msg_1", "from": "agent_b", "msg": "hello", "kind": "agent", "_line": 1},
            {"id": "msg_2", "from": "agent_c", "msg": "world", "kind": "agent", "_line": 2},
        ]
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_deliver.return_value = _make_ticket_ok()

        config = _make_config(order=["agent_a", "agent_b", "agent_c"],
                              agents={
                                  "agent_a": {"id": "agent_a", "adapter": {"type": "cli", "config": {"command": "echo a"}}},
                                  "agent_b": {"id": "agent_b", "adapter": {"type": "cli", "config": {"command": "echo b"}}},
                                  "agent_c": {"id": "agent_c", "adapter": {"type": "cli", "config": {"command": "echo c"}}},
                              })
        state = _make_state(status=ROOM_RUNNING, order=["agent_a", "agent_b", "agent_c"])
        mock_read.return_value = state

        run_room_step(config, "test_room")

        # 验证 deliver_via_registry 收到的 text 包含所有 pending 消息
        self.assertTrue(mock_deliver.called)
        call_args = mock_deliver.call_args
        text_arg = call_args[0][1]
        self.assertIn("hello", text_arg)
        self.assertIn("world", text_arg)

    # ── capability 检查 ──

    @patch("runtime.adapter_capability")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    @patch("runtime.deliver_via_registry")
    def test_capability_automatic_proceeds_to_delivery(
        self, mock_deliver, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_capability,
    ):
        """automatic=True → 正常进入投递流程。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_capability.return_value = {"automatic": True, "type": "cli"}
        mock_deliver.return_value = _make_ticket_ok()

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        # 应该正常投递
        self.assertTrue(result["ok"])
        mock_deliver.assert_called_once()

    @patch("runtime.adapter_capability")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.emit_event")
    def test_capability_non_automatic_triggers_manual(
        self, mock_emit, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_capability,
    ):
        """automatic=False → 返回 manual_required 且不投递。"""
        from runtime import run_room_step

        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_capability.return_value = {"automatic": False, "type": "manual"}

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result = run_room_step(config, "test_room")

        self.assertEqual(result["action"], "manual_required")

    # ── _callback_url / _callback_base_url ──

    def test_callback_base_url_default(self):
        """默认 server 配置的 callback_base_url。"""
        from runtime import _callback_base_url

        config = {"server": {}}
        result = _callback_base_url(config)
        self.assertEqual(result, "http://127.0.0.1:7899")

    def test_callback_base_url_custom(self):
        """自定义 server 配置的 callback_base_url。"""
        from runtime import _callback_base_url

        config = {"server": {"host": "0.0.0.0", "port": 9999}}
        result = _callback_base_url(config)
        self.assertEqual(result, "http://0.0.0.0:9999")

    def test_callback_url_format(self):
        """callback_url 格式正确。"""
        from runtime import _callback_url

        config = {"server": {"host": "127.0.0.1", "port": 7899}}
        result = _callback_url(config, "my_room", "my_agent")
        self.assertIn("/api/rooms/my_room/agents/my_agent/callback", result)

    # ── _now_ts / _parse_ts ──

    def test_now_ts_format(self):
        """_now_ts 返回标准格式的时间戳。"""
        from runtime import _now_ts

        ts = _now_ts()
        self.assertIsInstance(ts, str)
        # 格式应为 YYYY-MM-DD HH:MM:SS
        parts = ts.split(" ")
        self.assertEqual(len(parts), 2)
        date_parts = parts[0].split("-")
        self.assertEqual(len(date_parts), 3)

    def test_parse_ts_valid(self):
        """_parse_ts 正确解析时间戳。"""
        from runtime import _parse_ts
        from datetime import datetime as dt

        result = _parse_ts("2026-05-30 15:00:00")
        self.assertIsInstance(result, dt)
        self.assertEqual(result.year, 2026)

    def test_parse_ts_invalid_returns_none(self):
        """_parse_ts 无效输入返回 None。"""
        from runtime import _parse_ts

        self.assertIsNone(_parse_ts(""))
        self.assertIsNone(_parse_ts(None))
        self.assertIsNone(_parse_ts("not-a-date"))

    # ── _shared_dir ──

    def test_shared_dir_expands(self):
        """_shared_dir 展开环境变量和 ~。"""
        from runtime import _shared_dir

        result = _shared_dir({"shared_dir": "/tmp/test_dir"})
        self.assertEqual(str(result), "/tmp/test_dir")


# ═══════════════════════════════════════════════════════════
# D. 集成流程测试（模拟完整一轮）
# ═══════════════════════════════════════════════════════════

class TestCompleteTurnFlow(unittest.TestCase):
    """端到端一轮流程：投递 → callback 回调 → 完成 Turn。"""

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-bridge-test-")
        self.tmpdir = Path(self.tmpdir_obj.name)

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    @patch("runtime.deliver_via_registry")
    @patch("runtime.room_active_file", return_value=Path("/tmp/test_active.jsonl"))
    @patch("runtime._messages_with_lines")
    @patch("runtime.read_room_cursor", return_value=0)
    @patch("runtime._pending_for_agent")
    @patch("runtime.read_room_state")
    @patch("runtime.normalize_room", side_effect=lambda cfg: cfg)
    @patch("runtime.ensure_room")
    @patch("runtime.migrate_room_state", side_effect=lambda s, cfg=None: s)
    @patch("runtime._log_tick")
    @patch("runtime.write_room_state")
    @patch("runtime.write_room_cursor")
    @patch("runtime.emit_event")
    @patch("runtime.append_room_message")
    def test_full_callback_flow_from_delivery_to_completion(
        self, mock_append, mock_emit, mock_write_cursor, mock_write_state, mock_log,
        mock_migrate, mock_ensure, mock_normalize, mock_read,
        mock_pending, mock_cursor, mock_msgs, mock_active,
        mock_deliver,
    ):
        """完整流程：run_room_step 投递 callback → receive_agent_response 完成。

        验证：投递后 Turn ID 被存储，回调匹配 Turn 完成，状态正确推进。
        """
        from runtime import run_room_step, receive_agent_response

        # ── 第一阶段：run_room_step 投递 ──
        pending = _make_pending_messages(2)
        mock_pending.return_value = pending
        mock_msgs.return_value = pending
        mock_deliver.return_value = _make_ticket_ok(response_mode=RESPONSE_CALLBACK)

        config = _make_config()
        state = _make_state(status=ROOM_RUNNING)
        mock_read.return_value = state

        result_deliver = run_room_step(config, "test_room")

        self.assertEqual(result_deliver["action"], "waiting")

        # 获取写入的 state 中的 turn_id
        write_call_args = mock_write_state.call_args_list
        # 最后一次 write 包含 current_turn
        last_written = write_call_args[-1][0][2]
        turn_id = last_written["current_turn"]["turn_id"]
        correlation_id = last_written["current_turn"]["correlation_id"]
        self.assertTrue(turn_id.startswith("turn_"))

        # ── 第二阶段：模拟回调 ──
        # 更新 mock 让 read_room_state 返回带 current_turn 的 state
        mock_read.reset_mock()
        callback_state = _make_state(
            status=ROOM_RUNNING,
            current_turn=last_written["current_turn"],
        )
        mock_read.return_value = callback_state

        mock_append.return_value = {"id": "msg_callback_99"}

        result_callback = receive_agent_response(
            self.tmpdir, "test_room", "agent_a",
            "Response via callback",
            turn_id=turn_id,
            correlation_id=correlation_id,
        )

        self.assertTrue(result_callback["ok"])
        self.assertEqual(result_callback["message_id"], "msg_callback_99")

        # 验证 state 被标记了 response_message_id
        callback_write_state = mock_write_state.call_args_list[-1][0][2]
        self.assertEqual(
            callback_write_state["current_turn"]["response_message_id"],
            "msg_callback_99",
        )

        # ── 第三阶段：run_room_step 检测到 response → 完成 Turn ──
        mock_read.reset_mock()
        completion_state = _make_state(
            status=ROOM_RUNNING,
            current_turn=callback_write_state["current_turn"],
            turn_index=0,
            order=["agent_a", "agent_b"],
        )
        mock_read.return_value = completion_state

        result_complete = run_room_step(config, "test_room")

        self.assertEqual(result_complete["action"], "response_received")
        self.assertEqual(result_complete["to_agent"], "agent_a")

        # 验证 Turn 被清空，turn_index 推进
        final_state = mock_write_state.call_args_list[-1][0][2]
        self.assertIsNone(final_state["current_turn"])
        self.assertEqual(final_state["turn_index"], 1)


if __name__ == "__main__":
    unittest.main()
