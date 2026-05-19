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

function Find-Python {
    $candidates = @(
        @{ Command = "python3"; Args = @() },
        @{ Command = "python"; Args = @() },
        @{ Command = "py"; Args = @("-3") }
    )

    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if (-not $cmd) {
            continue
        }

        $args = @($candidate.Args)
        $version = $null
        try {
            $version = & $cmd.Source @args -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        } catch {
            continue
        }

        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($version)) {
            continue
        }

        $version = $version.Trim()
        if ($version -notmatch '^\d+\.\d+$') {
            continue
        }

        return [PSCustomObject]@{
            Source = $cmd.Source
            Args = $args
            Version = $version
        }
    }

    return $null
}

function Format-PythonCommand($pythonInfo) {
    $parts = @($pythonInfo.Source) + @($pythonInfo.Args)
    return ($parts | ForEach-Object {
        if ($_ -match '\s') { "`"$_`"" } else { $_ }
    }) -join " "
}

function Main {
    Write-Host ""
    Write-Host "  Agent Bridge Installer"
    Write-Host "  ---"

    # --- Check Python ---
    $pythonInfo = Find-Python
    if (-not $pythonInfo) {
        Write-Err "Python 3 not found"
        Write-Host ""
        Write-Host "    Install Python 3.8+: https://www.python.org/downloads/"
        Write-Host "    (check 'Add Python to PATH' during installation)"
        Write-Host ""
        Write-Host "    If Python is installed from python.org, restart PowerShell and try:"
        Write-Host "      python --version"
        Write-Host "      py -3 --version"
        Write-Host ""
        Write-Host "    If Windows opens Microsoft Store instead, disable App Execution Aliases:"
        Write-Host "      Settings > Apps > Advanced app settings > App execution aliases"
        return
    }

    $ver = $pythonInfo.Version
    $parts = $ver.Split(".")
    $maj = [int]$parts[0]
    $min = [int]$parts[1]
    if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 8)) {
        Write-Err "Python version too old: $ver (need >= 3.8)"
        return
    }
    $pythonCmd = Format-PythonCommand $pythonInfo
    Write-Ok "Python $ver ($pythonCmd)"

    # --- Download source ---
    Write-Info "Downloading source..."
    $zipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
    $zipPath = "$env:TEMP\agent-bridge.zip"
    Remove-Item -Path $SrcDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $zipPath -ErrorAction SilentlyContinue
    try {
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
    } catch {
        Write-Err "Cannot continue —unable to download source code from GitHub"
        Write-Host "    URL: $zipUrl"
        Write-Host ""
        Write-Host "    Make sure this VM has internet access and can reach github.com."
        Write-Host ""
        Write-Host "    Common fixes:"
        Write-Host "    - Check VM network settings (NAT / Bridged)"
        Write-Host "    - If behind a proxy, set environment variables first:"
        Write-Host "        `$env:HTTP_PROXY=`"http://proxy-address:port`""
        Write-Host "        `$env:HTTPS_PROXY=`"http://proxy-address:port`""
        Write-Host "    - Try pinging github.com to verify connectivity"
        Write-Host "    - Disable VPN / firewall temporarily"
        return
    }
    Expand-Archive -Path $zipPath -DestinationPath $InstallDir -Force
    Remove-Item -Path $zipPath -Force
    $extracted = "$InstallDir\agent-bridge-$Branch"
    Rename-Item -Path $extracted -NewName "src" -Force
    Write-Ok "Source ready: $SrcDir"

    # --- Install Python dependencies ---
    Write-Info "Installing Python dependencies..."
    try {
        & $pythonInfo.Source @($pythonInfo.Args) -m pip --disable-pip-version-check install --user -r "$SrcDir\requirements.txt"
    } catch {
        Write-Err "Python dependency installation failed"
        Write-Host ""
        Write-Host "    Try manually:"
        Write-Host "      $pythonCmd -m pip --disable-pip-version-check install --user -r `"$SrcDir\requirements.txt`""
        return
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Python dependency installation failed"
        Write-Host ""
        Write-Host "    Try manually:"
        Write-Host "      $pythonCmd -m pip --disable-pip-version-check install --user -r `"$SrcDir\requirements.txt`""
        return
    }
    Write-Ok "Dependencies installed"

    # --- Install bridge command ---
    Write-Info "Installing bridge command..."
    $binDir = "$env:USERPROFILE\.local\bin"
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
    $bridgeBat = "$binDir\bridge.bat"
    "@echo off
$pythonCmd `"$SrcDir\cli\bridge`" %*" | Out-File -FilePath $bridgeBat -Encoding ASCII -Force
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
    Write-Host "      bridge start"
    Write-Host ""
    Write-Host "    The WebUI will open automatically. Complete Agent settings there."
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
