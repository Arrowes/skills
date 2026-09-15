param(
    [ValidateSet('Handle', 'Test')]
    [string]$Mode = 'Handle',
    [ValidateSet('', 'skip', 'now')]
    [string]$Action = '',
    [string]$EventKey = '',
    [string]$MessageId = '',
    [string]$CommandDate = '',
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
$pendingRestorePath = Join-Path $runtimeDir 'shutdown-card-pending-restore.json'
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

function Send-Text([string]$Text, [string]$Key) {
    $output = & $larkCli im +messages-send --as bot --chat-id $chatId --text $Text --idempotency-key $Key --format json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "confirmation send failed: $($output -join ' ')" }
}

function Set-ShutdownCard([string]$MessageId, [string]$CardJson, [string]$Status) {
    if ([string]::IsNullOrWhiteSpace($MessageId)) {
        Write-Log 'WARN card-update skipped: missing message id'
        return $false
    }

    $request = @{ content = $CardJson } | ConvertTo-Json -Compress
    $output = & $larkCli im messages patch --as bot --message-id $MessageId --data $request --format json 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "WARN card-update failed messageId=$MessageId status=$Status output=$($output -join ' ')"
        return $false
    }

    try {
        $result = ($output -join "`n") | ConvertFrom-Json
        if (-not $result.ok) {
            Write-Log "WARN card-update unsuccessful messageId=$MessageId status=$Status"
            return $false
        }
    }
    catch {
        Write-Log "WARN card-update invalid-json messageId=$MessageId status=$Status"
        return $false
    }

    Write-Log "OK card-updated messageId=$MessageId status=$Status"
    return $true
}

function New-ShutdownCardJson([string]$CommandDate, [datetime]$CreatedAt) {
    $isFinalReminder = $CreatedAt.Hour -eq 1 -and $CreatedAt.Minute -ge 30 -and $CreatedAt.Minute -lt 35
    $autoText = if ($isFinalReminder) {
        '**01:45 将自动关机**，选择“今晚不关”可取消。'
    }
    else {
        '请选择本次处理方式；01:30 前可随时决定。'
    }
    $card = [ordered]@{
        schema = '2.0'
        config = [ordered]@{
            width_mode = 'compact'
            enable_forward = $false
            summary = [ordered]@{ content = '关机提醒' }
        }
        header = [ordered]@{
            title = [ordered]@{ tag = 'plain_text'; content = '关机提醒' }
            subtitle = [ordered]@{ tag = 'plain_text'; content = $CreatedAt.ToString('HH:mm') }
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
                                tag = 'button'; type = 'primary_filled'; width = 'fill'; disabled = $false
                                text = [ordered]@{ tag = 'plain_text'; content = '今晚不关' }
                                behaviors = @([ordered]@{ type = 'callback'; value = [ordered]@{ cmd = "shutdown.skip.$CommandDate" } })
                            })
                        },
                        [ordered]@{
                            tag = 'column'; width = 'weighted'; weight = 1
                            elements = @([ordered]@{
                                tag = 'button'; type = 'danger'; width = 'fill'; disabled = $false
                                text = [ordered]@{ tag = 'plain_text'; content = '立即关机' }
                                confirm = [ordered]@{
                                    title = [ordered]@{ tag = 'plain_text'; content = '确认立即关机？' }
                                    text = [ordered]@{ tag = 'plain_text'; content = '未保存的内容可能丢失。' }
                                }
                                behaviors = @([ordered]@{ type = 'callback'; value = [ordered]@{ cmd = "shutdown.now.$CommandDate" } })
                            })
                        }
                    )
                }
            )
        }
    }
    return ($card | ConvertTo-Json -Depth 20 -Compress)
}

function Get-OriginalCardJson([string]$MessageId, [string]$CommandDate) {
    if (Test-Path -LiteralPath $cardCachePath) {
        $record = Get-Content -LiteralPath $cardCachePath -ErrorAction SilentlyContinue |
            ForEach-Object { try { $_ | ConvertFrom-Json } catch { } } |
            Where-Object { $_.messageId -eq $MessageId } |
            Select-Object -Last 1
        if ($null -ne $record -and -not [string]::IsNullOrWhiteSpace($record.cardJson)) {
            return [string]$record.cardJson
        }
    }

    $createdAt = Get-Date
    $escapedId = [regex]::Escape($MessageId)
    $match = Select-String -LiteralPath $logPath -Pattern "^(?<ts>\S+) OK card message_id=$escapedId$" -ErrorAction SilentlyContinue |
        Select-Object -Last 1
    if ($null -ne $match -and $match.Line -match '^(?<ts>\S+)') {
        try { $createdAt = [DateTimeOffset]::Parse($Matches.ts).LocalDateTime } catch { }
    }
    return New-ShutdownCardJson -CommandDate $CommandDate -CreatedAt $createdAt
}

