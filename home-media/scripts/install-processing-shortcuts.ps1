$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$desktop = [Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)
$shortcutFolder = Join-Path $desktop 'Home media'
$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function New-ProcessingShortcut(
    [string]$Name,
    [string]$ScriptPath,
    [string]$PowerShellArguments,
    [string]$ScriptArguments,
    [int]$WindowStyle
) {
    $shortcut = $shell.CreateShortcut((Join-Path $shortcutFolder ($Name + '.lnk')))
    $shortcut.TargetPath = $powerShellExe
    $shortcut.Arguments = $PowerShellArguments + ' -File "' + $ScriptPath + '"'
    if ($ScriptArguments) { $shortcut.Arguments += ' ' + $ScriptArguments }
    $shortcut.WorkingDirectory = $taskRoot
    $shortcut.WindowStyle = $WindowStyle
    $shortcut.Description = $Name
    $shortcut.Save()
}

if (-not (Test-Path -LiteralPath $powerShellExe -PathType Leaf)) {
    throw 'Windows PowerShell could not be found.'
}
[IO.Directory]::CreateDirectory($shortcutFolder) | Out-Null
$shell = New-Object -ComObject WScript.Shell
New-ProcessingShortcut 'Start or resume tagging' (Join-Path $PSScriptRoot 'run-worker.ps1') '-NoProfile' '' 1
New-ProcessingShortcut 'Stop tagging' (Join-Path $PSScriptRoot 'stop-processing.ps1') '-NoProfile -NoExit' '' 1
New-ProcessingShortcut 'Check server tagging requests' (Join-Path $PSScriptRoot 'check-tagging-on-connect.ps1') '-NoProfile -NoExit' '-Manual' 1
New-ProcessingShortcut 'Check tagging progress' (Join-Path $PSScriptRoot 'check-progress.ps1') '-NoProfile -NoExit' '' 1
Write-Output "Created manual processing shortcuts in $shortcutFolder"
