param(
    [Parameter(Mandatory)] [string]$RecipientOpenId,
    [Parameter(Mandatory)] [string]$RecipientChatId,
    [string]$Profile = 'codex',
    [string]$Proxy = 'http://127.0.0.1:7890',
    [string]$TaskName = 'LarkAutomation'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-CommandPath([string[]]$Names) {
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
    }
    throw "Command not found: $($Names -join ', ')"
}

$pwsh = Resolve-CommandPath @('pwsh.exe', 'pwsh')
$node = Resolve-CommandPath @('node.exe', 'node')
$larkCli = Resolve-CommandPath @('lark-cli.ps1', 'lark-cli.cmd', 'lark-cli')
$npmRoot = (& npm root -g).Trim()
$codexJs = Join-Path $npmRoot '@openai\codex\bin\codex.js'
if (-not (Test-Path -LiteralPath $codexJs)) { throw "Codex JavaScript entrypoint not found: $codexJs" }

$channelHome = Join-Path $HOME '.lark-channel'
$profileRoot = Join-Path $channelHome "profiles\$Profile"
$runtimeDir = Join-Path $channelHome 'automation'
$configPath = Join-Path $runtimeDir 'config.json'
$worker = Join-Path $PSScriptRoot 'Lark-Automation.ps1'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

[ordered]@{
    profile = $Profile
    recipientOpenId = $RecipientOpenId
    recipientChatId = $RecipientChatId
    proxy = $Proxy
    pwsh = $pwsh
    node = $node
    codexJs = $codexJs
    larkCli = $larkCli
    larkChannelHome = $channelHome
    larkChannelConfig = (Join-Path $profileRoot 'lark-cli-source\config.json')
    larkCliConfigDir = (Join-Path $profileRoot 'lark-cli')
    bridgeLogDir = (Join-Path $profileRoot 'logs')
    runtimeDir = $runtimeDir
    quotaTimes = @('00:00','10:00','12:00','14:00','16:00','18:00','20:00','22:00')
    shutdownTimes = @('00:30','01:00','01:30')
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $configPath -Encoding utf8

$bootKey = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')
[ordered]@{ bootKey = $bootKey; minuteKeys = @(); eventKeys = @() } |
    ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $runtimeDir 'automation-state.json') -Encoding utf8

& $pwsh -NoLogo -NoProfile -NonInteractive -File $worker -Mode Test -ConfigPath $configPath
if ($LASTEXITCODE -ne 0) { throw 'Validation failed.' }

$backupDir = Join-Path $runtimeDir ('task-backups-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$legacyTasks = @('CodexQuotaNotifier','CodexQuotaLowAlert','CodexQuotaCardActions','Codex-Lark-Shutdown-Reminder-0030','Codex-Lark-Shutdown-Card-Actions')
foreach ($name in $legacyTasks) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath (Join-Path $backupDir "$name.xml") -Encoding utf8
    }
}

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pwsh -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$worker`" -Mode Watch -ConfigPath `"$configPath`""
$triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $userId), (New-ScheduledTaskTrigger -Daily -At '00:01'))
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4
$task = Get-ScheduledTask -TaskName $TaskName
if ($task.State -ne 'Running' -or $task.Principal.LogonType -ne 'S4U' -or $task.Principal.RunLevel -ne 'Limited') {
    throw "New task verification failed: state=$($task.State), logon=$($task.Principal.LogonType), runLevel=$($task.Principal.RunLevel)"
}
foreach ($name in $legacyTasks) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $name | Out-Null
    }
}
Write-Output "Installed $TaskName. Backups: $backupDir"
