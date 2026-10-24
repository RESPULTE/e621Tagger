param([switch]$StatusOnly)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$dataRoot = Join-Path $taskRoot 'data'
$controlPath = Join-Path $dataRoot 'worker-control.json'
$requestPath = Join-Path $dataRoot 'worker-stop.json'
$currentRunPath = Join-Path $dataRoot 'current-run.json'
$expectedPython = [IO.Path]::GetFullPath((Join-Path $taskRoot '.venv\Scripts\python.exe'))
$runWorkerPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'run-worker.ps1'))

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Get-ProcessById([int]$Id) {
    if ($Id -le 0) { return $null }
    return Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $Id" -ErrorAction SilentlyContinue
}

function Get-CreationUtc($Process) {
    if (-not $Process -or -not $Process.CreationDate) { return $null }
    if ($Process.CreationDate -is [DateTime]) { return $Process.CreationDate.ToUniversalTime() }
    return [Management.ManagementDateTimeConverter]::ToDateTime($Process.CreationDate).ToUniversalTime()
}

function Test-StartedAt($Process, $StartedAt) {
    if (-not $StartedAt) { return $false }
    try {
        if ($StartedAt -is [DateTimeOffset]) {
            $expected = $StartedAt.UtcDateTime
        } elseif ($StartedAt -is [DateTime]) {
            $expected = $StartedAt.ToUniversalTime()
        } else {
            $expected = [DateTimeOffset]::Parse([string]$StartedAt).UtcDateTime
        }
        $actual = Get-CreationUtc $Process
        if (-not $actual) { return $false }
        return [Math]::Abs(($actual - $expected).TotalSeconds) -le 10
    } catch { return $false }
}

function Test-WorkerCommand($Process) {
    return $Process -and $Process.CommandLine -and
        [regex]::IsMatch($Process.CommandLine, '(?:^|\s)-m\s+home_media\.cli(?:\s|$)', 'IgnoreCase')
}

function Test-WorkerProcess($Process, $Snapshot) {
    if (-not $Process -or -not $Process.ExecutablePath) { return $false }
    try {
        $actualPath = [IO.Path]::GetFullPath($Process.ExecutablePath)
        if ([string]::Equals($actualPath, $expectedPython, [StringComparison]::OrdinalIgnoreCase)) {
            return Test-WorkerCommand $Process
        }
    } catch { return $false }

    if ([IO.Path]::GetFileName($Process.ExecutablePath) -notmatch '^python(?:\d+(?:\.\d+)*)?\.exe$' -or
        -not (Test-WorkerCommand $Process)) { return $false }
    $parentId = [int]$Process.ParentProcessId
    if ($Snapshot) {
        $parent = $Snapshot | Where-Object { [int]$_.ProcessId -eq $parentId } | Select-Object -First 1
    } else {
        $parent = Get-ProcessById $parentId
    }
    if (-not $parent -or -not $parent.ExecutablePath -or -not (Test-WorkerCommand $parent)) { return $false }
    try {
        return [string]::Equals(
            [IO.Path]::GetFullPath($parent.ExecutablePath),
            $expectedPython,
            [StringComparison]::OrdinalIgnoreCase
        )
    } catch { return $false }
}

function Test-SameProcess($Expected, $Current) {
    if (-not $Expected -or -not $Current) { return $false }
    $expectedCreated = Get-CreationUtc $Expected
    $currentCreated = Get-CreationUtc $Current
    return $expectedCreated -and $currentCreated -and
        [Math]::Abs(($currentCreated - $expectedCreated).TotalMilliseconds) -lt 1
}

