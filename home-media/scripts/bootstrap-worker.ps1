$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$sourceLock = Get-Content '.\upstream.lock.json' -Raw | ConvertFrom-Json
$workspaceRoot = (Resolve-Path '..').Path
if (-not (Test-Path (Join-Path $workspaceRoot 'hydra\model.py'))) {
    throw 'Hydra source is missing from the parent e621Tagger workspace.'
}
uv sync --python 3.11
if ($LASTEXITCODE) { throw 'Base dependency installation failed' }
uv pip install --python '.\.venv\Scripts\python.exe' --no-deps 'https://download.pytorch.org/whl/cu128/torch-2.8.0%2Bcu128-cp311-cp311-win_amd64.whl'
if ($LASTEXITCODE) { throw 'CUDA PyTorch installation failed' }
uv pip install --python '.\.venv\Scripts\python.exe' -r requirements-gpu.txt
if ($LASTEXITCODE) { throw 'Hydra dependency installation failed' }
New-Item -ItemType Directory -Path (Join-Path $workspaceRoot 'models') -Force | Out-Null
$modelPath = Join-Path $workspaceRoot 'models\hydra-3.5.safetensors'
if (-not (Test-Path $modelPath)) {
    $partialPath = "$modelPath.part"
    Invoke-WebRequest -Uri $sourceLock.model -OutFile $partialPath
    if ((Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sourceLock.sha256) {
        throw 'Model checksum mismatch; partial file retained for diagnosis'
    }
    Move-Item -LiteralPath $partialPath -Destination $modelPath
}
if ((Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sourceLock.sha256) {
    throw 'Model checksum mismatch'
}
& '.\.venv\Scripts\python.exe' -c 'import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name())'
if ($LASTEXITCODE) { throw 'CUDA device check failed' }
