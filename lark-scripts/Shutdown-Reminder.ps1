param(
    [switch]$Test,
    [switch]$DryRun,
    [string]$ConfigPath = (Join-Path $HOME '.lark-channel\automation\config.json')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Config not found: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$larkCli = [string]$config.larkCli
$chatId = [string]$config.recipientChatId
$runtimeDir = [string]$config.runtimeDir
$statePath = Join-Path $runtimeDir 'shutdown-state.json'
$logPath = Join-Path $runtimeDir 'shutdown.log'
$cardCachePath = Join-Path $runtimeDir 'shutdown-card-cache.jsonl'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

$env:LARK_CHANNEL = '1'
$env:LARK_CHANNEL_HOME = [string]$config.larkChannelHome
$env:LARK_CHANNEL_PROFILE = [string]$config.profile
$env:LARK_CHANNEL_CONFIG = [string]$config.larkChannelConfig
$env:LARKSUITE_CLI_CONFIG_DIR = [string]$config.larkCliConfigDir
$env:HTTP_PROXY = [string]$config.proxy
$env:HTTPS_PROXY = [string]$config.proxy
$env:NO_PROXY = 'localhost,127.0.0.1,::1'

function Write-Log([string]$Message) {
    Add-Content -LiteralPath $logPath -Encoding utf8 -Value "$(Get-Date -Format o) $Message"
}

function Read-State([string]$DateKey) {
    if (Test-Path -LiteralPath $statePath) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
            if ($state.date -eq $DateKey) { return $state }
        }
        catch { Write-Log "WARN invalid-state $($_.Exception.Message)" }
    }
    return [pscustomobject]@{ date = $DateKey; status = 'active'; lastEventKey = $null; updatedAt = (Get-Date).ToString('o') }
}

function Save-State($State) {
    $State.updatedAt = (Get-Date).ToString('o')
    $State | ConvertTo-Json -Compress | Set-Content -LiteralPath $statePath -Encoding utf8
}

function Test-BridgeReady {
    return $null -ne (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -eq 'node.exe' -and $_.CommandLine -match 'lark-channel-bridge' -and $_.CommandLine -match '--profile\s+codex'
    } | Select-Object -First 1)
}

function Send-Card([string]$CardJson, [string]$IdempotencyKey) {
    $arguments = @('im', '+messages-send', '--as', 'bot', '--chat-id', $chatId, '--msg-type', 'interactive', '--content', $CardJson, '--idempotency-key', $IdempotencyKey, '--format', 'json')
    if ($DryRun) { $arguments += '--dry-run' }
    $output = & $larkCli @arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "lark-cli exited with code $LASTEXITCODE`: $($output -join ' ')" }
    if ($DryRun) {
        Write-Log 'OK card dry-run'
        return $null
    }
    $result = ($output -join "`n") | ConvertFrom-Json
    $messageId = [string]$result.data.message_id
    [ordered]@{ messageId = $messageId; createdAt = (Get-Date).ToString('o'); cardJson = $CardJson } |
        ConvertTo-Json -Compress |
        Add-Content -LiteralPath $cardCachePath -Encoding utf8
    Write-Log "OK card message_id=$messageId"
    return $messageId
}

$now = Get-Date
$dateKey = $now.ToString('yyyyMMdd')
$timeText = $now.ToString('HH:mm')
$state = Read-State $dateKey
if (-not $Test -and $state.status -in @('skip', 'shutdown-now')) {
    Write-Log "SKIP status=$($state.status)"
    exit 0
}

$isFinalReminder = -not $Test -and $now.Hour -eq 1 -and $now.Minute -ge 30 -and $now.Minute -lt 35
$bridgeReady = Test-BridgeReady
$autoText = if ($Test) {
    '样例卡：正式提醒中按钮可点击。'
} elseif ($isFinalReminder -and $bridgeReady) {
    '**01:45 将自动关机**，选择“今晚不关”可取消。'
} elseif ($isFinalReminder) {
    '按钮服务未运行，本次不会自动关机。'
} else {
    '请选择本次处理方式；01:30 前可随时决定。'
}

$nowCommand = if ($Test) { 'shutdown.test.now' } else { "shutdown.now.$dateKey" }
$skipCommand = if ($Test) { 'shutdown.test.skip' } else { "shutdown.skip.$dateKey" }
$card = [ordered]@{
    schema = '2.0'
    config = [ordered]@{
        width_mode = 'compact'
        enable_forward = $false
        summary = [ordered]@{ content = '关机提醒' }
    }
    header = [ordered]@{
        title = [ordered]@{ tag = 'plain_text'; content = if ($Test) { '关机提醒 · 样例' } else { '关机提醒' } }
        subtitle = [ordered]@{ tag = 'plain_text'; content = $timeText }
        template = if ($isFinalReminder) { 'red' } else { 'orange' }
        icon = [ordered]@{ tag = 'standard_icon'; token = 'power_colorful' }
    }
    body = [ordered]@{
        direction = 'vertical'
        padding = '8px 12px 10px 12px'
        vertical_spacing = '6px'
        elements = @(
            [ordered]@{ tag = 'markdown'; content = $autoText },
            [ordered]@{
                tag = 'column_set'; flex_mode = 'none'; horizontal_spacing = '8px'
                columns = @(
                    [ordered]@{
                        tag = 'column'; width = 'weighted'; weight = 1
                        elements = @([ordered]@{
                            tag = 'button'; type = 'primary_filled'; width = 'fill'; disabled = [bool]$Test
                            text = [ordered]@{ tag = 'plain_text'; content = '今晚不关' }
                            behaviors = @([ordered]@{ type = 'callback'; value = [ordered]@{ cmd = $skipCommand } })
                        })
                    },
                    [ordered]@{
                        tag = 'column'; width = 'weighted'; weight = 1
                        elements = @([ordered]@{
                            tag = 'button'; type = 'danger'; width = 'fill'; disabled = [bool]$Test
                            text = [ordered]@{ tag = 'plain_text'; content = '立即关机' }
                            confirm = [ordered]@{
                                title = [ordered]@{ tag = 'plain_text'; content = '确认立即关机？' }
                                text = [ordered]@{ tag = 'plain_text'; content = '未保存的内容可能丢失。' }
                            }
                            behaviors = @([ordered]@{ type = 'callback'; value = [ordered]@{ cmd = $nowCommand } })
                        })
                    }
                )
            }
        )
    }
}

$cardJson = $card | ConvertTo-Json -Depth 20 -Compress
$null = $cardJson | ConvertFrom-Json
$key = if ($Test) { 'shutdown-card-test-' + $now.ToString('yyyyMMddHHmmss') } else { 'shutdown-reminder-' + $now.ToString('yyyyMMdd-HHmm') }

try {
    $messageId = Send-Card -CardJson $cardJson -IdempotencyKey $key
    if ($messageId -and $isFinalReminder -and $bridgeReady) {
        $state = Read-State $dateKey
        if ($state.status -eq 'active') {
            & shutdown.exe /s /t 900 /c '01:45 自动关机；在飞书选择“今晚不关”可取消。'
            if ($LASTEXITCODE -ne 0) { throw "Unable to schedule shutdown, exit=$LASTEXITCODE" }
            $state.status = 'armed'
            Save-State $state
            Write-Log 'ARMED shutdown in 900 seconds'
        }
    }
    exit 0
}
catch {
    Write-Log "ERROR $($_.Exception.Message)"
    exit 1
}
