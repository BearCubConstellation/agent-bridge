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

# ─── 主流程 ───
function Main {
    Write-Host ""
    Write-Host "  " -NoNewline; Write-Host "Agent Bridge 安装程序" -ForegroundColor White
    Write-Host "  ────────────────────────"
    Write-Host ""

    $pyPath = $null
    try { $pyPath = Get-Command python3 -ErrorAction Stop } catch {}
    if (-not $pyPath) {
        try { $pyPath = Get-Command python -ErrorAction Stop } catch {}
    }
    if (-not $pyPath) {
        Write-Err "未找到 Python 3"
        Write-Host ""
        Write-Host "  请先安装 Python 3.8+: https://www.python.org/downloads/"
        Write-Host "  （安装时勾选 'Add Python to PATH'）"
        return
    }

    $ver = & $pyPath.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $parts = $ver.Split(".")
    $maj = [int]$parts[0]
    $min = [int]$parts[1]
    if ($maj -lt 3 -or ($maj -eq 3 -and $min -lt 8)) {
        Write-Err "Python 版本过低: $ver (需要 >= 3.8)"
        return
    }
    Write-Ok "Python $ver ($($pyPath.Source))"

    # 检查 git
    $gitPath = $null
    try { $gitPath = Get-Command git -ErrorAction Stop } catch {}
    if (-not $gitPath) {
        Write-Err "未找到 git"
        Write-Host "  请先安装 git: https://git-scm.com/download/win"
        return
    }
    Write-Ok "Git 已安装"

    # 下载代码
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

    # 安装 bridge 命令
    $binDir = "$env:USERPROFILE\.local\bin"
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
    $bridgeBat = "$binDir\bridge.bat"
    "@echo off
python `"$SrcDir\cli\bridge`" %*" | Out-File -FilePath $bridgeBat -Encoding ASCII -Force
    Write-Ok "CLI 已安装: $bridgeBat"

    # PATH
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$binDir*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
        $env:Path += ";$binDir"
        Write-Ok "已添加到用户 PATH（新窗口生效）"
    }

    Write-Host ""
    Write-Host "  " -NoNewline; Write-Host "安装完成！" -ForegroundColor Green
    Write-Host ""
    Write-Host "  请关闭当前窗口，打开新 PowerShell 后运行："
    Write-Host "    bridge setup"
    Write-Host "    bridge start"
    Write-Host ""
    Write-Host "  文档: https://github.com/$Repo"
    Write-Host ""
}

# 入口
try {
    Main
} catch {
    Write-Err "安装失败: $_"
    Write-Host ""
    Write-Host "  请确认已安装:"
    Write-Host "  - Python 3.8+ (https://www.python.org/downloads/)"
    Write-Host "  - Git (https://git-scm.com/download/win)"
    Write-Host ""
    Write-Host "  安装后重启终端，重新运行安装命令。"
}