function Write-StopRequest([string]$RunId) {
    [IO.Directory]::CreateDirectory($dataRoot) | Out-Null
    $document = [ordered]@{
        run_id = $RunId
        requested_at = [DateTimeOffset]::UtcNow.ToString('o')
    }
    $json = $document | ConvertTo-Json -Compress
    $temporary = Join-Path $dataRoot ('.worker-stop-' + $PID + '.tmp')
    $encoding = [Text.UTF8Encoding]::new($false)
    try {
        [IO.File]::WriteAllText($temporary, $json, $encoding)
        if (Test-Path -LiteralPath $requestPath -PathType Leaf) {
            [IO.File]::Replace($temporary, $requestPath, $null)
        } else {
            [IO.File]::Move($temporary, $requestPath)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Get-DescendantTree($Snapshot, [int]$RootId) {
    $found = @{}
    $depths = @{}
    $pending = New-Object 'System.Collections.Generic.Queue[object]'
    $pending.Enqueue([pscustomobject]@{ Id = $RootId; Depth = 0 })
    $found[$RootId] = $true
    while ($pending.Count -gt 0) {
        $entry = $pending.Dequeue()
        foreach ($child in $Snapshot) {
            $childId = [int]$child.ProcessId
            if ([int]$child.ParentProcessId -ne $entry.Id -or $found.ContainsKey($childId)) { continue }
            $found[$childId] = $true
            $depths[$childId] = $entry.Depth + 1
            $pending.Enqueue([pscustomobject]@{ Id = $childId; Depth = $entry.Depth + 1 })
        }
    }
    return @($Snapshot | Where-Object { $depths.ContainsKey([int]$_.ProcessId) } |
        ForEach-Object { [pscustomobject]@{ Process = $_; Depth = $depths[[int]$_.ProcessId] } } |
        Sort-Object Depth -Descending)
}

function Stop-VerifiedTree($Worker, [switch]$IncludeWrapper) {
    $snapshot = @(Get-CimInstance -ClassName Win32_Process)
    $currentWorker = $snapshot | Where-Object { [int]$_.ProcessId -eq [int]$Worker.ProcessId } | Select-Object -First 1
    if (-not (Test-SameProcess $Worker $currentWorker) -or -not (Test-WorkerProcess $currentWorker $snapshot)) {
        return
    }

    $tree = @(Get-DescendantTree $snapshot ([int]$Worker.ProcessId))
    foreach ($entry in $tree) {
        $candidate = $entry.Process
        $current = Get-ProcessById ([int]$candidate.ProcessId)
        if (-not (Test-SameProcess $candidate $current)) { continue }
        Stop-Process -Id ([int]$candidate.ProcessId) -Force -ErrorAction SilentlyContinue
    }

    $currentWorker = Get-ProcessById ([int]$Worker.ProcessId)
    if (Test-SameProcess $Worker $currentWorker) {
        if (Test-WorkerProcess $currentWorker $snapshot) {
            Stop-Process -Id ([int]$Worker.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }

    if ($IncludeWrapper) {
        $parentId = [int]$Worker.ParentProcessId
        $wrapper = $snapshot | Where-Object { [int]$_.ProcessId -eq $parentId } | Select-Object -First 1
        if ($wrapper -and
            [IO.Path]::GetFileName($wrapper.ExecutablePath) -match '^(powershell|pwsh)\.exe$' -and
            $wrapper.CommandLine.IndexOf($runWorkerPath, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $currentWrapper = Get-ProcessById $parentId
            if (Test-SameProcess $wrapper $currentWrapper) {
                Stop-Process -Id $parentId -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Stop-Worker($Worker, [string]$RunId, [switch]$IncludeWrapper) {
    Write-StopRequest $RunId
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        $current = Get-ProcessById ([int]$Worker.ProcessId)
        if (-not (Test-SameProcess $Worker $current)) {
            Write-Output 'Tagging worker stopped.'
            return
        }
        Start-Sleep -Seconds 1
    }

    $current = Get-ProcessById ([int]$Worker.ProcessId)
    if (Test-SameProcess $Worker $current) {
        Stop-VerifiedTree $Worker -IncludeWrapper:$IncludeWrapper
    }
    Write-Output 'Stop requested; the verified worker process tree was terminated if it had not stopped cooperatively.'
}

$control = Read-JsonFile $controlPath
if (Test-Path -LiteralPath $controlPath -PathType Leaf) {
    if (-not $control -or -not $control.pid -or -not $control.run_id -or -not $control.started_at) {
        throw 'Worker control metadata is unreadable or incomplete; refusing to stop a process.'
    }
    $worker = Get-ProcessById ([int]$control.pid)
    if (-not (Test-WorkerProcess $worker) -or -not (Test-StartedAt $worker $control.started_at)) {
        Write-Output 'No matching verified tagging worker is running.'
        exit 0
    }
    if ($StatusOnly) {
        Write-Output "Verified worker PID $($worker.ProcessId) (worker-control manifest)."
        exit 0
    }
    Stop-Worker $worker ([string]$control.run_id)
    exit 0
}

$legacy = Read-JsonFile $currentRunPath
if (-not $legacy -or -not $legacy.pid -or -not $legacy.started_at) {
    Write-Output 'No worker control manifest or legacy run manifest was found.'
    exit 0
}
$legacyWorker = Get-ProcessById ([int]$legacy.pid)
if (-not (Test-WorkerProcess $legacyWorker) -or -not (Test-StartedAt $legacyWorker $legacy.started_at)) {
    Write-Output 'No matching verified legacy tagging worker is running.'
    exit 0
}
if ($StatusOnly) {
    Write-Output "Verified worker PID $($legacyWorker.ProcessId) (legacy current-run manifest)."
    exit 0
}
$legacyRunId = 'legacy-' + [string]$legacy.pid + '-' + ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
Stop-Worker $legacyWorker $legacyRunId -IncludeWrapper
