param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$TokenFile = (Join-Path $env:LOCALAPPDATA 'HydraTrainingStudio/processing-worker.token'),
    [string]$VectorIndex = (Join-Path $env:LOCALAPPDATA 'HydraTrainingStudio/image-index.sqlite3'),
    [string]$BroadIndex,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = 'Stop'
$origin = $null
if (-not [Uri]::TryCreate($Server, [UriKind]::Absolute, [ref]$origin) -or
    $origin.Scheme -notin @('http', 'https') -or $origin.UserInfo -or
    $origin.AbsolutePath -ne '/' -or $origin.Query -or $origin.Fragment) {
    throw 'Server must be an HTTP(S) origin without credentials or a path.'
}
$workerRoot = Split-Path $PSScriptRoot -Parent
$launcher = Join-Path $PSScriptRoot 'run-similarity-worker.ps1'
foreach ($workerPath in @($launcher, (Join-Path $workerRoot '.venv-similarity/Scripts/python.exe'),
    (Join-Path $workerRoot 'models/hydra-3.5.safetensors'), $TokenFile, $VectorIndex)) {
    if (-not (Test-Path -LiteralPath $workerPath -PathType Leaf)) {
        throw "Required worker file missing: $workerPath. See docs/similarity.md."
    }
}
# Codex's packaged Windows process can virtualize LocalAppData. Task Scheduler
# runs outside that package, so persist physical paths to the existing files.
$resolvedPaths = & (Join-Path $workerRoot '.venv-similarity/Scripts/python.exe') -c `
    'import json,sys; from pathlib import Path; print(json.dumps([str(Path(p).resolve(strict=True)) for p in sys.argv[1:]]))' $TokenFile $VectorIndex
if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve private worker file paths.' }
$resolvedPaths = $resolvedPaths | ConvertFrom-Json
$TokenFile = $resolvedPaths[0]
$VectorIndex = $resolvedPaths[1]
if ($BroadIndex) {
    $broadManifest = Join-Path $BroadIndex 'manifest.json'
    $resolvedBroad = & (Join-Path $workerRoot '.venv-similarity/Scripts/python.exe') -c `
        'import json,sys; from pathlib import Path; p=Path(sys.argv[1]).resolve(strict=True); m=json.loads(p.read_text()); assert m.get("validation",{}).get("passed") is True, "Broad generation has not passed validation"; print(str(p.parent))' $broadManifest
    if ($LASTEXITCODE -ne 0) { throw 'Broad image generation is unavailable or unvalidated.' }
    $BroadIndex = $resolvedBroad
}
# Task Scheduler passes one command line; reject embedded quotes rather than
# allowing a supplied argument to become another PowerShell option.
foreach ($argument in @($launcher, $Server, $TokenFile, $VectorIndex, $BroadIndex)) {
    if (-not $argument) { continue }
    if ($argument.Contains('"') -or $argument.Contains("`r") -or $argument.Contains("`n")) {
        throw 'Worker arguments cannot contain quotes or newlines.'
    }
}
$taskName = 'HydraTrainingStudio-Similarity'
$powershell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$logFile = Join-Path (Split-Path $TokenFile -Parent) 'similarity-worker.log'
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" -Server `"$Server`" -TokenFile `"$TokenFile`" -VectorIndex `"$VectorIndex`" -LogFile `"$logFile`""
if ($BroadIndex) { $arguments += " -BroadIndex `"$BroadIndex`"" }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Count -ne 1 -or
    $existing.Actions[0].Execute -ne $powershell -or $existing.Actions[0].Arguments -ne $arguments)) {
    if (-not $ReplaceExisting -or $existing.Actions.Count -ne 1 -or
        $existing.Actions[0].Execute -ne $powershell -or -not $existing.Actions[0].Arguments.Contains("-File `"$launcher`"")) {
        throw "Task $taskName already exists with different settings; inspect it before replacing it."
    }
}
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $workerRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT30S'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description 'Outbound Hydra image retrieval GPU worker' -Force | Out-Null
Write-Output "Installed $taskName for $user at sign-in. Keep the current worker running; do not start a second worker."
