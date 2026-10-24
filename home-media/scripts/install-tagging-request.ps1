$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$pythonw = Join-Path $taskRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Set up the workspace Python environment first.' }
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$escapedCommand = [System.Security.SecurityElement]::Escape($powershell)
$escapedArguments = [System.Security.SecurityElement]::Escape('-NoProfile -NonInteractive -WindowStyle Hidden -File "' + (Join-Path $taskRoot 'scripts\check-tagging-on-connect.ps1') + '"')
$escapedDirectory = [System.Security.SecurityElement]::Escape($taskRoot)
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>One-shot home-server tagging request check on network connection and sign-in. GPU work needs a click; no resident queue listener.</Description></RegistrationInfo>
  <Triggers>
    <EventTrigger><Enabled>true</Enabled><Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational"&gt;&lt;Select Path="Microsoft-Windows-NetworkProfile/Operational"&gt;*[System[EventID=10000]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription><Delay>PT20S</Delay></EventTrigger>
    <LogonTrigger><Enabled>true</Enabled><UserId>$sid</UserId><Delay>PT30S</Delay></LogonTrigger>
  </Triggers>
  <Principals><Principal id="CurrentUser"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><Enabled>true</Enabled><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings>
  <Actions Context="CurrentUser"><Exec><Command>$escapedCommand</Command><Arguments>$escapedArguments</Arguments><WorkingDirectory>$escapedDirectory</WorkingDirectory></Exec></Actions>
</Task>
"@
Register-ScheduledTask -TaskName 'HomeMedia-TaggingRequest' -Xml $taskXml -User $account -Force | Out-Null
Start-ScheduledTask -TaskName 'HomeMedia-TaggingRequest'
Get-ScheduledTask -TaskName 'HomeMedia-TaggingRequest' | Select-Object TaskName, State
