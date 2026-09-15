param(
    [ValidateSet('Watch', 'Test')]
    [string]$Mode = 'Watch',
    [string]$ConfigPath = (Join-Path $HOME '.lark-channel\automation\config.json')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Config not found: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$runtimeDir = [string]$config.runtimeDir
$statePath = Join-Path $runtimeDir 'automation-state.json'
$logPath = Join-Path $runtimeDir 'automation.log'
$quotaScript = Join-Path $PSScriptRoot 'Quota.ps1'
$shutdownReminderScript = Join-Path $PSScriptRoot 'Shutdown-Reminder.ps1'
$shutdownActionsScript = Join-Path $PSScriptRoot 'Shutdown-Actions.ps1'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

function Write-AutomationLog([string]$Message) {
    Add-Content -LiteralPath $logPath -Encoding utf8 -Value "$(Get-Date -Format o) $Message"
}

function Read-AutomationState {
    if (Test-Path -LiteralPath $statePath) {
        try { return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json }
        catch { Write-AutomationLog "WARN invalid state: $($_.Exception.Message)" }
    }
    return [pscustomobject]@{ bootKey = ''; minuteKeys = @(); eventKeys = @() }
}

function Save-AutomationState($State) {
    $State.minuteKeys = @($State.minuteKeys | Select-Object -Last 256)
    $State.eventKeys = @($State.eventKeys | Select-Object -Last 512)
    $State | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding utf8
}

function Invoke-Child([string]$ScriptPath, [string[]]$Arguments) {
    $allArguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', $ScriptPath) + $Arguments + @('-ConfigPath', $ConfigPath)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = [string]$config.pwsh
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    foreach ($argument in $allArguments) { [void]$start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::Start($start)
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "$(Split-Path $ScriptPath -Leaf) exited with $($process.ExitCode)" }
    $process.Dispose()
}

function Test-BridgeReady {
    return $null -ne (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -eq 'node.exe' -and $_.CommandLine -match 'lark-channel-bridge' -and
        $_.CommandLine -match ('--profile\s+' + [regex]::Escape([string]$config.profile))
    } | Select-Object -First 1)
}

function Get-BootKey {
    return (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')
}

function Test-MonitorMinute([datetime]$Now) {
    $minutes = $Now.Hour * 60 + $Now.Minute
    $start = 10 * 60 + 5
    $end = 23 * 60 + 50
    return $minutes -ge $start -and $minutes -le $end -and (($minutes - $start) % 15 -eq 0)
}

function Invoke-DueWork($State, [datetime]$Now) {
    $minute = $Now.ToString('yyyyMMdd-HHmm')
    if ($State.minuteKeys -contains $minute) { return }

    $hhmm = $Now.ToString('HH:mm')
    $didWork = $false
    if (@($config.quotaTimes) -contains $hhmm) {
        Invoke-Child $quotaScript @('-Mode', 'Send')
        Write-AutomationLog "OK scheduled quota $hhmm"
        $didWork = $true
    }
    if (Test-MonitorMinute $Now) {
        Invoke-Child $quotaScript @('-Mode', 'Monitor')
        Write-AutomationLog "OK quota monitor $hhmm"
        $didWork = $true
    }
    if (@($config.shutdownTimes) -contains $hhmm) {
        Invoke-Child $shutdownReminderScript @()
        Write-AutomationLog "OK shutdown reminder $hhmm"
        $didWork = $true
    }
    if ($didWork) {
        $State.minuteKeys = @($State.minuteKeys) + $minute
        Save-AutomationState $State
    }
}

function Get-NewCardActions($State, [DateTimeOffset]$NotBefore) {
    $pattern = '^shutdown\.(?<action>skip|now)\.(?<commandDate>\d{8})$'
    $events = foreach ($file in Get-ChildItem -LiteralPath ([string]$config.bridgeLogDir) -Filter 'bridge-*.jsonl' -File -ErrorAction SilentlyContinue) {
        if ($file.LastWriteTime -lt (Get-Date).AddDays(-1)) { continue }
        foreach ($line in Get-Content -LiteralPath $file.FullName -ErrorAction SilentlyContinue) {
            try {
                $entry = $line | ConvertFrom-Json -ErrorAction Stop
                if ($entry.phase -ne 'cardAction' -or $entry.event -ne 'cmd' -or $entry.scope -ne [string]$config.recipientChatId) { continue }
                $timestamp = [DateTimeOffset]::Parse([string]$entry.ts)
                if ($timestamp -lt $NotBefore) { continue }
                $key = "$($entry.ts)|$($entry.msgId)|$($entry.cmd)"
                if (@($State.eventKeys) -contains $key) { continue }
                if ($entry.cmd -eq 'quota.refresh') {
                    [pscustomobject]@{ timestamp = $timestamp; key = $key; type = 'quota'; action = ''; commandDate = ''; messageId = [string]$entry.msgId }
                }
                elseif ([string]$entry.cmd -match $pattern) {
                    $today = Get-Date -Format 'yyyyMMdd'
                    if ($Matches.action -eq 'now' -or $Matches.commandDate -eq $today) {
                        [pscustomobject]@{ timestamp = $timestamp; key = $key; type = 'shutdown'; action = $Matches.action; commandDate = $Matches.commandDate; messageId = [string]$entry.msgId }
                    }
                }
            }
            catch { }
        }
    }
    return @($events | Sort-Object timestamp)
}

function Invoke-NewCardActions($State, [DateTimeOffset]$NotBefore) {
    foreach ($event in Get-NewCardActions $State $NotBefore) {
        try {
            if ($event.type -eq 'quota') {
                Invoke-Child $quotaScript @('-Mode', 'Send')
                Write-AutomationLog 'OK action quota.refresh'
            }
            else {
                Invoke-Child $shutdownActionsScript @('-Mode', 'Handle', '-Action', $event.action, '-EventKey', $event.key, '-MessageId', $event.messageId, '-CommandDate', $event.commandDate)
                Write-AutomationLog "OK action shutdown.$($event.action)"
            }
            $State.eventKeys = @($State.eventKeys) + $event.key
            Save-AutomationState $State
        }
        catch { Write-AutomationLog "ERROR action $($event.type): $($_.Exception.Message)" }
    }
}

$required = @($quotaScript, $shutdownReminderScript, $shutdownActionsScript, [string]$config.pwsh, [string]$config.bridgeLogDir)
foreach ($path in $required) { if (-not (Test-Path -LiteralPath $path)) { throw "Required path not found: $path" } }

if ($Mode -eq 'Test') {
    $null = Read-AutomationState
    $null = Get-BootKey
    Invoke-Child $quotaScript @('-Mode', 'Test')
    Invoke-Child $shutdownActionsScript @('-Mode', 'Test', '-Action', 'now')
    Write-Output 'OK: configuration, quota card, scheduler, and shutdown dry-run validated.'
    exit 0
}

$state = Read-AutomationState
$startedAt = [DateTimeOffset]::Now
Write-AutomationLog 'START watcher'
while ($true) {
    try {
        $bootKey = Get-BootKey
        if ($state.bootKey -ne $bootKey -and (Test-BridgeReady)) {
            Invoke-Child $quotaScript @('-Mode', 'Send')
            $state.bootKey = $bootKey
            Save-AutomationState $state
            Write-AutomationLog 'OK boot quota'
        }
        Invoke-DueWork $state (Get-Date)
        Invoke-NewCardActions $state $startedAt.AddMinutes(-5)
    }
    catch { Write-AutomationLog "ERROR loop: $($_.Exception.Message)" }
    Start-Sleep -Seconds 2
}
