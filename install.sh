#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# Agent Bridge — Installer (macOS / Linux)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.sh | bash
# ═══════════════════════════════════════════════════════════
set -euo pipefail

REPO="SusuAgent/agent-bridge"
BRANCH="main"
INSTALL_DIR="$HOME/.agent-bridge"
SRC_DIR="$INSTALL_DIR/src"
BIN_DIR="$HOME/.local/bin"
BRIDGE_BIN="$BIN_DIR/bridge"

G='\033[32m'; R='\033[31m'; C='\033[36m'; B='\033[1m'; N='\033[0m'

ok()   { echo -e "  ${G}OK${N}  $1"; }
err()  { echo -e "  ${R}ERR${N} $1"; }
info() { echo -e "  ${C}...${N} $1"; }

# ─── Check Python ───
check_python() {
    if command -v python3 &>/dev/null; then
        PY="$(command -v python3)"
    elif command -v python &>/dev/null; then
        PY="$(command -v python)"
    else
        err "Python 3 not found"
        echo ""
        echo "    Install Python 3.8+:"
        echo "      macOS:  brew install python3"
        echo "      Ubuntu: sudo apt install python3"
        echo "      Arch:   sudo pacman -S python"
        exit 1
    fi

    VER=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    MAJ=$(echo "$VER" | cut -d. -f1)
    MIN=$(echo "$VER" | cut -d. -f2)
    if [ "$MAJ" -lt 3 ] || { [ "$MAJ" -eq 3 ] && [ "$MIN" -lt 8 ]; }; then
        err "Python version too old: $VER (need >= 3.8)"
        exit 1
    fi
    ok "Python $VER"
}

# ─── Download source ───
download() {
    info "Downloading source..."
    rm -rf "$SRC_DIR"
    mkdir -p "$INSTALL_DIR"
    TARBALL_URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
    TMP_TGZ=$(mktemp /tmp/agent-bridge.XXXXXX.tar.gz)
    trap "rm -f '$TMP_TGZ'" EXIT
    if ! curl -fsSL "$TARBALL_URL" -o "$TMP_TGZ"; then
        err "Cannot continue — unable to download source code from GitHub"
        echo ""
        echo "    URL: $TARBALL_URL"
        echo ""
        echo "    Make sure this machine can reach github.com."
        echo ""
        echo "    Common fixes:"
        echo "    - Check internet connection"
        echo "    - If behind a proxy, set HTTP_PROXY/HTTPS_PROXY:"
        echo "        export HTTP_PROXY=http://proxy-address:port"
        echo "        export HTTPS_PROXY=http://proxy-address:port"
        echo "    - Try pinging github.com to verify connectivity"
        echo "    - Disable VPN / firewall temporarily"
        exit 1
    fi
    if ! tar xzf "$TMP_TGZ" -C "$INSTALL_DIR"; then
        err "Cannot continue — unable to extract source archive"
        exit 1
    fi
    rm -f "$TMP_TGZ"
    trap - EXIT
    mv "$INSTALL_DIR/agent-bridge-$BRANCH" "$SRC_DIR"
    ok "Source ready: $SRC_DIR"
}

# ─── Install bridge command ───
install_cli() {
    info "Installing bridge command..."
    mkdir -p "$BIN_DIR"

    cat > "$BRIDGE_BIN" << EOF
#!/usr/bin/env bash
exec $PY $SRC_DIR/cli/bridge "\$@"
EOF
    chmod +x "$BRIDGE_BIN"
    ok "Command installed: $BRIDGE_BIN"

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *)
            info "$BIN_DIR not in PATH"
            SHELL_RC=""
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
                ok "Added to $SHELL_RC (restart terminal)"
            else
                echo ""
                echo "    Add to PATH manually:"
                echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
            fi
            ;;
    esac
}

# ─── Done ───
print_done() {
    echo ""
    echo "  Install complete!"
    echo ""
    echo "    Run:"
    echo -e "      ${C}bridge start${N}"
    echo ""
    echo "    The WebUI will open automatically. Complete Agent settings there."
    echo ""
    echo "    Docs: https://github.com/$REPO"
    echo ""
}

# ─── Main ───
echo ""
echo "  Agent Bridge Installer"
echo "  ---"
echo ""

check_python
download

info "Installing Python dependencies..."
PIP_OUTPUT=$("$PY" -m pip --disable-pip-version-check install --user -r "$SRC_DIR/requirements.txt" 2>&1) || {
    err "Python dependency installation failed"
    if [ -n "$PIP_OUTPUT" ]; then
        echo ""
        echo "    pip output:"
        printf '%s\n' "$PIP_OUTPUT" | sed 's/^/      /'
    fi
    echo ""
    echo "    Try manually:"
    echo "      $PY -m pip --disable-pip-version-check install --user -r $SRC_DIR/requirements.txt"
    exit 1
}
ok "Dependencies installed"

install_cli
print_done
