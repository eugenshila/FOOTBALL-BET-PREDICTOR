param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectRoot ".env"
$uploadDir = Join-Path $projectRoot "uploads"
$profileFile = Join-Path $projectRoot "data\shop_profile.json"
$logDir = Join-Path $projectRoot "exports"
$logFile = Join-Path $logDir "cloud-backup.log"

function Write-BackupLog([string]$Message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logFile -Value "$stamp $Message" -Encoding UTF8
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
if (-not (Test-Path -LiteralPath $envFile)) { throw "Missing .env file at $envFile" }

# Load only this project's .env values into this script process. Never print them.
foreach ($rawLine in Get-Content -LiteralPath $envFile) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { continue }
    $key, $value = $line -split "=", 2
    $key = $key.Trim()
    $value = $value.Trim()
    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    if ($key -and -not (Test-Path "Env:$key")) { Set-Item -Path "Env:$key" -Value $value }
}

# The app can be configured with DATABASE_URL or with DB_ENGINE + POSTGRES_*.
# Build the equivalent DSN from the parts when only the parts are set.
if (-not $env:DATABASE_URL -and $env:DB_ENGINE -and $env:DB_ENGINE -notin @("postgres", "postgresql")) {
    throw "DB_ENGINE=$($env:DB_ENGINE) is not PostgreSQL. Cloud backups need the PostgreSQL engine."
}
if (-not $env:DATABASE_URL -and -not $env:DB_ENGINE) {
    # PostgreSQL is the app default; keep backups working without extra config.
    $env:DB_ENGINE = "postgres"
}
if (-not $env:DATABASE_URL -and $env:DB_ENGINE -in @("postgres", "postgresql")) {
    $pgHost = if ($env:POSTGRES_HOST) { $env:POSTGRES_HOST } else { "localhost" }
    $pgPort = if ($env:POSTGRES_PORT) { $env:POSTGRES_PORT } else { "5432" }
    $pgDb = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "partsid" }
    $pgUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "partsid_user" }
    $credentials = [Uri]::EscapeDataString($pgUser)
    if ($env:POSTGRES_PASSWORD) {
        $credentials = "$credentials:$([Uri]::EscapeDataString($env:POSTGRES_PASSWORD))"
    }
    $env:DATABASE_URL = "postgresql://$credentials@$($pgHost):$($pgPort)/$($pgDb)"
}
if (-not $env:DATABASE_URL -or $env:DATABASE_URL -notmatch '^postgres(ql)?://') {
    throw "Database is not PostgreSQL. Set DATABASE_URL (or DB_ENGINE=postgres + POSTGRES_*) before backups can run."
}
if (-not $env:CLOUD_BACKUP_BUCKET) { throw "Set CLOUD_BACKUP_BUCKET in .env before enabling scheduled backups." }
if (-not $env:AWS_ACCESS_KEY_ID -or -not $env:AWS_SECRET_ACCESS_KEY) {
    throw "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the ignored local .env file before running this script."
}

$pgDump = Get-Command "pg_dump" -ErrorAction SilentlyContinue
$pgDumpPath = if ($env:PG_DUMP_PATH -and (Test-Path -LiteralPath $env:PG_DUMP_PATH)) {
    $env:PG_DUMP_PATH
} elseif ($pgDump) {
    $pgDump.Source
} else {
    $postgresRoot = Join-Path $env:ProgramFiles "PostgreSQL"
    $foundDump = if (Test-Path -LiteralPath $postgresRoot) {
        Get-ChildItem -LiteralPath $postgresRoot -Filter "pg_dump.exe" -File -Recurse -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
    }
    if ($foundDump) { $foundDump.FullName } else { $null }
}
if (-not $pgDumpPath) { throw "pg_dump was not found. Set PG_DUMP_PATH in .env or add PostgreSQL's bin folder to PATH." }
$aws = Get-Command "aws" -ErrorAction SilentlyContinue
if (-not $aws) { throw "AWS CLI was not found. Install AWS CLI v2 and add it to PATH." }

