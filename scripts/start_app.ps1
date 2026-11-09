# Start the VERIFIX web app after logon, once PostgreSQL is available.

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $projectRoot "data"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$appLog = Join-Path $dataDir "app-startup.log"
$stdoutLog = Join-Path $dataDir "app-uvicorn-out.log"
$stderrLog = Join-Path $dataDir "app-uvicorn-error.log"

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
function Write-StartupLog([string]$Message) {
    Add-Content -LiteralPath $appLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-StartupLog "ERROR: Python virtual environment not found at $python"
    exit 1
}

if (Test-NetConnection -ComputerName localhost -Port 8000 -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Write-StartupLog "App already listening on port 8000; skipping launch."
    exit 0
}

$deadline = (Get-Date).AddMinutes(2)
while ((Get-Date) -lt $deadline) {
    $databaseReady = Test-NetConnection -ComputerName localhost -Port 5432 -InformationLevel Quiet -WarningAction SilentlyContinue
    if ($databaseReady) { break }
    Start-Sleep -Seconds 3
}
if (-not $databaseReady) {
    Write-StartupLog "ERROR: PostgreSQL did not become available on localhost:5432 within 2 minutes."
    exit 1
}

if (Test-NetConnection -ComputerName localhost -Port 8000 -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Write-StartupLog "App became available on port 8000 while waiting; skipping launch."
    exit 0
}

try {
    $process = Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000") `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    $appReady = $false
    $startupDeadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $startupDeadline) {
        if (Test-NetConnection -ComputerName localhost -Port 8000 -InformationLevel Quiet -WarningAction SilentlyContinue) {
            $appReady = $true
            break
        }
        if ($process.HasExited) {
            Write-StartupLog "ERROR: App process exited during startup (exit code $($process.ExitCode)); see app-uvicorn-error.log."
            exit 1
        }
        Start-Sleep -Seconds 2
    }
    if (-not $appReady) {
        Write-StartupLog "ERROR: App did not open port 8000 within 45 seconds; see app-uvicorn-error.log."
        exit 1
    }
    Write-StartupLog "Started app process $($process.Id) on port 8000."
} catch {
    Write-StartupLog "ERROR: Could not launch app: $($_.Exception.Message)"
    exit 1
}
