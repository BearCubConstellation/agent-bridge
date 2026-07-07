# Agent Bridge

> 一个基于共享文件的本地多 Agent 异步对话与调试工具。

Agent Bridge 用共享目录保存消息，通过轮询、HTTP 回调和 MCP 等方式驱动多个 Agent 协作。它适合本机或可信环境中的多 Agent 联调、角色对话和工作流实验，不是生产级消息队列或多租户平台。

## 快速开始

```bash
python -m pip install -r requirements.txt
python ui/server.py --open
```

默认地址：`http://127.0.0.1:8825`。

常用命令：

```bash
bridge start
bridge status
bridge open
bridge stop
```

## 运行边界

适合：本地 Agent 协作、回调链路调试、轮询式异步对话、人工介入与回放。

不适合：高并发生产消息系统、跨机器网络盘强一致、需要完整权限隔离、审计或死信队列的场景。

## 测试

```bash
python -m unittest discover -s tests
```

## 许可证

MIT
