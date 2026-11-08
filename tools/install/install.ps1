param(
    [string]$TargetPath = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$SrcPlugins = Join-Path $Here "plugins"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"

function Write-Head($t) {
    Write-Host ""
    Write-Host "==============================================" -ForegroundColor DarkCyan
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host "==============================================" -ForegroundColor DarkCyan
}

function Test-AstrBotDir([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { return $false }
    if (-not (Test-Path $p)) { return $false }
    $hasMain = Test-Path (Join-Path $p "main.py")
    $hasCore = Test-Path (Join-Path $p "astrbot")
    $hasData = Test-Path (Join-Path $p "data")
    return ($hasMain -and $hasCore -and $hasData)
}

function Find-AstrBot {
    $cands = New-Object System.Collections.Generic.List[string]
    if ($env:ASTRBOT_HOME) { $cands.Add($env:ASTRBOT_HOME) }
    foreach ($d in @("C:", "D:", "E:", "F:", "G:")) {
        if (Test-Path "$d\") {
            $cands.Add("$d\AstrBot")
            $cands.Add("$d\astrbot")
        }
    }
    $cands.Add((Join-Path $env:USERPROFILE "AstrBot"))
    $cands.Add((Join-Path $env:USERPROFILE "Desktop\AstrBot"))
    $cands.Add((Join-Path $env:USERPROFILE "Downloads\AstrBot"))

    foreach ($c in $cands) {
        if (Test-AstrBotDir $c) { return (Resolve-Path $c).Path }
    }
    return $null
}

Write-Head "Whale Mind 插件安装器 v0.1.0"

if (-not (Test-Path $SrcPlugins)) {
    Write-Host "找不到 plugins 目录，安装包不完整。" -ForegroundColor Red
    Write-Host "请重新下载压缩包并完整解压后再运行。" -ForegroundColor Yellow
    exit 1
}

$plugins = Get-ChildItem $SrcPlugins -Directory | Where-Object { $_.Name -like "astrbot_plugin_*" }
if ($plugins.Count -eq 0) {
    Write-Host "plugins 目录里没有插件，安装包不完整。" -ForegroundColor Red
    exit 1
}

Write-Host "待安装插件：$($plugins.Count) 个" -ForegroundColor Gray
foreach ($p in $plugins) { Write-Host "  - $($p.Name)" -ForegroundColor DarkGray }

# ---- 定位 AstrBot ----
if ($TargetPath) {
    $target = $TargetPath
} else {
    Write-Host ""
    Write-Host "正在查找 AstrBot 安装目录..." -ForegroundColor Gray
    $target = Find-AstrBot
}

if (-not $target -or -not (Test-AstrBotDir $target)) {
    Write-Host ""
    Write-Host "没能自动找到 AstrBot 目录。" -ForegroundColor Yellow
    Write-Host "请把 AstrBot 所在的文件夹（就是有 main.py 那一层）拖到这个窗口里，然后回车：" -ForegroundColor Yellow
    $typed = Read-Host "路径"
    $typed = $typed.Trim().Trim('"')
    if (-not (Test-AstrBotDir $typed)) {
        Write-Host "这个路径不对：找不到 main.py / astrbot / data 三者。" -ForegroundColor Red
        Write-Host "请确认你选的是解压后、包含 main.py 的那一层目录。" -ForegroundColor Yellow
        exit 1
    }
    $target = (Resolve-Path $typed).Path
}

$target = (Resolve-Path $target).Path
Write-Host ""
Write-Host "AstrBot 目录：$target" -ForegroundColor Green

$destPlugins = Join-Path $target "data\plugins"
if (-not (Test-Path $destPlugins)) {
    Write-Host ""
    Write-Host "警告：$destPlugins 不存在。" -ForegroundColor Yellow
    Write-Host "这说明 AstrBot 可能还没启动过一次（首次启动会自动建 data 目录）。" -ForegroundColor Yellow
    Write-Host "建议：先启动一次 AstrBot，看到面板后再回来运行本安装器。" -ForegroundColor Yellow
    $go = Read-Host "仍然要继续吗？(y/N)"
    if ($go -ne "y" -and $go -ne "Y") { exit 0 }
    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $destPlugins | Out-Null }
}

# ---- 安装 ----
Write-Host ""
Write-Host "开始安装..." -ForegroundColor Gray
$installed = @()
$skipped = @()

foreach ($p in $plugins) {
    $dst = Join-Path $destPlugins $p.Name
    if (Test-Path $dst) {
        $bak = "$dst.bak_$Stamp"
        Write-Host "  已存在 $($p.Name)，备份为 $(Split-Path -Leaf $bak)" -ForegroundColor Yellow
        if (-not $DryRun) { Move-Item $dst $bak -Force }
    } else {
        $skipped += $p.Name
    }
    if (-not $DryRun) {
        Copy-Item $p.FullName -Destination $dst -Recurse -Force
    }
    $installed += $p.Name
    Write-Host "  已安装 $($p.Name)" -ForegroundColor Green
}

# ---- 收尾 ----
Write-Head "安装完成"
Write-Host "目录：$destPlugins" -ForegroundColor Gray
Write-Host ""
Write-Host "下一步：" -ForegroundColor Cyan
Write-Host "  1. 重启 AstrBot（关掉再打开，或用你的启动脚本）" -ForegroundColor Gray
Write-Host "  2. 打开面板 → 插件，确认上面 $($installed.Count) 个插件都在" -ForegroundColor Gray
Write-Host "  3. 每个插件右侧有齿轮，点进去调整参数（默认值已能跑）" -ForegroundColor Gray
Write-Host ""
if ($installed -contains "astrbot_plugin_self_study") {
    Write-Host "注意：self_study 的 main.py 和 textnorm.py 必须放在同一目录。" -ForegroundColor Yellow
    Write-Host "      本安装器已整个文件夹复制，正常情况不用管。" -ForegroundColor Yellow
    Write-Host ""
}
Write-Host "已有的旧插件已备份为 *.bak_$Stamp，想还原就把新目录删掉、把备份改回原名。" -ForegroundColor DarkGray
if ($DryRun) { Write-Host "（本次是演练模式，没有真正写入任何文件）" -ForegroundColor Magenta }
