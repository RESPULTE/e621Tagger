param([int]$Limit = 0)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& '.\.venv\Scripts\python.exe' -c "import sys; from home_media.config import Settings; from home_media.connection import worker_available; sys.exit(0 if worker_available(Settings.load('config.toml')) else 10)"
if ($LASTEXITCODE -eq 10) { Write-Output 'Tagging is already running. Use Stop tagging to stop it.'; exit 0 }
if ($LASTEXITCODE -ne 0) { throw 'Could not validate the tagging worker settings.' }
$workerArgs = @('-m', 'home_media.cli', '--config', 'config.toml')
if ($Limit -gt 0) { $workerArgs += @('--limit', "$Limit") }
& '.\.venv\Scripts\python.exe' @workerArgs
exit $LASTEXITCODE
