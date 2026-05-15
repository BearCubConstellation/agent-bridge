#!/bin/bash
# Agent Bridge — macOS 部署脚本 (launchd)
#
# 用法:
#   bash setup/macos.sh                    # 交互式配置
#   bash setup/macos.sh --agent alice \    # 快速配置
#     --config /path/to/config.yaml
#
# 为每个 agent 创建一个 launchd plist，每 3 分钟运行一次 poll 脚本。

set -euo pipefail

# ─── 配置 ──────────────────────────────────────────────
BRIDGE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_ID=""
CONFIG_PATH=""
POLL_SCRIPT="$BRIDGE_DIR/core/poll.py"

# ─── 参数解析 ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)    AGENT_ID="$2"; shift 2 ;;
    --config)   CONFIG_PATH="$2"; shift 2 ;;
    --help|-h)  echo "Usage: $0 [--agent <id>] [--config <path>]"; exit 0 ;;
    *)          echo "Unknown: $1"; exit 1 ;;
  esac
done

# ─── 交互配置 ──────────────────────────────────────────
if [[ -z "$AGENT_ID" ]]; then
  read -r -p "Agent ID (e.g. alice, bob, momo, susu): " AGENT_ID
fi
if [[ -z "$CONFIG_PATH" ]]; then
  read -r -p "Config path (e.g. ~/agent-bridge-config.yaml): " CONFIG_PATH
fi

# 解析路径
CONFIG_PATH="$(eval echo "$CONFIG_PATH")"
LABEL="com.agent-bridge.poll.$AGENT_ID"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

# ─── 创建 plist ────────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(command -v python3)</string>
    <string>$POLL_SCRIPT</string>
    <string>--config</string>
    <string>$CONFIG_PATH</string>
    <string>--agent</string>
    <string>$AGENT_ID</string>
  </array>
  <key>StartInterval</key>
  <integer>180</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/agent-bridge-$AGENT_ID.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/agent-bridge-$AGENT_ID.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
  </dict>
</dict>
</plist>
EOF

echo "→ Created: $PLIST_PATH"

# ─── 加载 ──────────────────────────────────────────────
launchctl load "$PLIST_PATH"
echo "→ Loaded: $LABEL (每 3 分钟运行一次)"

# ─── 验证 ──────────────────────────────────────────────
sleep 1
if launchctl list | grep -q "$LABEL"; then
  echo "✓ $AGENT_ID polling started successfully"
else
  echo "! Check status: launchctl list | grep $LABEL"
  echo "! Check logs: cat ~/Library/Logs/agent-bridge-$AGENT_ID.log"
fi

echo ""
echo "Done. Agent '$AGENT_ID' will poll for new messages every 180 seconds."
echo "To stop:  launchctl unload $PLIST_PATH"
echo "To debug: python3 $POLL_SCRIPT --config $CONFIG_PATH"
