#!/usr/bin/env python3
"""MCPManager — 管理 stdio MCP server 子进程。

bridge start 时自动拉起 core/mcp_server.py 作为子进程，
让原生 MCP client（Claude Desktop / Cursor / Cherry Studio 等）
能通过 stdio JSON-RPC 接入 Agent Bridge。

注意：
- 子进程的 stdin/stdout 由 MCP client 直接接管（本 manager 只负责生命周期）
- 子进程的 stderr 写入日志文件供排查
- HTTP MCP 端点（/api/mcp）独立于此，不依赖子进程存活
"""
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


class MCPManager:
    """stdio MCP server 子进程的生命周期管理器。"""

    def __init__(self, shared_dir, src_dir=None, log_dir=None):
        """
        Parameters
        ----------
        shared_dir : str | Path
            Agent Bridge 共享目录（~/.agent-bridge）
        src_dir : str | Path | None
            core/ 目录的绝对路径（mcp_server.py 所在位置）。
            默认推断为 ../core（相对于本文件）
        log_dir : str | Path | None
            日志目录，默认 shared_dir
        """
        self.shared_dir = str(shared_dir)
        self.src_dir = str(src_dir or Path(__file__).resolve().parent.parent / "core")
        self.log_dir = str(log_dir or shared_dir)
        self.mcp_script = str(Path(self.src_dir) / "mcp_server.py")
        self._proc = None
        self._lock = threading.Lock()
        self._stderr_fh = None

    def start(self):
        """启动 stdio MCP server 子进程。

        子进程通过 stdin/stdout 与 MCP client（而非本 manager）通信，
        所以这里用 PIPE 而非 DEVNULL——但 manager 本身不读写这些管道。
        """
        with self._lock:
            if self._proc and self._proc.poll() is None:
                logging.info("MCP server 已在运行 (pid=%s)", self._proc.pid)
                return True

            if not Path(self.mcp_script).exists():
                logging.warning("MCP server 脚本不存在: %s", self.mcp_script)
                return False

            # stderr 写入日志文件
            log_path = Path(self.log_dir) / "mcp_server.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._stderr_fh = open(log_path, "ab", buffering=0)
            except Exception as e:
                logging.warning("无法打开 MCP 日志文件 %s: %s", log_path, e)
                self._stderr_fh = None

            env = dict(os.environ)
            env["AGENT_BRIDGE_SHARED_DIR"] = self.shared_dir
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"

            try:
                self._proc = subprocess.Popen(
                    [sys.executable, self.mcp_script, "--shared-dir", self.shared_dir],
                    stdin=subprocess.PIPE,   # 由 MCP client 通过本进程转发；本 manager 不直接写
                    stdout=subprocess.PIPE,  # 同上，本 manager 不直接读
                    stderr=self._stderr_fh or subprocess.DEVNULL,
                    env=env,
                    # Windows 不支持 process group，用 CREATE_NEW_PROCESS_GROUP
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                )
                logging.info("MCP server 已启动 (pid=%s), shared_dir=%s", self._proc.pid, self.shared_dir)
                print(f"[mcp] stdio MCP server started (pid={self._proc.pid})")
                return True
            except Exception as e:
                logging.exception("MCP server 启动失败: %s", e)
                print(f"[mcp] 启动失败: {e}")
                return False

    def stop(self, timeout=5):
        """优雅停止子进程（SIGTERM → wait → SIGKILL）。"""
        with self._lock:
            if not self._proc:
                return
            if self._proc.poll() is not None:
                self._proc = None
                self._close_stderr()
                return

            pid = self._proc.pid
            logging.info("停止 MCP server (pid=%s)", pid)
            try:
                # Windows: SIGTERM 不存在，用 CTRL_BREAK_EVENT
                if sys.platform == "win32":
                    self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    self._proc.terminate()
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logging.warning("MCP server 未在 %ss 内退出，强制终止", timeout)
                self._proc.kill()
                try:
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
            except Exception as e:
                logging.warning("停止 MCP server 异常: %s", e)
                try:
                    self._proc.kill()
                except Exception:
                    pass
            finally:
                self._proc = None
                self._close_stderr()

    def _close_stderr(self):
        if self._stderr_fh:
            try:
                self._stderr_fh.close()
            except Exception:
                pass
            self._stderr_fh = None

    def is_alive(self):
        """检查子进程是否存活。"""
        return bool(self._proc and self._proc.poll() is None)

    def pid(self):
        return self._proc.pid if self._proc and self._proc.poll() is None else None
