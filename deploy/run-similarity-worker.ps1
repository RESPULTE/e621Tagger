param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$TokenFile = (Join-Path $env:LOCALAPPDATA 'HydraTrainingStudio/processing-worker.token'),
    [string]$VectorIndex = (Join-Path $env:LOCALAPPDATA 'HydraTrainingStudio/image-index.sqlite3'),
    [string]$LogFile,
    [string]$BroadIndex
)

$ErrorActionPreference = 'Stop'
if ($LogFile) {
    if ((Test-Path -LiteralPath $LogFile) -and (Get-Item -LiteralPath $LogFile).Length -gt 1MB) {
        Move-Item -LiteralPath $LogFile -Destination "$LogFile.previous" -Force
    }
    Start-Transcript -LiteralPath $LogFile -Append | Out-Null
}
$workerRoot = Split-Path $PSScriptRoot -Parent
$workerPython = Join-Path $workerRoot '.venv-similarity/Scripts/python.exe'
$workerCheckpoint = Join-Path $workerRoot 'models/hydra-3.5.safetensors'
foreach ($workerPath in @($workerPython, $workerCheckpoint, $TokenFile, $VectorIndex)) {
    if (-not (Test-Path -LiteralPath $workerPath -PathType Leaf)) {
        throw "Required worker file missing: $workerPath. See docs/similarity.md."
    }
}
if ($BroadIndex -and -not (Test-Path -LiteralPath (Join-Path $BroadIndex 'manifest.json') -PathType Leaf)) {
    throw 'Broad image generation is missing its manifest.'
}

Push-Location $workerRoot
try {
    $workerArguments = @('-m', 'backend.similarity_worker', '--server', $Server, '--vector-index', $VectorIndex,
        '--checkpoint', $workerCheckpoint, '--token-file', $TokenFile)
    if ($BroadIndex) { $workerArguments += @('--broad-index', $BroadIndex) }
    & $workerPython @workerArguments
    if ($LASTEXITCODE -ne 0) { throw 'Image worker stopped with an error; check the output above.' }
} finally {
    Pop-Location
    if ($LogFile) { Stop-Transcript | Out-Null }
}
