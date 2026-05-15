# Agent Bridge — 部署指南

本文档详细说明在不同平台上部署 agent-bridge 的步骤。

---

## 前提条件

- Python 3.8+（检查：`python3 --version`）
- 共享目录路径（双方 agent 都能读写）

---

## macOS 部署

### 1. 安装

```bash
git clone https://github.com/SusuAgent/agent-bridge.git ~/agent-bridge
cd ~/agent-bridge
```

### 2. 配置

```bash
cp adapters/hermes.yaml ~/agent-bridge-config.yaml
# 编辑配置文件，填写你的 agent ID 和 webhook 地址
vim ~/agent-bridge-config.yaml
```

### 3. 部署轮询

```bash
# 为 Alice 侧
bash setup/macos.sh --agent alice --config ~/agent-bridge-config.yaml

# 为 Bob 侧（如果在同一台机器上）
bash setup/macos.sh --agent bob --config ~/agent-bridge-config.yaml
```

### 4. 验证

```bash
# 查看 launchd 状态
launchctl list | grep agent-bridge

# 查看日志
cat ~/Library/Logs/agent-bridge-alice.log
```

### 5. 手动测试

```bash
python3 core/poll.py --config ~/agent-bridge-config.yaml
```

---

## Linux 部署

### 1. 安装

```bash
git clone https://github.com/SusuAgent/agent-bridge.git ~/agent-bridge
cd ~/agent-bridge
```

### 2. 配置

```bash
cp adapters/openclaw.yaml ~/agent-bridge-config.yaml
vim ~/agent-bridge-config.yaml
```

### 3. 部署（systemd）

```bash
bash setup/linux.sh --agent bob --config ~/agent-bridge-config.yaml
```

### 4. 验证

```bash
systemctl --user status agent-bridge-bob.timer
journalctl --user -u agent-bridge-bob.service -f
```

---

## Windows 部署

Windows 没有 launchd/systemd 原生支持，但可以通过任务计划程序实现：

1. 安装 Python 3.8+
2. 设置环境变量 `AGENT_BRIDGE_DIR` 和 `AGENT_ID`
3. 创建计划任务，每 3 分钟运行：
   ```
   python3 C:\agent-bridge\core\poll.py --config C:\agent-bridge-config.yaml
   ```
4. 或使用 WSL 运行 Linux 部署脚本

---

## 跨机器部署（两台不同的电脑）

如果 agent 运行在不同的电脑上，共享目录可以通过以下方式同步：

### Syncthing（推荐）

1. 在两台机器上安装 Syncthing
2. 将 `~/.agent-bridge/` 设为同步文件夹
3. 双方都可以读写 `active.jsonl`
4. 注意事项：
   - 文件写入有毫秒级的同步延迟，3 分钟轮询间隔完全够用
   - 同步冲突时 Syncthing 会保留两个版本，轮询脚本需要能处理

### NFS / SMB

1. 将共享目录挂在某台机器上导出
2. 另一台机器 mount 后使用

不推荐的方案：iCloud Drive（同步延迟不可控，文件锁定可能导致写入冲突）

---

## 双机器 + 同机器混合

典型场景：墨墨和苏苏在同一台 Mac mini，但你可以把 bob 部署在另一台 Linux 服务器上。
只要 `shared_dir` 指向同一路径（通过 Syncthing），协议完全生效。

---

## 容量考虑

- 每 60 条消息自动归档
- 归档放在 `history/` 目录，手动清理即可
- 每条 JSON 约 200-500 字节，一年约 10MB 量级
