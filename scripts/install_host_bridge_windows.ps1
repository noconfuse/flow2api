param(
    [string]$TaskName = "Flow2API Host Bridge",
    [string]$BindHost = "0.0.0.0",
    [string]$Port = "8765",
    [string]$ChromePath = ""
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$LogDir = Join-Path $RepoRoot "tmp\host-bridge"
$StartCmd = Join-Path $ScriptDir "start_host_bridge_windows.cmd"
$PythonCmd = "py -3"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not $ChromePath) {
    $Candidates = @(
        "$Env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "$Env:ProgramFiles(x86)\Google\Chrome\Application\chrome.exe",
        "$Env:LocalAppData\Google\Chrome\Application\chrome.exe",
        "$Env:ProgramFiles\Chromium\Application\chrome.exe",
        "$Env:ProgramFiles(x86)\Chromium\Application\chrome.exe"
    )
    $ChromePath = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if (-not $ChromePath -or -not (Test-Path $ChromePath)) {
    throw "未找到 Chrome/Chromium，请安装 Chrome，或用 -ChromePath 显式指定 chrome.exe 路径。"
}

$CmdLines = @(
    "@echo off",
    "setlocal",
    "cd /d ""$RepoRoot""",
    "if not exist ""tmp\host-bridge"" mkdir ""tmp\host-bridge""",
    "set FLOW2API_BROWSER_LAUNCH_HOST_BIND=$BindHost",
    "set FLOW2API_BROWSER_LAUNCH_HOST_PORT=$Port",
    "set FLOW2API_CHROME_PATH=$ChromePath",
    "$PythonCmd scripts\browser_profile_host_bridge.py >> tmp\host-bridge\stdout.log 2>> tmp\host-bridge\stderr.log"
)

Set-Content -Path $StartCmd -Value ($CmdLines -join "`r`n") -Encoding ASCII

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$StartCmd`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host "已注册并启动任务: $TaskName"
Write-Host "Chrome 路径: $ChromePath"
Write-Host "启动脚本: $StartCmd"
Write-Host "健康检查: http://127.0.0.1:$Port/health"
Write-Host "stdout 日志: $(Join-Path $LogDir 'stdout.log')"
Write-Host "stderr 日志: $(Join-Path $LogDir 'stderr.log')"
