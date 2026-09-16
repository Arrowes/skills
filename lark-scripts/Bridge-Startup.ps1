param(
    [ValidateSet('Run', 'Test')]
    [string]$Mode = 'Run',
    [string]$Profile = 'codex',
    [string]$Agent = 'codex',
    [string]$ProxyHost = '127.0.0.1',
    [int]$ProxyPort = 7890
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$env:HTTP_PROXY = "http://${ProxyHost}:$ProxyPort"
$env:HTTPS_PROXY = "http://${ProxyHost}:$ProxyPort"
$env:NO_PROXY = 'localhost,127.0.0.1,::1'

function Resolve-CommandPath([string[]]$Names) {
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
    }
    throw "Command not found: $($Names -join ', ')"
}

$node = Resolve-CommandPath @('node.exe', 'node')
$npm = Resolve-CommandPath @('npm.cmd', 'npm')
$npmRoot = (& $npm root -g).Trim()
$bridgeModule = Join-Path $npmRoot 'lark-channel-bridge\bin\lark-channel-bridge.mjs'
$bridgeHome = Join-Path $HOME '.lark-channel'
$workingDirectory = Join-Path $HOME ".lark-channel-workspaces\$Profile\default"
$launcherLogDirectory = Join-Path $bridgeHome "profiles\$Profile\logs\launcher"
$startupLog = Join-Path $launcherLogDirectory 'startup.log'
$commandPattern = 'lark-channel-bridge[\\/]bin[\\/]lark-channel-bridge\.mjs run --profile ' + [regex]::Escape($Profile) + ' --agent ' + [regex]::Escape($Agent)

New-Item -ItemType Directory -Path $launcherLogDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $workingDirectory -Force | Out-Null

function Write-StartupLog([string]$Message) {
    Add-Content -LiteralPath $startupLog -Encoding utf8 -Value "$(Get-Date -Format o) $Message"
}

function Get-LiveBridge {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -match $commandPattern } |
        Select-Object -First 1
}

function Test-TcpPort([string]$ComputerName, [int]$Port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync($ComputerName, $Port)
        return $connection.Wait(1000) -and $client.Connected
    }
    catch { return $false }
    finally { $client.Dispose() }
}

function Clear-StaleProcessRegistry {
    if (Get-LiveBridge) { return }
    $registryFile = Join-Path $bridgeHome 'registry\processes.json'
    if (-not (Test-Path -LiteralPath $registryFile)) { return }
    try {
        $state = Get-Content -LiteralPath $registryFile -Raw | ConvertFrom-Json
        $liveEntries = @($state.entries | Where-Object {
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($_.pid)" -ErrorAction SilentlyContinue
            $process -and $process.CommandLine -match 'lark-channel-bridge'
        })
        if ($liveEntries.Count -ne @($state.entries).Count) {
            $state.entries = $liveEntries
            $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $registryFile -Encoding utf8
            Write-StartupLog 'Removed stale bridge process registry entries.'
        }
    }
    catch { Write-StartupLog "Could not clean process registry: $($_.Exception.Message)" }
}

$required = @($node, $bridgeModule, $bridgeHome)
foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required path not found: $path" }
}

if ($Mode -eq 'Test') {
    [pscustomobject]@{
        Node = $node
        BridgeModule = $bridgeModule
        BridgeHome = $bridgeHome
        WorkingDirectory = $workingDirectory
        ProxyReady = (Test-TcpPort $ProxyHost $ProxyPort)
        BridgeRunning = ($null -ne (Get-LiveBridge))
    } | Format-List
    exit 0
}

if (Get-LiveBridge) {
    Write-StartupLog 'Bridge is already running.'
    exit 0
}

Write-StartupLog "Waiting for proxy ${ProxyHost}:$ProxyPort."
$proxyReady = $false
for ($check = 1; $check -le 150; $check++) {
    if (Test-TcpPort $ProxyHost $ProxyPort) { $proxyReady = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $proxyReady) {
    Write-StartupLog 'Proxy was not ready after 5 minutes.'
    exit 1
}

for ($attempt = 1; $attempt -le 3; $attempt++) {
    Clear-StaleProcessRegistry
    Write-StartupLog "Starting bridge (attempt $attempt/3)."
    try {
        $process = Start-Process -FilePath $node `
            -ArgumentList @($bridgeModule, 'run', '--profile', $Profile, '--agent', $Agent) `
            -WorkingDirectory $workingDirectory `
            -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $launcherLogDirectory 'stdout.log') `
            -RedirectStandardError (Join-Path $launcherLogDirectory 'stderr.log') `
            -PassThru
        Start-Sleep -Seconds 10
        $process.Refresh()
        if (-not $process.HasExited) {
            Write-StartupLog "Bridge started successfully with PID $($process.Id)."
            exit 0
        }
        Write-StartupLog "Bridge exited with code $($process.ExitCode)."
    }
    catch { Write-StartupLog "Bridge start failed: $($_.Exception.Message)" }
    Start-Sleep -Seconds (10 * $attempt)
}

Write-StartupLog 'Bridge failed after 3 attempts.'
exit 1
