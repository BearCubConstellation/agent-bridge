#!/bin/bash
# Agent Bridge — Linux 部署脚本 (systemd + cron fallback)
#
# 优先使用 systemd timer（更可靠、可追溯），
# 没有 systemd 时退化到 cron。
#
# 用法:
#   bash setup/linux.sh --agent alice --config /path/to/config.yaml

set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_ID=""
CONFIG_PATH=""
POLL_SCRIPT="$BRIDGE_DIR/core/poll.py"
PYTHON="$(which python3 || which python || echo '/usr/bin/python3')"

# ─── 参数解析 ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)    AGENT_ID="$2"; shift 2 ;;
    --config)   CONFIG_PATH="$2"; shift 2 ;;
    --help|-h)  echo "Usage: $0 --agent <id> --config <path>"; exit 0 ;;
    *)          echo "Unknown: $1"; exit 1 ;;
  esac
done

if [[ -z "$AGENT_ID" || -z "$CONFIG_PATH" ]]; then
  echo "Error: --agent and --config are required"
  exit 1
fi

CONFIG_PATH="$(eval echo "$CONFIG_PATH")"

# ─── systemd user service ──────────────────────────────
if command -v systemctl &>/dev/null; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"

  # Service unit
  cat > "$UNIT_DIR/agent-bridge-$AGENT_ID.service" <<EOF
[Unit]
Description=Agent Bridge — poll for $AGENT_ID
After=network.target

[Service]
Type=oneshot
ExecStart=$PYTHON $POLL_SCRIPT --config $CONFIG_PATH --agent $AGENT_ID
WorkingDirectory=$BRIDGE_DIR
EOF

  # Timer unit
  cat > "$UNIT_DIR/agent-bridge-$AGENT_ID.timer" <<EOF
[Unit]
Description=Agent Bridge — $AGENT_ID poll timer (every 3 min)

[Timer]
OnBootSec=1min
OnUnitActiveSec=3min
Persistent=true

[Install]
WantedBy=timers.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now "agent-bridge-$AGENT_ID.timer"

  echo "✓ systemd timer installed for '$AGENT_ID' (every 3 minutes)"
  echo "  Status: systemctl --user status agent-bridge-$AGENT_ID.timer"
  echo "  Logs:   journalctl --user -u agent-bridge-$AGENT_ID.service -f"

# ─── cron fallback ────────────────────────────────────
elif command -v crontab &>/dev/null; then
  CRON_LINE="*/3 * * * * $PYTHON $POLL_SCRIPT --config $CONFIG_PATH >> \$HOME/.agent-bridge/poll-$AGENT_ID.log 2>&1"

  if crontab -l 2>/dev/null | grep -q "$AGENT_ID"; then
    echo "→ Cron entry for '$AGENT_ID' already exists, skipping"
  else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "✓ Cron installed for '$AGENT_ID' (every 3 minutes)"
  fi

  echo "  Logs: tail -f ~/.agent-bridge/poll-$AGENT_ID.log"

else
  echo "✗ No systemd or cron found. Install either and re-run."
  exit 1
fi

echo ""
echo "Done. To test: $PYTHON $POLL_SCRIPT --config $CONFIG_PATH"
