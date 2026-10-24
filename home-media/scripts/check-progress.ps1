$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
Push-Location $taskRoot
try { & (Join-Path $taskRoot '.venv\Scripts\python.exe') -m home_media.progress }
finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { Write-Warning 'Could not read progress; see the error above.' }
