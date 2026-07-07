# Agent Bridge

> 基于共享文件的轻量多 Agent 异步对话与调试工具。

Agent Bridge 使用 Room、active.jsonl 与 Runtime 组织多个 Agent 的异步互动。Adapter 仅负责接入不同 Agent，不拥有独立消息中心。

## 快速开始

```bash
python -m pip install -r requirements.txt
python ui/server.py --open
```

默认地址：`http://127.0.0.1:8825`。

## 运行边界

适合：本地 Agent 协作、角色对话、人工介入与回放。

不适合：高并发生产消息系统、跨机器网络盘强一致、完整多租户权限隔离或企业级死信队列。

## 测试

```bash
python -m unittest discover -s tests
```

## 许可证

MIT
