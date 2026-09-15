param(
    [ValidateSet('Once', 'Watch', 'Install', 'Uninstall', 'Status', 'Health', 'Failover')]
    [string] $Mode = 'Watch',

    [ValidateRange(1, 3600)]
    [int] $IntervalSeconds = 60,

    [string] $TaskName = 'Codex Proxy Guard'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:ProxyEnvironmentNames = @(
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy'
)
$script:HealthCheckUrls = @(
    'https://chatgpt.com/cdn-cgi/trace'
)
$script:BlockedChatGPTRegions = @('HK')
$script:ClashController = 'http://127.0.0.1:9097'
$script:ClashConfigPath = Join-Path $env:APPDATA 'io.github.clash-verge-rev.clash-verge-rev\config.yaml'
$script:RuntimeDir = Join-Path $HOME '.codex\proxy-guard'
$script:FailureThreshold = 2
$script:NodeMetadataPattern = '(?i)状态|剩余|到期|官网|流量|套餐|重置|更新|公告|traffic|expire|website|香港|Hong\s*Kong|🇭🇰|\bHK\b|DIRECT|REJECT'

function Write-GuardLog {
    param([string] $Message)

    New-Item -ItemType Directory -Path $script:RuntimeDir -Force | Out-Null
    $logPath = Join-Path $script:RuntimeDir 'codex-proxy-guard.log'
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Get-PropertyValue {
    param([object] $InputObject, [string] $Name, [object] $Default)

    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        return $Default
    }
    return $property.Value
}

function Test-LoopbackHost {
    param([string] $HostName)

    if ([string]::IsNullOrWhiteSpace($HostName)) { return $false }
    $normalized = $HostName.Trim('[', ']').ToLowerInvariant()
    if ($normalized -eq 'localhost') { return $true }

    $ipAddress = $null
    if ([System.Net.IPAddress]::TryParse($normalized, [ref] $ipAddress)) {
        return [System.Net.IPAddress]::IsLoopback($ipAddress)
    }
    return $false
}

function ConvertTo-HttpProxyUrl {
    param([AllowNull()] [string] $Candidate)

    if ([string]::IsNullOrWhiteSpace($Candidate)) { return $null }
    $value = $Candidate.Trim()
    $uriText = if ($value -match '^[a-zA-Z][a-zA-Z0-9+.-]*://') { $value } else { "http://$value" }

    try { $uri = [Uri] $uriText } catch { return $null }
    if ($uri.Scheme -notin @('http', 'https')) { return $null }
    if (-not (Test-LoopbackHost -HostName $uri.Host)) { return $null }
    if ($uri.Port -le 0) { return $null }
    return $uri.AbsoluteUri.TrimEnd('/')
}

function Resolve-CodexProxyUrl {
    param([int] $ProxyEnable, [AllowNull()] [string] $ProxyServer)

    if ($ProxyEnable -ne 1 -or [string]::IsNullOrWhiteSpace($ProxyServer)) { return $null }
    $entries = @($ProxyServer -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($entries.Count -eq 0) { return $null }

    $byScheme = @{}
    foreach ($entry in $entries) {
        if ($entry -match '^([^=]+)=(.+)$') {
            $byScheme[$matches[1].Trim().ToLowerInvariant()] = $matches[2].Trim()
        }
    }
    if ($byScheme.ContainsKey('https')) { return ConvertTo-HttpProxyUrl $byScheme['https'] }
    if ($byScheme.ContainsKey('http')) { return ConvertTo-HttpProxyUrl $byScheme['http'] }
    if ($entries.Count -eq 1 -and $entries[0] -notmatch '=') { return ConvertTo-HttpProxyUrl $entries[0] }
    return $null
}

function Get-WinInetProxy {
    $path = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
    $settings = Get-ItemProperty -LiteralPath $path
    [pscustomobject] @{
        ProxyEnable = [int] (Get-PropertyValue $settings 'ProxyEnable' 0)
        ProxyServer = [string] (Get-PropertyValue $settings 'ProxyServer' '')
        AutoConfigURL = [string] (Get-PropertyValue $settings 'AutoConfigURL' '')
    }
}

function Test-ProxyEndpoint {
    param([AllowNull()] [string] $ProxyUrl, [int] $TimeoutMilliseconds = 1500)

    if ([string]::IsNullOrWhiteSpace($ProxyUrl)) { return $false }
    try { $uri = [Uri] $ProxyUrl } catch { return $false }

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.BeginConnect($uri.Host.Trim('[', ']'), $uri.Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) { return $false }
        $client.EndConnect($connect)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-InternetThroughProxy {
    param(
        [string] $ProxyUrl,
        [string[]] $Urls = $script:HealthCheckUrls,
        [int] $TimeoutMilliseconds = 8000
    )

    if (-not (Test-ProxyEndpoint -ProxyUrl $ProxyUrl)) { return $false }
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
    if ([string]::IsNullOrWhiteSpace($curl)) { return $false }
    $timeoutSeconds = [Math]::Max(1, [Math]::Ceiling($TimeoutMilliseconds / 1000))
    foreach ($url in $Urls) {
        $trace = & $curl --proxy $ProxyUrl --connect-timeout $timeoutSeconds --max-time $timeoutSeconds --silent --user-agent 'Mozilla/5.0' $url
        if ($LASTEXITCODE -eq 0 -and (($trace -join "`n") -match '(?m)^loc=([A-Z]{2})\r?$')) {
            return $matches[1] -notin $script:BlockedChatGPTRegions
        }
    }
    return $false
}

function Get-ClashApiSecret {
    param([string] $ConfigPath = $script:ClashConfigPath)

    if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Clash config not found: $ConfigPath" }
    $secretLine = Get-Content -LiteralPath $ConfigPath | Where-Object { $_ -match '^\s*secret\s*:' } | Select-Object -First 1
    if ($null -eq $secretLine) { return '' }
    return (($secretLine -replace '^\s*secret\s*:\s*', '').Trim()).Trim('''', '"')
}

function Invoke-ClashApi {
    param(
        [ValidateSet('GET', 'PUT')]
        [string] $Method,
        [string] $Path,
        [AllowNull()] $Body = $null,
        [int] $TimeoutSeconds = 20
    )

    Add-Type -AssemblyName System.Net.Http
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
    $secret = Get-ClashApiSecret
    if (-not [string]::IsNullOrEmpty($secret)) {
        $client.DefaultRequestHeaders.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $secret)
    }

    try {
        $uri = $script:ClashController.TrimEnd('/') + $Path
        if ($Method -eq 'GET') {
            $response = $client.GetAsync($uri).GetAwaiter().GetResult()
        } else {
            $json = $Body | ConvertTo-Json -Compress
            $content = [System.Net.Http.StringContent]::new($json, [System.Text.Encoding]::UTF8, 'application/json')
            try { $response = $client.PutAsync($uri, $content).GetAwaiter().GetResult() } finally { $content.Dispose() }
        }
        try {
            $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            if (-not $response.IsSuccessStatusCode) {
                throw "Clash API $Method $Path returned HTTP $([int]$response.StatusCode): $text"
            }
            if ([string]::IsNullOrWhiteSpace($text)) { return $null }
            return $text | ConvertFrom-Json
        } finally {
            $response.Dispose()
        }
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-ClashSelector {
    param([AllowNull()] $ProxyData)

    $selectors = @($ProxyData.proxies.PSObject.Properties | ForEach-Object {
        $proxy = $_.Value
        if ($proxy.type -eq 'Selector' -and $_.Name -ne 'GLOBAL') {
            [pscustomobject] @{ Name = $_.Name; Now = $proxy.now; All = @($proxy.all); Count = @($proxy.all).Count }
        }
    })
    return $selectors | Sort-Object Count -Descending | Select-Object -First 1
}

function Get-ClashFailoverCandidates {
    param([AllowNull()] $ProxyData, [AllowNull()] $Selector)

    $encodedGroup = [Uri]::EscapeDataString($Selector.Name)
    $encodedUrl = [Uri]::EscapeDataString($script:HealthCheckUrls[-1])
    $delays = Invoke-ClashApi -Method GET -Path "/group/$encodedGroup/delay?url=$encodedUrl&timeout=5000" -TimeoutSeconds 15
    $allowed = @{}
    foreach ($name in $Selector.All) { $allowed[[string] $name] = $true }

    return @($delays.PSObject.Properties | ForEach-Object {
        $name = [string] $_.Name
        $delay = 0
        if ($allowed.ContainsKey($name) -and $name -notmatch $script:NodeMetadataPattern -and [int]::TryParse([string] $_.Value, [ref] $delay) -and $delay -gt 0) {
            $proxyType = $ProxyData.proxies.PSObject.Properties[$name].Value.type
            if ($proxyType -notin @('Selector', 'URLTest', 'Fallback', 'LoadBalance')) {
                [pscustomobject] @{ Name = $name; Delay = $delay }
            }
        }
    } | Sort-Object Delay)
}

function Invoke-ClashAutoFailover {
    param([string] $ProxyUrl)

    try {
        $proxyData = Invoke-ClashApi -Method GET -Path '/proxies' -TimeoutSeconds 8
        $selector = Get-ClashSelector -ProxyData $proxyData
        if ($null -eq $selector) { throw 'No non-GLOBAL Clash selector group was found.' }

        $candidates = Get-ClashFailoverCandidates -ProxyData $proxyData -Selector $selector
        if ($candidates.Count -eq 0) { throw "No healthy candidates were returned for group '$($selector.Name)'." }

        $encodedSelector = [Uri]::EscapeDataString($selector.Name)
        foreach ($candidate in $candidates) {
            if ($candidate.Name -eq $selector.Now) { continue }
            Invoke-ClashApi -Method PUT -Path "/proxies/$encodedSelector" -Body @{ name = $candidate.Name } -TimeoutSeconds 8 | Out-Null
            Start-Sleep -Milliseconds 800
            if (Test-InternetThroughProxy -ProxyUrl $ProxyUrl) {
                Write-GuardLog "Clash failover switched group '$($selector.Name)' to '$($candidate.Name)' ($($candidate.Delay) ms)."
                return $true
            }
            Write-GuardLog "Candidate '$($candidate.Name)' failed the ChatGPT health check; trying the next node."
        }
        Invoke-ClashApi -Method PUT -Path "/proxies/$encodedSelector" -Body @{ name = $selector.Now } -TimeoutSeconds 8 | Out-Null
        Write-GuardLog "Clash failover exhausted $($candidates.Count) tested candidates; restored '$($selector.Now)'."
        return $false
    } catch {
        Write-GuardLog "Clash failover failed: $($_.Exception.Message)"
        return $false
    }
}

function Get-UserEnvironmentValue {
    param([string] $Name)
    return [Environment]::GetEnvironmentVariable($Name, 'User')
}

function Set-UserEnvironmentValue {
    param([string] $Name, [AllowNull()] $Value)
    [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

function Sync-ProxyEnvironment {
    param(
        [AllowNull()] $ProxyUrl,
        [scriptblock] $GetValue = { param([string] $Name) Get-UserEnvironmentValue $Name },
        [scriptblock] $SetValue = { param([string] $Name, [AllowNull()] $Value) Set-UserEnvironmentValue $Name $Value }
    )

    $changed = $false
    foreach ($name in $script:ProxyEnvironmentNames) {
        if ((& $GetValue $name) -ne $ProxyUrl) {
            & $SetValue $name $ProxyUrl
            $changed = $true
        }
    }
    return $changed
}

function Sync-CodexProxyOnce {
    $proxy = Get-WinInetProxy
    $proxyUrl = Resolve-CodexProxyUrl $proxy.ProxyEnable $proxy.ProxyServer

    if ($null -ne $proxyUrl -and -not (Test-ProxyEndpoint $proxyUrl)) {
        Write-GuardLog "Proxy $proxyUrl is configured but not listening; keeping existing environment."
        return $false
    }

    $changed = Sync-ProxyEnvironment $proxyUrl
    if ($changed) {
        if ($null -eq $proxyUrl) {
            Write-GuardLog 'Cleared Codex proxy environment variables.'
        } else {
            Write-GuardLog "Synced Codex proxy environment variables to $proxyUrl."
        }
    }
    return $changed
}

function Get-CodexProxyGuardTaskActionSpec {
    param([int] $IntervalSeconds = 60)

    $scriptPath = Join-Path $PSScriptRoot 'CodexProxyGuard.ps1'
    $powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $powershellPath)) { $powershellPath = 'powershell.exe' }
    [pscustomobject] @{
        Execute = $powershellPath
        Arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Mode Watch -IntervalSeconds {1}' -f $scriptPath, $IntervalSeconds
    }
}

function Install-CodexProxyGuardTask {
    param([string] $TaskName = 'Codex Proxy Guard', [int] $IntervalSeconds = 60)

    $spec = Get-CodexProxyGuardTaskActionSpec $IntervalSeconds
    $action = New-ScheduledTaskAction -Execute $spec.Execute -Argument $spec.Arguments
    $userId = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Days 0)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Keeps Codex proxy environment variables in sync with the current Windows loopback proxy.' -Force | Out-Null
    Write-GuardLog "Installed scheduled task '$TaskName'."
}

function Uninstall-CodexProxyGuardTask {
    param([string] $TaskName = 'Codex Proxy Guard')
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-GuardLog "Uninstalled scheduled task '$TaskName'."
    }
}

function Get-CodexProxyGuardWatcherProcessCount {
    $scriptPath = Join-Path $PSScriptRoot 'CodexProxyGuard.ps1'
    $pathPattern = [regex]::Escape($scriptPath)
    $watchPattern = '(?i)-File\s+[\"]?' + $pathPattern + '[\"]?\s+-Mode\s+Watch(?:\s|$)'
    $watchers = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('powershell.exe', 'pwsh.exe') -and
        $_.CommandLine -match $watchPattern
    }
    return @($watchers).Count
}

function Show-CodexProxyGuardStatus {
    param([string] $TaskName = 'Codex Proxy Guard')
    $proxy = Get-WinInetProxy
    $proxyUrl = Resolve-CodexProxyUrl $proxy.ProxyEnable $proxy.ProxyServer
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    [pscustomobject] @{
        WinInetProxyEnable = $proxy.ProxyEnable
        WinInetProxyServer = $proxy.ProxyServer
        CodexProxyUrl = $proxyUrl
        ProxyListening = if ($null -eq $proxyUrl) { $false } else { Test-ProxyEndpoint $proxyUrl }
        ChatGPTHealthy = if ($null -eq $proxyUrl) { $false } else { Test-InternetThroughProxy $proxyUrl }
        UserHttpsProxy = Get-UserEnvironmentValue 'HTTPS_PROXY'
        TaskName = $TaskName
        TaskState = if ($null -eq $task) { 'NotInstalled' } else { $task.State }
        WatcherProcessCount = Get-CodexProxyGuardWatcherProcessCount
    }
}

function Invoke-CodexProxyGuard {
    param([string] $Mode, [int] $IntervalSeconds, [string] $TaskName)
    switch ($Mode) {
        'Once' { [void] (Sync-CodexProxyOnce) }
        'Watch' {
            Write-GuardLog "Watcher started; interval ${IntervalSeconds}s."
            $failureCount = 0
            while ($true) {
                try { [void] (Sync-CodexProxyOnce) } catch { Write-GuardLog "Sync failed: $($_.Exception.Message)" }
                try {
                    $proxy = Get-WinInetProxy
                    $proxyUrl = Resolve-CodexProxyUrl $proxy.ProxyEnable $proxy.ProxyServer
                    if ($null -ne $proxyUrl -and (Test-ProxyEndpoint $proxyUrl)) {
                        if (Test-InternetThroughProxy $proxyUrl) {
                            if ($failureCount -gt 0) { Write-GuardLog 'ChatGPT connectivity recovered without switching nodes.' }
                            $failureCount = 0
                        } else {
                            $failureCount++
                            Write-GuardLog "ChatGPT connectivity check failed ($failureCount/$script:FailureThreshold)."
                            if ($failureCount -ge $script:FailureThreshold) {
                                [void] (Invoke-ClashAutoFailover $proxyUrl)
                                $failureCount = 0
                            }
                        }
                    }
                } catch {
                    Write-GuardLog "Health monitor failed: $($_.Exception.Message)"
                }
                Start-Sleep -Seconds $IntervalSeconds
            }
        }
        'Install' {
            Install-CodexProxyGuardTask $TaskName $IntervalSeconds
            [void] (Sync-CodexProxyOnce)
            Start-ScheduledTask -TaskName $TaskName
            Write-GuardLog "Started scheduled task '$TaskName'."
        }
        'Uninstall' { Uninstall-CodexProxyGuardTask $TaskName }
        'Status' { Show-CodexProxyGuardStatus $TaskName | Format-List }
        'Health' {
            $proxy = Get-WinInetProxy
            $proxyUrl = Resolve-CodexProxyUrl $proxy.ProxyEnable $proxy.ProxyServer
            [pscustomobject] @{ ProxyUrl = $proxyUrl; ChatGPTHealthy = ($null -ne $proxyUrl -and (Test-InternetThroughProxy $proxyUrl)) } | Format-List
        }
        'Failover' {
            $proxy = Get-WinInetProxy
            $proxyUrl = Resolve-CodexProxyUrl $proxy.ProxyEnable $proxy.ProxyServer
            if ($null -eq $proxyUrl) { throw 'No supported Windows loopback proxy is configured.' }
            [void] (Invoke-ClashAutoFailover $proxyUrl)
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-CodexProxyGuard $Mode $IntervalSeconds $TaskName
}
