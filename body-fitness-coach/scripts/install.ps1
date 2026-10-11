[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$DataDir,
    [string]$LegacyRoot,
    [string]$Profile = 'codex'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Resolve-FullPath([string]$Path) {
    return [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path))
}

function Assert-SafeDataDir([string]$Path) {
    $root = [IO.Path]::GetPathRoot($Path).TrimEnd('\')
    $candidate = $Path.TrimEnd('\')
    if ($candidate -eq $root -or $candidate -eq $env:USERPROFILE.TrimEnd('\')) {
        throw "Refusing broad data directory: $Path"
    }
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable failed with exit code $LASTEXITCODE"
    }
}

$scriptPath = Resolve-FullPath $MyInvocation.MyCommand.Path
$skillSource = Resolve-FullPath (Join-Path (Split-Path $scriptPath -Parent) '..')
$coach = Resolve-FullPath (Join-Path $skillSource 'scripts\coach.py')
$defaultData = Resolve-FullPath (Join-Path $skillSource '.private')
if (-not $DataDir) {
    $DataDir = $defaultData
}
$data = Resolve-FullPath $DataDir
Assert-SafeDataDir $data
$useDefaultData = $data.TrimEnd('\') -eq $defaultData.TrimEnd('\')

$codexHome = if ($env:CODEX_HOME) { Resolve-FullPath $env:CODEX_HOME } else { Resolve-FullPath (Join-Path $env:USERPROFILE '.codex') }
$skillsRoot = Join-Path $codexHome 'skills'
$skillTarget = Join-Path $skillsRoot 'body-fitness-coach'
$venv = Join-Path $data '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
$dataArgument = if ($useDefaultData) { '' } else { " --data-dir `"$data`"" }
$dailyArguments = "`"$coach`"$dataArgument daily"
$weeklyArguments = "`"$coach`"$dataArgument weekly"
$mealArguments = "`"$coach`"$dataArgument meal-reminder"
$coachArguments = @($coach)
if (-not $useDefaultData) {
    $coachArguments += @('--data-dir', $data)
}
$legacy = $null
$archive = $null
if ($LegacyRoot) {
    $legacy = Resolve-FullPath $LegacyRoot
    $archive = Join-Path (Split-Path $legacy -Parent) ("{0}-legacy-{1}.zip" -f (Split-Path $legacy -Leaf), (Get-Date -Format 'yyyyMMdd-HHmmss'))
}

if (Test-Path -LiteralPath $skillTarget) {
    $item = Get-Item -LiteralPath $skillTarget -Force
    if (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing to overwrite unrelated skill target: $skillTarget"
    }
    $targetValue = [string]($item.Target | Select-Object -First 1)
    if (-not [IO.Path]::IsPathRooted($targetValue)) {
        $targetValue = Join-Path (Split-Path $skillTarget -Parent) $targetValue
    }
    if ((Resolve-FullPath $targetValue).TrimEnd('\') -ne $skillSource.TrimEnd('\')) {
        throw "Existing skill junction points elsewhere: $skillTarget"
    }
}

if ($WhatIfPreference) {
    [void]$PSCmdlet.ShouldProcess($data, 'Create private venv, install pinned dependency, and migrate legacy state')
    if ($archive) {
        [void]$PSCmdlet.ShouldProcess($archive, "Archive $legacy excluding .venv, verify it, then migrate")
    }
    [void]$PSCmdlet.ShouldProcess($skillTarget, "Create junction to $skillSource")
    [void]$PSCmdlet.ShouldProcess('BodyCoach-DailyReport', "Register pythonw.exe $dailyArguments (AtLogOn + PT3M)")
    [void]$PSCmdlet.ShouldProcess('BodyCoach-WeeklyReport', "Register pythonw.exe $weeklyArguments (Sunday 23:00, StartWhenAvailable)")
    [void]$PSCmdlet.ShouldProcess('BodyCoach-MealReminder', "Register pythonw.exe $mealArguments (Daily 23:30)")
    return
}

if ($PSCmdlet.ShouldProcess($data, 'Create private runtime directory')) {
    New-Item -ItemType Directory -Force -Path $data | Out-Null
}

if (-not (Test-Path -LiteralPath $python)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        Invoke-Checked $launcher.Source @('-3', '-m', 'venv', $venv)
    } else {
        $basePython = (Get-Command python.exe -ErrorAction Stop).Source
        Invoke-Checked $basePython @('-m', 'venv', $venv)
    }
}

$dependency = 'mi-fitness-mcp[api] @ git+https://github.com/HUAYUE1024/mi-fitness-mcp-cn.git@v0.2.2'
Invoke-Checked $python @('-m', 'pip', 'install', $dependency)
$miCli = Join-Path $venv 'Scripts\mi-fitness-mcp.exe'
& $miCli --help *> $null
if ($LASTEXITCODE -ne 0) {
    Invoke-Checked $python @('-m', 'pip', 'install', '--force-reinstall', '--no-deps', $dependency)
    & $miCli --help *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "mi-fitness-mcp launcher is not executable after repair"
    }
}

if ($LegacyRoot) {
    if (-not (Test-Path -LiteralPath $legacy -PathType Container)) {
        throw "Legacy root does not exist: $legacy"
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::Open($archive, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $venvPrefix = (Join-Path $legacy '.venv').TrimEnd('\') + '\'
        Get-ChildItem -LiteralPath $legacy -Recurse -Force -File | Where-Object {
            -not $_.FullName.StartsWith($venvPrefix, [StringComparison]::OrdinalIgnoreCase)
        } | ForEach-Object {
            $entryName = $_.FullName.Substring($legacy.TrimEnd('\').Length).TrimStart('\').Replace('\', '/')
            [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $entryName, [IO.Compression.CompressionLevel]::Optimal)
        }
    } finally {
        $zip.Dispose()
    }
    if (-not (Test-Path -LiteralPath $archive) -or (Get-Item -LiteralPath $archive).Length -le 0) {
        throw "Legacy archive verification failed: $archive"
    }
    Invoke-Checked $python @($coach, '--data-dir', $data, 'migrate', '--legacy-root', $legacy)
}

if (-not (Test-Path -LiteralPath $skillTarget)) {
    New-Item -ItemType Directory -Force -Path $skillsRoot | Out-Null
    New-Item -ItemType Junction -Path $skillTarget -Target $skillSource | Out-Null
}

if ($useDefaultData) {
    [Environment]::SetEnvironmentVariable('BODY_FITNESS_DATA_DIR', $null, 'User')
    Remove-Item Env:BODY_FITNESS_DATA_DIR -ErrorAction SilentlyContinue
} else {
    [Environment]::SetEnvironmentVariable('BODY_FITNESS_DATA_DIR', $data, 'User')
    $env:BODY_FITNESS_DATA_DIR = $data
}
[Environment]::SetEnvironmentVariable('LARK_CHANNEL_PROFILE', $Profile, 'User')
$env:LARK_CHANNEL_PROFILE = $Profile

Invoke-Checked $python ($coachArguments + @('self-check'))
Invoke-Checked $python ($coachArguments + @('doctor'))

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$dailyAction = New-ScheduledTaskAction -Execute $pythonw -Argument $dailyArguments
$dailyTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$dailyTrigger.Delay = 'PT3M'
Register-ScheduledTask -TaskName 'BodyCoach-DailyReport' -Action $dailyAction -Trigger $dailyTrigger -Settings $settings -Description 'Send the daily private fitness report after logon' -Force | Out-Null

$weeklyAction = New-ScheduledTaskAction -Execute $pythonw -Argument $weeklyArguments
$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '23:00'
Register-ScheduledTask -TaskName 'BodyCoach-WeeklyReport' -Action $weeklyAction -Trigger $weeklyTrigger -Settings $settings -Description 'Send the weekly private fitness report on Sunday night' -Force | Out-Null

$mealAction = New-ScheduledTaskAction -Execute $pythonw -Argument $mealArguments
$mealTrigger = New-ScheduledTaskTrigger -Daily -At '23:30'
Register-ScheduledTask -TaskName 'BodyCoach-MealReminder' -Action $mealAction -Trigger $mealTrigger -Settings $settings -Description 'Remind when a meal record is missing' -Force | Out-Null

Write-Host "Installed body-fitness-coach with private data at $data"
