# Agent Bridge — 开发原则

> 任何 AI 或人类在修改本项目代码之前，必须先读完本文件。

---

## 一、这个项目是什么

Agent Bridge 是一个 **Agent 间异步通信中间件**。

它让两个独立运行的 AI Agent（如 Hermes Agent 和 OpenClaw）通过共享文件系统进行异步对话。不依赖消息队列、数据库、WebSocket 或任何外部服务——唯一的传输介质是文件系统上的 JSONL 文件。

### 核心原理

```
Agent A 写入消息 → active.jsonl ← Agent B 写入消息
                        │
        每 3 分钟由定时任务调度 poll.py 检查
                        │
            发现对方的新消息 → POST webhook 唤醒本地 Agent
            没有新消息     → 零 token 消耗，直接退出
```

**设计哲学：文件即协议。** 消息格式是 JSONL，游标是纯文本文件，归档是文件移动。所有状态都可以用 `cat` 和 `ls` 查看，用 `mv` 和 `rm` 管理。没有黑盒。

### 数据流

1. Agent A 调用 `send.py` 向 `active.jsonl` 追加一行 JSON
2. Agent B 的 `poll.py` 每 3 分钟检查文件，发现新消息
3. `poll.py` POST 到 Agent B 的 webhook 唤醒对方
4. Agent B 处理消息后调用 `send.py` 追加回复
5. Agent A 的 `poll.py` 在下一轮发现回复，唤醒 Agent A

### 目录职责

| 目录 | 职责 | 可否独立运行 |
|------|------|-------------|
| `core/` | 核心引擎：发送（send.py）、轮询（poll.py）、锁（lock.py） | 是，纯标准库 |
| `cli/bridge` | 命令行工具：setup/start/send/status 等 | 依赖 core/ |
| `ui/` | 本地 Web UI + API 服务 | 依赖 core/，可选 |
| `setup/` | macOS/Linux 部署脚本 | 依赖 core/ |
| `adapters/` | 各 Agent 框架的配置模板 | 参考文档 |
| `protocol/` | 通信协议规范 | 文档 |
| `docs/` | 详细文档 | 文档 |

---

## 二、最终目标

构建一个 **可靠、简单、零外部依赖** 的 Agent 间通信中间件，使得：

1. **消息不丢失** — 任何情况下（进程崩溃、网络中断、并发写入），已写入的消息都不会丢失
2. **消息不重复** — 同一条消息不会多次唤醒对方 Agent（浪费 LLM token）
3. **零资源空闲** — 没有新消息时，轮询脚本零 token、零网络请求，仅消耗文件读取
4. **双方独立** — 两个 Agent 不需要同时在线，一方写入后离线，另一方下次轮询时处理
5. **可观测** — 所有状态都是文件，用标准系统工具即可查看和调试
6. **框架无关** — 任何支持 webhook 入站和 CLI 出站的 Agent 均可接入
7. **容错** — webhook 失败时自动重试（默认 1 次，可通过 `wakeup.retry` 或环境变量 `AGENT_BRIDGE_RETRY` 配置），游标不更新，下次轮询时会再次尝试投递

---

## 三、技术约束

### 3.1 只用标准库

运行时依赖：Python 3.8+，仅标准库。`pyyaml` 是可选增强（未安装时自动降级为 JSON）。

**禁止引入**：
- 外部消息队列（RabbitMQ、Redis、NSQ）
- 数据库（SQLite 也不行）
- Web 框架（Flask、FastAPI — 当前用 `http.server`）
- 异步框架（asyncio 在本项目中没有收益，轮询本身就是低频操作）

### 3.2 只用已验证的技术

本项目中使用的每一项技术都必须满足以下条件之一：

- **在项目中已有工作代码并经过测试验证**（如 `fcntl.flock` 文件锁、`shutil.move` 归档）
- **是 Python 标准库的稳定 API**，在 3.8+ 全系列可用
- **在 openclaw-lessons 中有踩坑记录和已验证的解决方案**

