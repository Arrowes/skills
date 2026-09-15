param(
    [ValidateSet('Send', 'Monitor', 'Test')]
    [string]$Mode = 'Send',
    [string]$ConfigPath = (Join-Path $HOME '.lark-channel\automation\config.json')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Config not found: $ConfigPath" }
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$node = [string]$config.node
$codexJs = [string]$config.codexJs
$larkCli = [string]$config.larkCli
$pwsh = [string]$config.pwsh
$recipient = [string]$config.recipientOpenId
$runtimeDir = [string]$config.runtimeDir
$log = Join-Path $runtimeDir 'quota.log'
$stateFile = Join-Path $runtimeDir 'quota-state.json'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

$env:LARK_CHANNEL = '1'
$env:LARK_CHANNEL_HOME = [string]$config.larkChannelHome
$env:LARK_CHANNEL_PROFILE = [string]$config.profile
$env:LARK_CHANNEL_CONFIG = [string]$config.larkChannelConfig
$env:LARKSUITE_CLI_CONFIG_DIR = [string]$config.larkCliConfigDir
$env:HTTP_PROXY = [string]$config.proxy
$env:HTTPS_PROXY = [string]$config.proxy
$env:NO_PROXY = 'localhost,127.0.0.1,::1'

function Wait-CodexResponse {
    param(
        [System.Diagnostics.Process]$Process,
        [int]$Id
    )

    for ($i = 0; $i -lt 100; $i++) {
        $read = $Process.StandardOutput.ReadLineAsync()
        if (-not $read.Wait(15000)) {
            throw "Codex app-server response $Id timed out."
        }
        if ($null -eq $read.Result) {
            throw "Codex app-server exited before response $Id."
        }
        try {
            $message = $read.Result | ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            continue
        }
        if ($message.PSObject.Properties['id'] -and $message.id -eq $Id) {
            if ($message.PSObject.Properties['error'] -and $null -ne $message.error) {
                throw "Codex app-server error: $($message.error.message)"
            }
            return $message.result
        }
    }
    throw "Codex app-server response $Id was not found."
}

function Get-CodexQuota {
    foreach ($path in @($node, $codexJs, $larkCli, $pwsh)) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Required file not found: $path"
        }
    }

    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $node
    $start.Arguments = '"' + $codexJs + '" app-server --stdio'
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.CreateNoWindow = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    [void]$process.Start()

    try {
        $process.StandardInput.WriteLine('{"id":1,"method":"initialize","params":{"clientInfo":{"name":"quota-notifier","version":"1.0"},"capabilities":{"experimentalApi":true}}}')
        $process.StandardInput.Flush()
        [void](Wait-CodexResponse -Process $process -Id 1)

        $process.StandardInput.WriteLine('{"method":"initialized"}')
        $process.StandardInput.WriteLine('{"id":2,"method":"account/rateLimits/read","params":null}')
        $process.StandardInput.Flush()
        return Wait-CodexResponse -Process $process -Id 2
    }
    finally {
        try { $process.StandardInput.Close() } catch {}
        if (-not $process.WaitForExit(3000)) {
            $process.Kill()
        }
        $process.Dispose()
    }
}

function Format-ResetTime {
    param($UnixSeconds)
    if ($null -eq $UnixSeconds) { return '未知' }
    return [DateTimeOffset]::FromUnixTimeSeconds([long]$UnixSeconds).ToLocalTime().ToString('MM-dd HH:mm')
}

function Format-Countdown {
    param($UnixSeconds)
    if ($null -eq $UnixSeconds) { return '重置时间未知' }
    $remaining = [DateTimeOffset]::FromUnixTimeSeconds([long]$UnixSeconds) - [DateTimeOffset]::Now
    if ($remaining.TotalSeconds -le 0) { return '等待重置' }
    if ($remaining.TotalDays -ge 1) { return ('{0}天{1}小时后重置' -f [Math]::Floor($remaining.TotalDays), $remaining.Hours) }
    if ($remaining.TotalHours -ge 1) { return ('{0}小时{1}分后重置' -f [Math]::Floor($remaining.TotalHours), $remaining.Minutes) }
    return ('{0}分钟后重置' -f [Math]::Max(1, [Math]::Ceiling($remaining.TotalMinutes)))
}

