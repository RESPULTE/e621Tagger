param([switch]$Manual)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$networkEvent = Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-NetworkProfile/Operational'; Id=10000} -MaxEvents 1
$bootTime = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
$connectionText = "$bootTime/$($networkEvent.RecordId)/$($networkEvent.TimeCreated.ToUniversalTime().ToString('o'))"
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $connectionId = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($connectionText))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
$arguments = @('-m', 'home_media.connection', '--connection-id', $connectionId)
if ($Manual) { $arguments += '--manual' }
if ($Manual) {
    Push-Location $taskRoot
    try { & (Join-Path $taskRoot '.venv\Scripts\python.exe') @arguments }
    finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Request check failed; see the output above.' }
} else {
    $process = Start-Process -FilePath (Join-Path $taskRoot '.venv\Scripts\pythonw.exe') -ArgumentList $arguments -WorkingDirectory $taskRoot -WindowStyle Hidden -Wait -PassThru
    exit $process.ExitCode
}