**禁止使用**：
- 未经验证的并发方案（如 `asyncio` + 文件 IO 混用，在本项目中没有先例）
- 需要额外安装的系统级依赖（如 `inotify`、`watchdog`）
- 需要编译的 C 扩展
- "看起来很酷"但没人用过的新 API

### 3.3 文件操作的硬规则

1. **写 active.jsonl 必须加文件锁**（`.active.lock`）— 否则并发写入会交叉
2. **归档 active.jsonl 必须同时持有 `.active.lock` 和 `.archive.lock`** — 否则发送方和归档方竞态（当前代码未实现，是待修复项）
3. **游标只在 webhook 成功后更新** — 否则网络故障导致消息被跳过永不重试（注意：protocol/SPEC.md 4.2 节描述与此相反，以代码和本文档为准，SPEC 待同步修正）
4. **归档后必须重置行号游标** — 否则新 active.jsonl 的消息被跳过
5. **JSONL 解析必须逐行 try/catch** — 否则一行损坏导致整个文件无法读取
6. **禁止 `sys.exit()` 在库函数中调用** — `send()` 和 `load_config()` 等被 server.py 等长期运行进程导入，`sys.exit` 会杀掉整个进程。改为抛异常，由调用方决定如何处理（当前 `send()` 仍在违反，是待修复项）
7. **多锁场景按固定顺序加锁**：先 `.active.lock`，再 `.archive.lock` — 防止死锁

### 3.4 安全硬规则

1. **所有 HTTP 服务绑定 `127.0.0.1`** — 禁止绑定 `0.0.0.0`
2. **路径遍历防护用 `is_relative_to()`** — 不用字符串前缀匹配
3. **Agent ID 必须通过正则校验**（`^[a-zA-Z0-9_-]+$`）— 防止路径注入
4. **Token 从文件读取，不硬编码在脚本中**
5. **Shell 脚本禁止 `eval`** — 用参数展开替代

---

## 四、架构原则

### 4.1 配置单一真相源

`bridge.yaml` 是唯一的配置真相源。CLI、UI、poll 脚本都从同一个文件读取。UI 编辑配置后写回 `bridge.yaml`，CLI 和 poll 下次读取时自然获得更新。

**禁止**在代码中硬编码配置默认值后不再读取文件。

### 4.2 核心引擎无头可运行

`core/send.py` 和 `core/poll.py` 不依赖 UI，不依赖 CLI，不依赖任何图形化工具。它们是纯命令行工具，可以被 cron/launchd/systemd 直接调度。

UI（`ui/server.py`）是可选的增强，不是必需品。

### 4.3 每个模块一个职责

| 模块 | 职责 | 不做的事 |
|------|------|---------|
| `send.py` | 向 active.jsonl 追加一条消息 | 不负责轮询、唤醒、归档 |
| `poll.py` | 检查新消息、唤醒对方、自动归档 | 不负责发送消息 |
| `lock.py` | 提供跨平台文件锁原语 | 不关心业务逻辑 |
| `server.py` | HTTP API + 后台轮询线程 + 静态文件 | 不重新实现 core/ 的逻辑，而是导入调用 |

### 4.4 不重复造轮子

`server.py` 不自己实现 JSONL 解析——它导入 `poll.parse_jsonl`。
`cli/bridge` 不自己实现发送——它导入 `send.send`。

如果在两个地方写了相同的逻辑，说明需要提取到公共模块。

---

## 五、修改检查清单

每次提交代码前，确认以下各项：

- [ ] `python3 -m unittest discover -s tests -v` 全部通过
- [ ] 没有引入新的外部依赖（标准库和 pyyaml 除外）
- [ ] 没有在库函数中调用 `sys.exit()`
- [ ] 涉及文件写入的地方都使用了文件锁
- [ ] 涉及文件路径的地方都做了路径遍历防护
- [ ] 修改配置格式时，同步更新了 `protocol/SPEC.md` 和 `docs/CUSTOMIZE.md`
- [ ] `bridge.yaml` 仍然是唯一的配置真相源
- [ ] 如果修改了消息格式（JSONL 字段），SPEC.md 已同步更新