function Save-LowState {
    param([bool]$PrimaryLow, [bool]$SecondaryLow, $EarliestCreditExpiry)
    [ordered]@{
        primaryLow = $PrimaryLow
        secondaryLow = $SecondaryLow
        earliestCreditExpiry = $EarliestCreditExpiry
        updatedAt = (Get-Date).ToString('o')
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $stateFile -Encoding UTF8
}

function Read-LowState {
    if (-not (Test-Path -LiteralPath $stateFile)) { return $null }
    try { return Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Remove-OldLogEntries {
    if (-not (Test-Path -LiteralPath $log)) { return }
    $cutoff = (Get-Date).AddDays(-30)
    $kept = @(
        foreach ($line in Get-Content -LiteralPath $log) {
            if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
                $stamp = [DateTime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
                if ($stamp -ge $cutoff) { $line }
            }
            else { $line }
        }
    )
    [IO.File]::WriteAllLines($log, $kept, (New-Object Text.UTF8Encoding($true)))
}

function New-ProgressBar {
    param([int]$Percent)
    $segments = 8
    $filled = [int][Math]::Round($Percent * $segments / 100)
    return ((('█' * $filled) -join '') + (('░' * ($segments - $filled)) -join ''))
}

try {
    Remove-OldLogEntries
    $snapshot = Get-CodexQuota
    $limits = $snapshot.rateLimits
    $primary = $limits.primary
    $secondary = $limits.secondary
    $extraCredits = if ($limits.PSObject.Properties['credits']) { $limits.credits } else { $null }
    $primaryRemaining = 100 - [int]$primary.usedPercent
    $secondaryRemaining = 100 - [int]$secondary.usedPercent
    $resetCredits = if ($null -eq $snapshot.rateLimitResetCredits) { 0 } else { [int]$snapshot.rateLimitResetCredits.availableCount }
    $creditRows = if ($null -eq $snapshot.rateLimitResetCredits -or $null -eq $snapshot.rateLimitResetCredits.credits) { @() } else { @($snapshot.rateLimitResetCredits.credits) }
    $storedState = Read-LowState
    $showExtraCredits = $false
    $extraCreditsText = $null
    if ($null -ne $extraCredits) {
        if ([bool]$extraCredits.unlimited) {
            $showExtraCredits = $true
            $extraCreditsText = '无限'
        }
        elseif ([bool]$extraCredits.hasCredits) {
            $balance = 0D
            if ([Decimal]::TryParse($extraCredits.balance.ToString(), [Globalization.NumberStyles]::Any, [Globalization.CultureInfo]::InvariantCulture, [ref]$balance) -and $balance -gt 0) {
                $showExtraCredits = $true
                $extraCreditsText = $extraCredits.balance.ToString()
            }
        }
    }
    $earliest = $creditRows | Where-Object { $_.PSObject.Properties['expiresAt'] -and $null -ne $_.expiresAt } | Sort-Object expiresAt | Select-Object -First 1
    $earliestExpiresAt = if ($null -eq $earliest) { $null } else { [long]$earliest.expiresAt }
    if ($null -eq $earliestExpiresAt -and $resetCredits -gt 0 -and $null -ne $storedState -and $storedState.PSObject.Properties['earliestCreditExpiry'] -and $null -ne $storedState.earliestCreditExpiry) {
        $cachedExpiry = [long]$storedState.earliestCreditExpiry
        if ($cachedExpiry -gt [DateTimeOffset]::Now.ToUnixTimeSeconds()) { $earliestExpiresAt = $cachedExpiry }
    }
    $earliestText = if ($null -ne $earliestExpiresAt) { Format-ResetTime $earliestExpiresAt } elseif ($resetCredits -gt 0) { '暂未返回' } else { '无' }
    $creditTheme = $null
    if ($null -ne $earliestExpiresAt) {
        $creditHours = ([DateTimeOffset]::FromUnixTimeSeconds($earliestExpiresAt) - [DateTimeOffset]::Now).TotalHours
        if ($creditHours -le 24) { $creditTheme = 'red' }
        elseif ($creditHours -le 48) { $creditTheme = 'orange' }
    }
    $creditCountText = "$resetCredits 次"
    $creditExpiryText = $earliestText
    if ($null -ne $creditTheme) {
        $creditCountText = "<font color='$creditTheme'>$creditCountText</font>"
        $creditExpiryText = "<font color='$creditTheme'>$creditExpiryText</font>"
    }
    if ($primaryRemaining -lt 0 -or $primaryRemaining -gt 100 -or $secondaryRemaining -lt 0 -or $secondaryRemaining -gt 100) {
        throw 'Codex returned an invalid usage percentage.'
    }

    $lowestRemaining = [Math]::Min($primaryRemaining, $secondaryRemaining)
    if ($lowestRemaining -lt 20) {
        $theme = 'red'
        $status = '额度紧张'
    }
    elseif ($lowestRemaining -lt 50) {
        $theme = 'orange'
        $status = '注意用量'
    }
    else {
        $theme = 'green'
        $status = '额度充足'
    }

    $primaryTheme = if ($primaryRemaining -lt 20) { 'red' } elseif ($primaryRemaining -lt 50) { 'orange' } else { 'green' }
    $secondaryTheme = if ($secondaryRemaining -lt 20) { 'red' } elseif ($secondaryRemaining -lt 50) { 'orange' } else { 'green' }

    $primaryLow = $primaryRemaining -lt 20
    $secondaryLow = $secondaryRemaining -lt 20
    $cardTitle = 'Codex 额度状态'
    if ($Mode -eq 'Monitor') {
        $previous = $storedState
        $previousPrimaryLow = $false
        $previousSecondaryLow = $false
        if ($null -ne $previous) {
            if ($previous.PSObject.Properties['primaryLow']) { $previousPrimaryLow = [bool]$previous.primaryLow }
            if ($previous.PSObject.Properties['secondaryLow']) { $previousSecondaryLow = [bool]$previous.secondaryLow }
        }
        $newLow = ($primaryLow -and -not $previousPrimaryLow) -or ($secondaryLow -and -not $previousSecondaryLow)
        if (-not $newLow) {
            Save-LowState -PrimaryLow $primaryLow -SecondaryLow $secondaryLow -EarliestCreditExpiry $earliestExpiresAt
            exit 0
        }
        $cardTitle = 'Codex 低额度提醒'
    }

    $primaryReset = Format-ResetTime $primary.resetsAt
    $secondaryReset = Format-ResetTime $secondary.resetsAt
    $primaryCountdown = Format-Countdown $primary.resetsAt
    $secondaryCountdown = Format-Countdown $secondary.resetsAt
    $primaryBar = New-ProgressBar $primaryRemaining
    $secondaryBar = New-ProgressBar $secondaryRemaining
    $nowText = Get-Date -Format 'yyyy-MM-dd HH:mm'

    $card = [ordered]@{
        schema = '2.0'
        config = [ordered]@{
            update_multi = $true
            width_mode = 'default'
            enable_forward = $false
            summary = [ordered]@{ content = "Codex 额度：5 小时剩余 $primaryRemaining%，7 天剩余 $secondaryRemaining%" }
        }
        header = [ordered]@{
            title = [ordered]@{ tag = 'plain_text'; content = $cardTitle }
            subtitle = [ordered]@{ tag = 'plain_text'; content = $nowText }
            template = $theme
            icon = [ordered]@{ tag = 'standard_icon'; token = 'ai-common_colorful' }
            text_tag_list = @(
                [ordered]@{ tag = 'text_tag'; text = [ordered]@{ tag = 'plain_text'; content = $status }; color = $theme }
            )
        }
        body = [ordered]@{
            direction = 'vertical'
            padding = '8px 12px 12px 12px'
            vertical_spacing = '6px'
            elements = @(
                [ordered]@{
                    tag = 'column_set'
                    flex_mode = 'none'
                    columns = @(
                        [ordered]@{
                            tag = 'column'; width = 'weighted'; weight = 1
                            background_style = "$primaryTheme-50"; padding = '8px 10px'; vertical_spacing = '0px'
                            elements = @(
                                [ordered]@{ tag = 'markdown'; content = "**5 小时额度**　<font color='$primaryTheme'>**$primaryRemaining%**</font>　<font color='grey'>$primaryReset</font>`n<font color='$primaryTheme'>$primaryBar</font>　<font color='grey'>$primaryCountdown</font>" }
                            )
                        }
                    )
                },
                [ordered]@{
                    tag = 'column_set'
                    flex_mode = 'none'
                    columns = @(
                        [ordered]@{
                            tag = 'column'; width = 'weighted'; weight = 1
                            background_style = "$secondaryTheme-50"; padding = '8px 10px'; vertical_spacing = '0px'
                            elements = @(
                                [ordered]@{ tag = 'markdown'; content = "**7 天额度**　　<font color='$secondaryTheme'>**$secondaryRemaining%**</font>　<font color='grey'>$secondaryReset</font>`n<font color='$secondaryTheme'>$secondaryBar</font>　<font color='grey'>$secondaryCountdown</font>" }
                            )
                        }
                    )
                },
                [ordered]@{
                    tag = 'div'
                    fields = @(
                        [ordered]@{ is_short = $true; text = [ordered]@{ tag = 'lark_md'; content = "**可用重置次数**`n$creditCountText" } },
                        [ordered]@{ is_short = $true; text = [ordered]@{ tag = 'lark_md'; content = "**最近到期**`n$creditExpiryText" } }
                    )
                }
            )
        }
    }
    if ($showExtraCredits) {
        $card.body.elements += [ordered]@{
            tag = 'div'
            text = [ordered]@{
                tag = 'lark_md'
                content = "**额外额度**　<font color='blue'>$extraCreditsText</font>"
            }
        }
    }
    $card.body.elements += [ordered]@{
        tag = 'button'
        type = 'primary_filled'
        size = 'small'
        width = 'default'
        disabled = ($Mode -eq 'Test')
        text = [ordered]@{ tag = 'plain_text'; content = '查询额度' }
        behaviors = @(
            [ordered]@{
                type = 'callback'
                value = [ordered]@{ cmd = if ($Mode -eq 'Test') { 'quota.test.refresh' } else { 'quota.refresh' } }
            }
        )
    }
    $cardJson = $card | ConvertTo-Json -Depth 20 -Compress

    if ($Mode -eq 'Test') {
        $parsed = $cardJson | ConvertFrom-Json
        $expectedBlocks = if ($showExtraCredits) { 5 } else { 4 }
        if ($parsed.schema -ne '2.0' -or $parsed.body.elements.Count -ne $expectedBlocks) {
            throw 'Generated card failed structural validation.'
        }
        if ($parsed.body.elements[0].columns[0].background_style -ne "$primaryTheme-50" -or
            $parsed.body.elements[1].columns[0].background_style -ne "$secondaryTheme-50") {
            throw 'Generated card failed independent color validation.'
        }
        $refreshButton = $parsed.body.elements[-1]
        if ($refreshButton.tag -ne 'button' -or $refreshButton.text.content -ne '查询额度' -or
            $refreshButton.behaviors[0].value.cmd -ne 'quota.test.refresh' -or -not $refreshButton.disabled) {
            throw 'Generated refresh button failed structural validation.'
        }
        $cardJson
        exit 0
    }

    $key = 'codex-quota-' + $Mode.ToLowerInvariant() + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
    $env:CODEX_QUOTA_CARD = $cardJson
    $childCommand = "& '$larkCli' im +messages-send --as bot --user-id '$recipient' --msg-type interactive --content `$env:CODEX_QUOTA_CARD --idempotency-key '$key' --format json"
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childCommand))
    $sendOutput = & $pwsh -NoLogo -NoProfile -NonInteractive -EncodedCommand $encodedCommand 2>&1
    Remove-Item Env:CODEX_QUOTA_CARD -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -ne 0) {
        throw "lark-cli exited with code $LASTEXITCODE`: $($sendOutput -join ' ')"
    }
    $sendResult = ($sendOutput -join "`n") | ConvertFrom-Json
    Save-LowState -PrimaryLow $primaryLow -SecondaryLow $secondaryLow -EarliestCreditExpiry $earliestExpiresAt
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') OK message_id=$($sendResult.data.message_id)"
}
catch {
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ERROR $($_.Exception.Message)"
    exit 1
}
