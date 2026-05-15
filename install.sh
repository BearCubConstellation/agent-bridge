#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# Agent Bridge — 一键安装脚本 (macOS / Linux)
#
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.sh | bash
# ═══════════════════════════════════════════════════════════
set -euo pipefail

REPO="SusuAgent/agent-bridge"
BRANCH="main"
INSTALL_DIR="$HOME/.agent-bridge"
SRC_DIR="$INSTALL_DIR/src"
BIN_DIR="$HOME/.local/bin"
BRIDGE_BIN="$BIN_DIR/bridge"

# 颜色
G='\033[32m'; R='\033[31m'; C='\033[36m'; B='\033[1m'; N='\033[0m'

ok()   { echo "  ${G}✓${N} $1"; }
err()  { echo "  ${R}✗${N} $1"; }
info() { echo "  ${C}→${N} $1"; }

# ─── 检查 Python 3 ───
check_python() {
    if command -v python3 &>/dev/null; then
        PY="$(command -v python3)"
    elif command -v python &>/dev/null; then
        PY="$(command -v python)"
    else
        err "未找到 Python 3"
        echo ""
        echo "  请先安装 Python 3.8+:"
        echo "    macOS:  brew install python3"
        echo "    Ubuntu: sudo apt install python3"
        echo "    Arch:   sudo pacman -S python"
        exit 1
    fi

    # 检查版本 >= 3.8
    VER=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    MAJ=$(echo "$VER" | cut -d. -f1)
    MIN=$(echo "$VER" | cut -d. -f2)
    if [ "$MAJ" -lt 3 ] || { [ "$MAJ" -eq 3 ] && [ "$MIN" -lt 8 ]; }; then
        err "Python 版本过低: $VER (需要 >= 3.8)"
        exit 1
    fi
    ok "Python $VER ($PY)"
}

# ─── 检查 git ───
check_git() {
    if ! command -v git &>/dev/null; then
        err "未找到 git"
        echo "  请先安装 git"
        exit 1
    fi
}

# ─── 下载代码 ───
download() {
    if [ -d "$SRC_DIR/.git" ]; then
        info "更新已有安装..."
        cd "$SRC_DIR"
        git fetch origin "$BRANCH" --quiet
        git reset --hard "origin/$BRANCH" --quiet
    else
        info "克隆仓库..."
        rm -rf "$SRC_DIR"
        git clone --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" "$SRC_DIR" --quiet
    fi
    ok "代码就绪: $SRC_DIR"
}

# ─── 安装 bridge 命令 ───
install_cli() {
    mkdir -p "$BIN_DIR"

    # 创建 wrapper 脚本
    cat > "$BRIDGE_BIN" << EOF
#!/usr/bin/env bash
exec $PY $SRC_DIR/cli/bridge "\$@"
EOF
    chmod +x "$BRIDGE_BIN"
    ok "CLI 已安装: $BRIDGE_BIN"

    # 检查 PATH
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *)
            info "$BIN_DIR 不在 PATH 中"
            SHELL_RC=""
            # 优先检查当前 shell 的 rc 文件
            if [ -n "$ZSH_VERSION" ] && [ -f "$HOME/.zshrc" ]; then
                SHELL_RC="$HOME/.zshrc"
            elif [ -n "$BASH_VERSION" ] && [ -f "$HOME/.bashrc" ]; then
                SHELL_RC="$HOME/.bashrc"
            elif [ -f "$HOME/.zshrc" ]; then
                SHELL_RC="$HOME/.zshrc"
            elif [ -f "$HOME/.bashrc" ]; then
                SHELL_RC="$HOME/.bashrc"
            fi
            if [ -n "$SHELL_RC" ]; then
                echo "" >> "$SHELL_RC"
                echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$SHELL_RC"
                ok "已添加到 $SHELL_RC (新终端生效)"
            else
                echo ""
                echo "  请手动添加到 PATH:"
                echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
            fi
            ;;
    esac
}

# ─── 安装完成 ───
print_done() {
    echo ""
    echo "  ${B}══════════════════════════════════════${N}"
    echo "  ${G}安装完成！${N}"
    echo "  ${B}══════════════════════════════════════${N}"
    echo ""
    echo "  初始化配置:  ${C}bridge setup${N}"
    echo "  启动服务:    ${C}bridge start${N}"
    echo "  查看帮助:    ${C}bridge --help${N}"
    echo ""
    echo "  文档: https://github.com/$REPO"
    echo ""
}

# ─── 主流程 ───
echo ""
echo "  ${B}Agent Bridge 安装程序${N}"
echo "  ────────────────────────"
echo ""

check_python
check_git
download
install_cli
print_done