# Put PostgreSQL credentials in process environment variables, not command arguments.
$dbUri = [Uri]$env:DATABASE_URL
$userInfo = $dbUri.UserInfo -split ":", 2
$env:PGHOST = $dbUri.Host
$env:PGPORT = if ($dbUri.Port -gt 0) { [string]$dbUri.Port } else { "5432" }
$env:PGDATABASE = [Uri]::UnescapeDataString($dbUri.AbsolutePath.TrimStart("/"))
$env:PGUSER = [Uri]::UnescapeDataString($userInfo[0])
$env:PGPASSWORD = if ($userInfo.Count -gt 1) { [Uri]::UnescapeDataString($userInfo[1]) } else { "" }
$query = [System.Web.HttpUtility]::ParseQueryString($dbUri.Query)
if ($query["sslmode"]) { $env:PGSSLMODE = $query["sslmode"] }

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$workDir = Join-Path ([IO.Path]::GetTempPath()) "verifix-backup-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $workDir | Out-Null
$dbDump = Join-Path $workDir "inventory-$timestamp.dump"
$mediaArchive = Join-Path $workDir "uploads-and-profile-$timestamp.zip"
$bucket = $env:CLOUD_BACKUP_BUCKET.Trim().Trim("/")
$prefix = if ($env:CLOUD_BACKUP_PREFIX) { $env:CLOUD_BACKUP_PREFIX.Trim("/") } else { "verifix/backups" }
$region = if ($env:CLOUD_BACKUP_REGION) { $env:CLOUD_BACKUP_REGION } else { "us-east-1" }
$destination = "s3://$bucket/$prefix/$timestamp"

try {
    Write-BackupLog "Starting database and photo backup."
    & $pgDumpPath --format=custom --no-owner --no-acl --file $dbDump
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }

    $archiveItems = @()
    if (Test-Path -LiteralPath $uploadDir) { $archiveItems += $uploadDir }
    if (Test-Path -LiteralPath $profileFile) { $archiveItems += $profileFile }
    if ($archiveItems.Count -gt 0) {
        Compress-Archive -Path $archiveItems -DestinationPath $mediaArchive -CompressionLevel Optimal
    } else {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::Open($mediaArchive, [System.IO.Compression.ZipArchiveMode]::Create).Dispose()
    }

    $awsArgs = @("s3", "cp", $dbDump, "$destination/$([IO.Path]::GetFileName($dbDump))", "--region", $region)
    if ($env:CLOUD_BACKUP_ENDPOINT) { $awsArgs += @("--endpoint-url", $env:CLOUD_BACKUP_ENDPOINT) }
    & $aws.Source @awsArgs
    if ($LASTEXITCODE -ne 0) { throw "Cloud upload of the database dump failed with exit code $LASTEXITCODE" }

    $awsArgs = @("s3", "cp", $mediaArchive, "$destination/$([IO.Path]::GetFileName($mediaArchive))", "--region", $region)
    if ($env:CLOUD_BACKUP_ENDPOINT) { $awsArgs += @("--endpoint-url", $env:CLOUD_BACKUP_ENDPOINT) }
    & $aws.Source @awsArgs
    if ($LASTEXITCODE -ne 0) { throw "Cloud upload of photos/settings failed with exit code $LASTEXITCODE" }

    Write-BackupLog "Backup completed successfully at $destination."
    Write-Output "Backup completed: $destination"
} catch {
    Write-BackupLog "Backup failed: $($_.Exception.Message)"
    throw
} finally {
    # Delete only the unique temporary folder created by this run.
    $resolvedWork = [IO.Path]::GetFullPath($workDir)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $resolvedParent = [IO.Path]::GetDirectoryName($resolvedWork)
    if ($resolvedParent.Equals($tempRoot.TrimEnd([IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase) -and [IO.Path]::GetFileName($resolvedWork).StartsWith("verifix-backup-", [StringComparison]::Ordinal) -and (Test-Path -LiteralPath $resolvedWork)) {
        Remove-Item -LiteralPath $resolvedWork -Recurse -Force
    }
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
