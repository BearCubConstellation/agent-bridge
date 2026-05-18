# Repository Guidelines

## Project Structure & Module Organization

Agent Bridge is a small Python project for file-based asynchronous agent messaging. Core runtime logic lives in `core/`: `send.py` appends messages, `poll.py` checks for new messages and wakes agents, and `lock.py` provides file locking. The executable CLI is `cli/bridge`. The local web UI is in `ui/`, with `server.py` serving API routes and `index.html` containing the frontend. Adapter templates are in `adapters/`, protocol details are in `protocol/SPEC.md`, setup scripts are in `setup/`, and user-facing docs are in `docs/`. Unit tests live in `tests/` and use `test_*.py` naming.

## Build, Test, and Development Commands

- `python cli/bridge setup`: run the interactive local configuration flow.
- `python cli/bridge start --open`: start the UI server and open `http://127.0.0.1:7899`.
- `python ui/server.py --port 7899 --no-poll`: run only the development UI/API server.
- `python core/send.py --agent alice "hello"`: append one JSONL message as `alice`.
- `python -m pip install -r requirements.txt`: install YAML support used by documented configs.
- `python -m unittest discover -s tests`: run the full test suite.

Runtime code is Python-first and depends on `PyYAML` for documented YAML configuration.

## Coding Style & Naming Conventions

Use Python 3.8+ compatible code, 4-space indentation, `snake_case` for functions and variables, and `PascalCase` for test classes. Keep modules script-friendly with explicit `main()` entry points and `if __name__ == "__main__"` guards. Validate agent IDs with the existing letters/numbers/hyphen/underscore rule before writing shared files. Prefer `pathlib.Path` for filesystem work and keep JSONL records shaped as `{"ts": "...", "from": "...", "msg": "..."}`.

## Testing Guidelines

Tests are written with `unittest` and should be placed under `tests/` as `test_<feature>.py`. Use temporary directories for filesystem cases and avoid writing to real user config unless the behavior being tested requires expansion logic. Add focused regression tests for locking, archive rotation, config parsing, HTTP endpoints, and message validation.

## Commit & Pull Request Guidelines

Recent history uses concise Conventional Commit-style subjects, often with scopes, such as `fix(install): ...`, `refactor(install): ...`, and `docs: ...`. Keep commits small and action-oriented. Pull requests should include a short summary, test results, linked issues when relevant, and screenshots or notes for UI changes. Mention any installer, startup, or configuration migration impact explicitly.

## Security & Configuration Tips

Do not commit generated `bridge.yaml`, tokens, logs, or shared message data. Treat webhook URLs, bearer token paths, and `~/.agent-bridge` contents as user-local configuration. When touching HTTP handlers, preserve localhost-oriented defaults and path traversal checks.
