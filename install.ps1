# ═══════════════════════════════════════════════════════════
# Agent Bridge — Installer (Windows PowerShell)
#
# Usage:
#   powershell -c "irm https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.ps1 | iex"
# ═══════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"

$Repo = "SusuAgent/agent-bridge"
$Branch = "main"
$InstallDir = "$env:USERPROFILE\.agent-bridge"
$SrcDir = "$InstallDir\src"

function Write-Ok($msg)   { Write-Host "  " -NoNewline; Write-Host "OK" -ForegroundColor Green -NoNewline; Write-Host "  $msg" }
function Write-Err($msg)  { Write-Host "  " -NoNewline; Write-Host "ERR" -ForegroundColor Red -NoNewline; Write-Host " $msg" }
function Write-Info($msg) { Write-Host "  " -NoNewline; Write-Host "..." -ForegroundColor Cyan -NoNewline; Write-Host " $msg" }

function Main {
    Write-Host ""
    Write-Host "  Agent Bridge Installer"
    Write-Host "  ---"

    # --- Check Python ---
    $pyPath = $null
    try { $pyPath = Get-Command python3 -ErrorAction Stop } catch {}
    if (-not $pyPath) {
        try { $pyPath = Get-Command python -ErrorAction Stop } catch {}
    }
    if (-not $pyPath) {
        Write-Err "Python 3 not found"
        Write-Host ""
        Write-Host "    Install Python 3.8+: https://www.python.org/downloads/"
        Write-Host "    (check 'Add Python to PATH' during installation)"
        return
    }

    $ver = & $pyPath.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $parts = $ver.Split(".")
    $maj = [int]$parts[0]
    $min = [int]$parts[1]
    if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 8)) {
        Write-Err "Python version too old: $ver (need >= 3.8)"
        return
    }
    Write-Ok "Python $ver"

    # --- Download source ---
    Write-Info "Downloading source..."
    $zipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
    $zipPath = "$env:TEMP\agent-bridge.zip"
    Remove-Item -Path $SrcDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $zipPath -ErrorAction SilentlyContinue
    try {
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
    } catch {
        Write-Err "Failed to download source"
        Write-Host "    Could not reach github.com. Check:"
        Write-Host "    - Internet connection"
        Write-Host "    - Firewall / proxy settings"
        Write-Host "    - DNS resolution"
        return
    }
    Expand-Archive -Path $zipPath -DestinationPath $InstallDir -Force
    Remove-Item -Path $zipPath -Force
    $extracted = "$InstallDir\agent-bridge-$Branch"
    Rename-Item -Path $extracted -NewName "src" -Force
    Write-Ok "Source ready: $SrcDir"

    # --- Install bridge command ---
    Write-Info "Installing bridge command..."
    $binDir = "$env:USERPROFILE\.local\bin"
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
    $bridgeBat = "$binDir\bridge.bat"
    "@echo off
python `"$SrcDir\cli\bridge`" %*" | Out-File -FilePath $bridgeBat -Encoding ASCII -Force
    Write-Ok "Command installed: $bridgeBat"

    # --- Update PATH ---
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$binDir*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
        $env:Path += ";$binDir"
        Write-Ok "PATH updated (restart terminal to apply)"
    }

    # --- Done ---
    Write-Host ""
    Write-Host "  Install complete!"
    Write-Host ""
    Write-Host "    Close this window, open a new PowerShell, then run:"
    Write-Host "      bridge setup"
    Write-Host "      bridge start"
    Write-Host ""
}

# --- Entry point ---
try {
    Main
} catch {
    Write-Err "Install failed: $_"
    Write-Host ""
    Write-Host "    Make sure Python 3.8+ is installed:"
    Write-Host "      https://www.python.org/downloads/"
    Write-Host "    (check 'Add Python to PATH' during installation)"
    Write-Host ""
    Write-Host "    Then restart terminal and run the install command again."
}
