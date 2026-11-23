param(
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$At = "23:00",
    [string]$TaskName = "VERIFIX Cloud Backup",
    [ValidateSet("Auto", "Python", "PowerShell")]
    [string]$Runner = "Auto"
)

# Registers a daily Windows Task Scheduler job for the VERIFIX backup.
#
# Runner Auto (default)  -> scripts\backup_to_cloud.py, which works with the
#                           default SQLite database AND Google Drive backups.
# Runner PowerShell      -> legacy scripts\backup_to_cloud.ps1, which needs
#                           PostgreSQL + pg_dump + AWS CLI.
#
# Run it from PowerShell:
#   .\scripts\register_cloud_backup_task.ps1 -At 23:00

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$exportsDir = Join-Path $projectRoot "exports"
$logPath = Join-Path $exportsDir "cloud-backup.log"
$pythonScript = Join-Path $PSScriptRoot "backup_to_cloud.py"
$powershellScript = Join-Path $PSScriptRoot "backup_to_cloud.ps1"
$powershellExe = Join-Path $PSHOME "powershell.exe"

function Resolve-PythonExecutable {
    $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw "Python was not found. Install Python 3.11+, or create .venv in $projectRoot, then run this again."
}

if ($Runner -eq "Auto") {
    $hasPython = (Get-Command python -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath (Join-Path $projectRoot ".venv\Scripts\python.exe"))
    if ($hasPython -and (Test-Path -LiteralPath $pythonScript)) {
        $Runner = "Python"
    } else {
        $Runner = "PowerShell"
    }
}

New-Item -ItemType Directory -Force -Path $exportsDir | Out-Null

if ($Runner -eq "Python") {
    if (-not (Test-Path -LiteralPath $pythonScript)) { throw "Backup script not found: $pythonScript" }
    $pythonExe = Resolve-PythonExecutable
    # Append both output streams to the log so a failed nightly run is visible.
    $inner = "& '$pythonExe' '$pythonScript' *>> '$logPath'"
    $arguments = "-NoProfile -ExecutionPolicy Bypass -Command `"$inner`""
    $action = New-ScheduledTaskAction -Execute $powershellExe -Argument $arguments -WorkingDirectory $projectRoot
    $description = "Back up the VERIFIX inventory database and part photos to the configured cloud destination (Google Drive or S3-compatible storage)."
}
else {
    if (-not (Test-Path -LiteralPath $powershellScript)) { throw "Backup script not found: $powershellScript" }
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$powershellScript`""
    $action = New-ScheduledTaskAction -Execute $powershellExe -Argument $arguments -WorkingDirectory $projectRoot
    $description = "Back up VERIFIX PostgreSQL inventory and uploaded photos to AWS S3-compatible storage (legacy PowerShell runner)."
}

$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, "HH:mm", [Globalization.CultureInfo]::InvariantCulture))
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType InteractiveToken -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings -Description $description -Force | Out-Null

Write-Output "Registered '$TaskName' to run daily at $At ($Runner runner)."
Write-Output "It runs while $env:USERDOMAIN\$env:USERNAME is signed in, and catches up if the PC was asleep or off."
Write-Output "Log file: $logPath"
Write-Output ""
Write-Output "Test it right now: open Task Scheduler, right-click '$TaskName', choose Run, then read the log."
