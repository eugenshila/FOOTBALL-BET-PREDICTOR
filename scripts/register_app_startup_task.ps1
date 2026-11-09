param([string]$TaskName = "VERIFIX App Startup")

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot "start_app.ps1"
if (-not (Test-Path -LiteralPath $launcher)) { throw "Startup script not found: $launcher" }

$powershell = Join-Path $PSHOME "powershell.exe"
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(3))

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Start the VERIFIX web app after this user signs in, waiting for PostgreSQL." -Force | Out-Null
Write-Output "Registered '$TaskName' to run when $env:USERDOMAIN\$env:USERNAME signs in. PostgreSQL is already configured to start automatically."
