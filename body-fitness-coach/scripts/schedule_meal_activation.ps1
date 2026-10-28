[CmdletBinding(SupportsShouldProcess=$true)]
param([Parameter(Mandatory=$true)][int]$AgentPid, [Parameter(Mandatory=$true)][int]$BridgePid)
$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path $PSScriptRoot -Parent
$taskName = 'BodyCoach-ActivateMealForms'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw 'Activation task already exists; inspect it before replacing.'
}
$pythonw = Join-Path $skillRoot '.private\.venv\Scripts\pythonw.exe'
$helper = Join-Path $PSScriptRoot 'activate_meal_forms.py'
if (-not (Test-Path -LiteralPath $pythonw) -or -not (Test-Path -LiteralPath $helper)) {
    throw 'Activation runtime missing.'
}
$arguments = '"{0}" --agent-pid {1} --bridge-pid {2}' -f $helper, $AgentPid, $BridgePid
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $skillRoot
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1))
$trigger.EndBoundary = (Get-Date).AddDays(1).ToString('s')
$settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DeleteExpiredTaskAfter (New-TimeSpan -Days 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
if ($PSCmdlet.ShouldProcess($taskName, 'Schedule one-shot activation after the bridge agent exits')) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Activate body coach meal forms after agent exit and verify delivery' | Select-Object TaskName,State
}
