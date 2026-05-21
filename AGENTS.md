# 仓库协作指南

## 项目概览

Agent Bridge 是一个小型 Python 项目，用于基于文件的异步 Agent 消息通信。运行时代码以 Python 为主，文档化的 YAML 配置依赖 `PyYAML`。

## 目录结构

- `core/`：核心运行逻辑。
- `core/send.py`：追加 JSONL 消息。
- `core/poll.py`：检查新消息并唤醒 Agent。
- `core/lock.py`：提供文件锁。
- `cli/bridge`：可执行 CLI 入口。
- `ui/`：本地 Web UI。
- `ui/server.py`：提供 API 路由。
- `ui/index.html`：前端页面。
- `adapters/`：适配器模板。
- `protocol/SPEC.md`：协议说明。
- `setup/`：安装与初始化脚本。
- `docs/`：面向用户的文档。
- `tests/`：单元测试，文件命名使用 `test_*.py`。

## 常用命令

- `python cli/bridge start`：启动 UI 服务并打开 `http://127.0.0.1:7899`。
- `python cli/bridge start --no-open`：启动 UI 服务但不打开浏览器。
- `python ui/server.py --port 7899 --no-poll`：仅运行开发用 UI/API 服务。
- `python core/send.py --agent alice "hello"`：以 `alice` 身份追加一条 JSONL 消息。
- `python -m pip install -r requirements.txt`：安装文档化 YAML 配置所需依赖。
- `python -m unittest discover -s tests`：运行完整测试套件。

## 代码风格

- 使用兼容 Python 3.8+ 的代码。
- 使用 4 空格缩进。
- 函数和变量使用 `snake_case`。
- 测试类使用 `PascalCase`。
- 模块应保持脚本友好，提供明确的 `main()` 入口和 `if __name__ == "__main__"` 保护。
- 写入共享文件前，沿用现有规则校验 Agent ID：只允许字母、数字、连字符和下划线。
- 文件系统操作优先使用 `pathlib.Path`。
- JSONL 记录保持 `{"ts": "...", "from": "...", "msg": "..."}` 结构。

## 测试规范

- 测试使用 `unittest`。
- 测试文件放在 `tests/` 目录下，并命名为 `test_<feature>.py`。
- 文件系统相关测试应使用临时目录。
- 除非测试目标就是路径展开逻辑，否则不要写入真实用户配置。
- 针对文件锁、归档轮转、配置解析、HTTP 端点和消息校验添加聚焦的回归测试。

## 提交与 PR

- 提交信息保持简短、清晰，并准确描述变更核心。
- 近期提交采用简洁的 Conventional Commit 风格，常见格式包括 `fix(install): ...`、`refactor(install): ...` 和 `docs: ...`。
- 保持提交小而聚焦。
- PR 应包含简短摘要、测试结果、相关 issue 链接，以及 UI 变更的截图或说明。
- 如果变更影响安装器、启动流程或配置迁移，需要在 PR 中明确说明。

## 安全与配置

- 不要提交生成的 `bridge.yaml`、令牌、日志或共享消息数据。
- 将 webhook URL、bearer token 路径和 `~/.agent-bridge` 内容视为用户本地配置。
- 修改 HTTP 处理逻辑时，保留面向 localhost 的默认行为和路径穿越检查。
