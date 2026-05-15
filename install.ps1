# ═══════════════════════════════════════════════════════════
# Agent Bridge — 一键安装脚本 (Windows PowerShell)
#
# 用法:
#   powershell -c "irm https://raw.githubusercontent.com/SusuAgent/agent-bridge/main/install.ps1 | iex"
# ═══════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"

$Repo = "SusuAgent/agent-bridge"
$Branch = "main"
$InstallDir = "$env:USERPROFILE\.agent-bridge"
$SrcDir = "$InstallDir\src"

function Write-Ok($msg)   { Write-Host "  " -NoNewline; Write-Host "✓" -ForegroundColor Green -NoNewline; Write-Host " $msg" }
function Write-Err($msg)  { Write-Host "  " -NoNewline; Write-Host "✗" -ForegroundColor Red -NoNewline; Write-Host " $msg" }
function Write-Info($msg) { Write-Host "  " -NoNewline; Write-Host "→" -ForegroundColor Cyan -NoNewline; Write-Host " $msg" }

# ─── 检查 Python 3 ───
function Check-Python {
    $py = $null
    try { $py = Get-Command python3 -ErrorAction Stop } catch {}
    if (-not $py) {
        try { $py = Get-Command python -ErrorAction Stop } catch {}
    }
    if (-not $py) {
        Write-Err "未找到 Python 3"
        Write-Host ""
        Write-Host "  请先安装 Python 3.8+: https://www.python.org/downloads/"
        exit 1
    }

    $ver = & $py.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $parts = $ver.Split(".")
    $maj = [int]$parts[0]
    $min = [int]$parts[1]

    if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 8)) {
        Write-Err "Python 版本过低: $ver (需要 >= 3.8)"
        exit 1
    }

    Write-Ok "Python $ver ($($py.Source))"
    return $py.Source
}

# ─── 检查 git ───
function Check-Git {
    try {
        Get-Command git -ErrorAction Stop | Out-Null
    } catch {
        Write-Err "未找到 git"
        Write-Host "  请先安装 git: https://git-scm.com/download/win"
        exit 1
    }
}

# ─── 下载代码 ───
function Download-Code {
    if (Test-Path "$SrcDir\.git") {
        Write-Info "更新已有安装..."
        Push-Location $SrcDir
        git fetch origin $Branch --quiet
        git reset --hard "origin/$Branch" --quiet
        Pop-Location
    } else {
        Write-Info "克隆仓库..."
        if (Test-Path $SrcDir) { Remove-Item $SrcDir -Recurse -Force }
        git clone --depth 1 --branch $Branch "https://github.com/$Repo.git" $SrcDir --quiet
    }
    Write-Ok "代码就绪: $SrcDir"
}

# ─── 安装 bridge 命令 ───
function Install-Cli {
    $binDir = "$env:USERPROFILE\.local\bin"
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null

    # 创建 wrapper 脚本
    $wrapper = "@echo off`n$pyPath `"$SrcDir\cli\bridge`" %*"
    $bridgeBat = "$binDir\bridge.bat"
    Set-Content -Path $bridgeBat -Value $wrapper -Encoding ASCII
    Write-Ok "CLI 已安装: $bridgeBat"

    # 检查 PATH
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$binDir*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
        $env:Path += ";$binDir"
        Write-Ok "已添加到用户 PATH"
    }
}

# ─── 主流程 ───
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "Agent Bridge 安装程序" -ForegroundColor White
Write-Host "  ────────────────────────"
Write-Host ""

$pyPath = Check-Python
Check-Git
Download-Code
Install-Cli

Write-Host ""
Write-Host "  " -NoNewline; Write-Host "安装完成！" -ForegroundColor Green
Write-Host ""
Write-Host "  初始化配置:  bridge setup"
Write-Host "  启动服务:    bridge start"
Write-Host "  查看帮助:    bridge --help"
Write-Host ""
Write-Host "  文档: https://github.com/$Repo"
Write-Host ""