function Restore-PendingCard {
    if (-not (Test-Path -LiteralPath $pendingRestorePath)) { return }
    try {
        $pending = Get-Content -LiteralPath $pendingRestorePath -Raw | ConvertFrom-Json
        if (Set-ShutdownCard -MessageId $pending.messageId -CardJson $pending.cardJson -Status 'restored-on-start') {
            Remove-Item -LiteralPath $pendingRestorePath -Force
        }
    }
    catch {
        Write-Log "WARN pending-card-restore failed $($_.Exception.Message)"
    }
}

function Invoke-Action([string]$Action, [string]$EventKey, [string]$MessageId, [string]$CommandDate, [bool]$DryRun) {
    $dateKey = Get-Date -Format 'yyyyMMdd'
    $state = Read-State $dateKey
    if ($state.lastEventKey -eq $EventKey) { return }

    if ($DryRun) {
        Write-Output "TEST action=$Action event=$EventKey messageId=$MessageId commandDate=$CommandDate (no card update or shutdown command executed)"
        return
    }

    if ($Action -eq 'skip') {
        & shutdown.exe /a 2>$null
        $abortExit = $LASTEXITCODE
        $state.status = 'skip'
        $state.lastEventKey = $EventKey
        Save-State $state
        Send-Text '✓ 已取消今晚关机。' "shutdown-skip-confirm-$dateKey"
        Write-Log "ACTION skip abortExit=$abortExit"
        return
    }

    & shutdown.exe /a 2>$null
    $abortExit = $LASTEXITCODE
    $state.status = 'shutdown-now'
    $state.lastEventKey = $EventKey
    Save-State $state
    $originalCardJson = Get-OriginalCardJson -MessageId $MessageId -CommandDate $CommandDate
    $statusCard = $originalCardJson | ConvertFrom-Json
    $nowButton = $statusCard.body.elements[1].columns[1].elements[0]
    $nowButton.text.content = '正在关机…'
    $nowButton.disabled = $true
    $statusCardJson = $statusCard | ConvertTo-Json -Depth 20 -Compress
    $statusShown = Set-ShutdownCard -MessageId $MessageId -CardJson $statusCardJson -Status 'shutdown-pending'
    if ($statusShown) {
        [ordered]@{ messageId = $MessageId; cardJson = $originalCardJson } |
            ConvertTo-Json -Compress |
            Set-Content -LiteralPath $pendingRestorePath -Encoding utf8
        Start-Sleep -Seconds 5
        if (Set-ShutdownCard -MessageId $MessageId -CardJson $originalCardJson -Status 'restored') {
            Remove-Item -LiteralPath $pendingRestorePath -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Log "ACTION shutdown-now abortExit=$abortExit"
    & shutdown.exe /s /f /t 0 /c '已通过飞书选择立即关机。'
    if ($LASTEXITCODE -ne 0) { throw "Immediate shutdown failed, exit=$LASTEXITCODE" }
}

try {
    if ($Mode -eq 'Test') {
        if ([string]::IsNullOrEmpty($Action)) { throw 'Action is required in Test mode.' }
        Invoke-Action -Action $Action -EventKey "test-$Action" -MessageId 'test-message-id' -CommandDate (Get-Date -Format 'yyyyMMdd') -DryRun $true
        exit 0
    }

    Restore-PendingCard
    if ([string]::IsNullOrEmpty($Action) -or [string]::IsNullOrEmpty($EventKey) -or
        [string]::IsNullOrEmpty($MessageId) -or [string]::IsNullOrEmpty($CommandDate)) {
        throw 'Action, EventKey, MessageId and CommandDate are required in Handle mode.'
    }
    Invoke-Action -Action $Action -EventKey $EventKey -MessageId $MessageId -CommandDate $CommandDate -DryRun $false
    exit 0
}
catch {
    Write-Log "ACTION-ERROR $($_.Exception.Message)"
    exit 1
}
