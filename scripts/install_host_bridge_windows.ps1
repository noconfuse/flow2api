param(
    [string]$TaskName = "Flow2API Host Bridge",
    [string]$BindHost = "0.0.0.0",
    [string]$Port = "8765",
    [string]$ChromePath = "",
    [ValidateSet("auto", "task", "run")]
    [string]$InstallMode = "auto"
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
    throw "Chrome/Chromium not found. Install Chrome or pass -ChromePath with the full chrome.exe path."
}

$CmdLines = @(
    '@echo off',
    'setlocal',
    "cd /d ""$RepoRoot""",
    'if not exist "tmp\host-bridge" mkdir "tmp\host-bridge"',
    "set FLOW2API_BROWSER_LAUNCH_HOST_BIND=$BindHost",
    "set FLOW2API_BROWSER_LAUNCH_HOST_PORT=$Port",
    "set FLOW2API_CHROME_PATH=$ChromePath",
    "$PythonCmd scripts\browser_profile_host_bridge.py >> tmp\host-bridge\stdout.log 2>> tmp\host-bridge\stderr.log"
)

Set-Content -Path $StartCmd -Value ($CmdLines -join "`r`n") -Encoding ASCII

function Install-WithScheduledTask {
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
    return "scheduled-task"
}

function Install-WithRunKey {
    $RunKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $RunCommand = "cmd.exe /c `"$StartCmd`""
    New-Item -Path $RunKeyPath -Force | Out-Null
    New-ItemProperty -Path $RunKeyPath -Name $TaskName -PropertyType String -Value $RunCommand -Force | Out-Null
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$StartCmd`""
    return "user-run-key"
}

$InstalledMode = $null

if ($InstallMode -eq "task") {
    $InstalledMode = Install-WithScheduledTask
} elseif ($InstallMode -eq "run") {
    $InstalledMode = Install-WithRunKey
} else {
    try {
        $InstalledMode = Install-WithScheduledTask
    } catch {
        if ($_.Exception.Message -match "拒绝访问" -or $_.FullyQualifiedErrorId -match "0x80070005") {
            Write-Host "Scheduled task registration was denied. Falling back to HKCU Run startup."
            $InstalledMode = Install-WithRunKey
        } else {
            throw
        }
    }
}

Write-Host "Install mode: $InstalledMode"
Write-Host "Chrome path: $ChromePath"
Write-Host "Launcher script: $StartCmd"
Write-Host "Health check: http://127.0.0.1:$Port/health"
Write-Host "stdout log: $(Join-Path $LogDir 'stdout.log')"
Write-Host "stderr log: $(Join-Path $LogDir 'stderr.log')"
