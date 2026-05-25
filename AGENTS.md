# Repository Guidelines

## Project Structure & Module Organization

Agent Bridge 是一个小型 Python 项目，用于基于文件的异步 Agent 消息通信。核心代码位于 `core/`：`send.py` 负责追加 JSONL 消息，`poll.py` 负责轮询与唤醒，`lock.py` 提供文件锁。CLI 入口在 `cli/bridge`。本地 Web UI 位于 `ui/`，其中 `server.py` 提供 API 路由，`index.html` 是前端页面。适配器模板放在 `adapters/`，协议文档放在 `protocol/SPEC.md`，安装脚本在 `setup/` 及根目录 `install.sh`、`install.ps1`。测试统一放在 `tests/`，文件命名为 `test_*.py`。

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt`：安装文档化 YAML 配置所需依赖。
- `python -m unittest discover -s tests`：运行完整测试套件。
- `python cli/bridge start`：启动 UI 服务并打开 `http://127.0.0.1:7899`。
- `python cli/bridge start --no-open`：启动 UI 服务但不打开浏览器。
- `python ui/server.py --port 7899 --no-poll`：仅运行开发用 UI/API 服务。
- `python core/send.py --agent alice "hello"`：以 `alice` 身份写入一条消息。

## Coding Style & Naming Conventions

代码保持兼容 Python 3.8+，使用 4 空格缩进。函数、变量和模块名使用 `snake_case`，测试类使用 `PascalCase`。模块应保持脚本友好，提供明确的 `main()` 入口和 `if __name__ == "__main__"` 保护。文件系统操作优先使用 `pathlib.Path`。共享消息记录保持 `{"ts": "...", "from": "...", "msg": "..."}` JSONL 结构。写入共享文件前，沿用现有 Agent ID 校验规则：只允许字母、数字、连字符和下划线。

## Testing Guidelines

测试框架为 `unittest`。新增测试应放入 `tests/`，命名为 `test_<feature>.py`。文件系统相关测试必须使用临时目录；除非测试目标是路径展开逻辑，否则不要写入真实用户配置。针对文件锁、归档轮转、配置解析、HTTP 端点、消息校验和安全边界添加聚焦回归测试。

## Commit & Pull Request Guidelines

提交信息保持简短、清晰，准确描述变更核心。近期历史包含 `fix: ...`、`feat: ...` 以及简短中文描述；优先使用简洁 Conventional Commit 风格，例如 `fix: 修复轮询游标更新`。PR 应包含变更摘要、测试结果、相关 issue 链接；涉及 UI 的变更需附截图或说明。影响安装器、启动流程或配置迁移时，必须在 PR 中明确说明。

## Security & Configuration Tips

不要提交生成的 `bridge.yaml`、令牌、日志或共享消息数据。将 webhook URL、bearer token 路径和 `~/.agent-bridge` 内容视为用户本地配置。修改 HTTP 处理逻辑时，保留默认 localhost 行为和路径穿越检查。
