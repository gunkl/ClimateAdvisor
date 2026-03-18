<#
.SYNOPSIS
    Deploy Climate Advisor integration to a Home Assistant OS instance.

.DESCRIPTION
    Validates, backs up, deploys, and optionally restarts the Climate Advisor
    integration on a remote HAOS server via SSH/SCP.

.PARAMETER Rollback
    Restore the most recent backup and restart HA.

.PARAMETER SkipRestart
    Deploy files without restarting Home Assistant.

.PARAMETER ReloadOnly
    Use HA API to reload the integration instead of a full restart.
    Faster, but only works for changes to non-init files.

.PARAMETER DryRun
    Run validation only. Show what would be deployed without making changes.

.EXAMPLE
    .\tools\deploy.ps1
    .\tools\deploy.ps1 -DryRun
    .\tools\deploy.ps1 -SkipRestart
    .\tools\deploy.ps1 -ReloadOnly
    .\tools\deploy.ps1 -Rollback
#>

[CmdletBinding()]
param(
    [switch]$Rollback,
    [switch]$SkipRestart,
    [switch]$ReloadOnly,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# --- Configuration ---
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ComponentDir = Join-Path $RepoRoot "custom_components" "climate_advisor"
$EnvFile = Join-Path $RepoRoot ".deploy.env"

# Default configuration
$Config = @{
    HA_HOST       = "homeassistant.local"
    HA_SSH_PORT   = "22"
    HA_SSH_USER   = "hassio"
    HA_SSH_KEY    = ""
    HA_CONFIG_PATH = "/config"
}

# Load .deploy.env overrides
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) {
            $parts = $line -split "=", 2
            if ($parts.Count -eq 2) {
                $Config[$parts[0].Trim()] = $parts[1].Trim()
            }
        }
    }
} else {
    Write-Host "WARNING: .deploy.env not found. Using defaults." -ForegroundColor Yellow
    Write-Host "   Copy .deploy.env.sample to .deploy.env and update with your values." -ForegroundColor Yellow
}

$SshTarget = "$($Config.HA_SSH_USER)@$($Config.HA_HOST)"
$SshArgs = @("-p", $Config.HA_SSH_PORT, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10")
# Only add -i flag if a specific SSH key is configured
if ($Config.HA_SSH_KEY -and $Config.HA_SSH_KEY -ne "") {
    $SshArgs = @("-i", $Config.HA_SSH_KEY) + $SshArgs
}
$RemotePath = "$($Config.HA_CONFIG_PATH)/custom_components/climate_advisor"
$BackupKeepCount = 5

function Write-Step {
    param([string]$Message)
    Write-Host "`n>> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "   [OK] $Message" -ForegroundColor Green
}

function Write-Fail {
    param([string]$Message)
    Write-Host "   [FAIL] $Message" -ForegroundColor Red
}

function Test-SshConnection {
    Write-Step "Testing SSH connection to $($Config.HA_HOST):$($Config.HA_SSH_PORT)"
    try {
        $result = ssh @SshArgs $SshTarget "echo ok" 2>&1
        if ($result -match "ok") {
            Write-Ok "SSH connection successful"
            return $true
        }
    }
    catch {}
    Write-Fail "Cannot connect via SSH. Check .deploy.env and SSH setup."
    Write-Host "   See docs/SSH-SETUP.md for configuration instructions." -ForegroundColor Yellow
    return $false
}

function Invoke-Validation {
    Write-Step "Running pre-deploy validation"
    $validateScript = Join-Path $RepoRoot "tools" "validate.py"
    & python $validateScript
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Validation failed. Fix errors before deploying."
        exit 1
    }
    Write-Ok "All validation checks passed"
}

function New-Backup {
    Write-Step "Creating backup on HA server"
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupPath = "$RemotePath.bak.$timestamp"

    # Check if the integration directory exists on the server
    $exists = ssh @SshArgs $SshTarget "test -d '$RemotePath' && echo yes || echo no" 2>&1
    if ($exists -match "yes") {
        ssh @SshArgs $SshTarget "cp -r '$RemotePath' '$backupPath'" 2>&1
        Write-Ok "Backup created: $backupPath"
    }
    else {
        Write-Host "   [INFO] No existing installation found. Skipping backup." -ForegroundColor Yellow
    }
}

function Remove-OldBackups {
    Write-Step "Pruning old backups (keeping last $BackupKeepCount)"
    $pruneCmd = @"
ls -1d ${RemotePath}.bak.* 2>/dev/null | sort -r | tail -n +$($BackupKeepCount + 1) | xargs rm -rf 2>/dev/null; echo done
"@
    ssh @SshArgs $SshTarget $pruneCmd 2>&1 | Out-Null
    Write-Ok "Old backups pruned"
}

function Deploy-Files {
    Write-Step "Deploying files to HA server"

    # Ensure remote directory exists
    ssh @SshArgs $SshTarget "mkdir -p '$RemotePath'" 2>&1

    # Copy all files from component directory
    $scpArgs = @("-P", $Config.HA_SSH_PORT, "-o", "StrictHostKeyChecking=no", "-r")
    if ($Config.HA_SSH_KEY -and $Config.HA_SSH_KEY -ne "") {
        $scpArgs = @("-i", $Config.HA_SSH_KEY) + $scpArgs
    }
    $sourceFiles = Join-Path $ComponentDir "*"

    scp @scpArgs $sourceFiles "${SshTarget}:${RemotePath}/" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "File copy failed"
        exit 1
    }

    # Verify file count on remote
    $remoteCount = ssh @SshArgs $SshTarget "ls -1 '$RemotePath' | wc -l" 2>&1
    $localCount = (Get-ChildItem $ComponentDir -File).Count
    Write-Ok "Deployed $localCount files to $RemotePath (remote has $($remoteCount.Trim()) files)"
}

function Restart-HomeAssistant {
    if ($SkipRestart) {
        Write-Host "`n   [INFO] Skipping restart (-SkipRestart). Remember to restart HA manually." -ForegroundColor Yellow
        return
    }

    if ($ReloadOnly) {
        Write-Step "Reloading integration via HA CLI"
        ssh @SshArgs $SshTarget "ha core restart" 2>&1
        # Note: HAOS doesn't have a native per-integration reload CLI command.
        # Using full restart as the reliable method.
        Write-Ok "HA core restart initiated"
    }
    else {
        Write-Step "Restarting Home Assistant core"
        ssh @SshArgs $SshTarget "ha core restart" 2>&1
        Write-Ok "HA core restart initiated"
    }

    Write-Step "Waiting 60 seconds for HA to restart"
    Start-Sleep -Seconds 60
}

function Test-PostDeploy {
    Write-Step "Checking HA logs for errors"
    $logOutput = ssh @SshArgs $SshTarget "grep -i 'climate_advisor' '$($Config.HA_CONFIG_PATH)/home-assistant.log' 2>/dev/null | tail -30" 2>&1

    if ($logOutput) {
        $errorLines = $logOutput | Where-Object { $_ -match "ERROR" }
        if ($errorLines) {
            Write-Fail "Errors found in HA logs:"
            $errorLines | ForEach-Object { Write-Host "   $_" -ForegroundColor Red }
            Write-Host "`n   Consider running: .\tools\deploy.ps1 -Rollback" -ForegroundColor Yellow
        }
        else {
            Write-Ok "No errors found in recent logs"
            # Show last few relevant lines
            $logOutput | Select-Object -Last 5 | ForEach-Object {
                Write-Host "   $_" -ForegroundColor Gray
            }
        }
    }
    else {
        Write-Host "   [INFO] No log entries found for climate_advisor yet." -ForegroundColor Yellow
    }
}

function Invoke-Rollback {
    Write-Step "Listing available backups"

    if (-not (Test-SshConnection)) { exit 1 }

    $backups = ssh @SshArgs $SshTarget "ls -1d ${RemotePath}.bak.* 2>/dev/null | sort -r" 2>&1
    if (-not $backups -or $backups -match "No such file") {
        Write-Fail "No backups found on server"
        exit 1
    }

    $backupList = $backups -split "`n" | Where-Object { $_.Trim() }
    Write-Host "   Available backups:" -ForegroundColor Yellow
    for ($i = 0; $i -lt $backupList.Count; $i++) {
        $name = Split-Path $backupList[$i] -Leaf
        Write-Host "   [$i] $name"
    }

    $latest = $backupList[0].Trim()
    Write-Step "Restoring from: $(Split-Path $latest -Leaf)"

    ssh @SshArgs $SshTarget "rm -rf '$RemotePath' && cp -r '$latest' '$RemotePath'" 2>&1
    Write-Ok "Backup restored"

    Write-Step "Restarting Home Assistant core"
    ssh @SshArgs $SshTarget "ha core restart" 2>&1
    Write-Ok "HA core restart initiated after rollback"

    Write-Step "Waiting 60 seconds for HA to restart"
    Start-Sleep -Seconds 60

    Test-PostDeploy
}

# --- Main ---
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Climate Advisor Deployment Tool" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Host: $($Config.HA_HOST):$($Config.HA_SSH_PORT)"
Write-Host "  Target: $RemotePath"

if ($Rollback) {
    Invoke-Rollback
    exit 0
}

# Step 1: Validate
Invoke-Validation

if ($DryRun) {
    Write-Host "`n============================================" -ForegroundColor Cyan
    Write-Host "  DRY RUN complete. No changes made." -ForegroundColor Yellow
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host "`nFiles that would be deployed:"
    Get-ChildItem $ComponentDir -File | ForEach-Object {
        Write-Host "   $($_.Name)" -ForegroundColor Gray
    }
    exit 0
}

# Step 2: Test SSH
if (-not (Test-SshConnection)) { exit 1 }

# Step 3: Backup
New-Backup
Remove-OldBackups

# Step 4: Deploy
Deploy-Files

# Step 5: Restart
Restart-HomeAssistant

# Step 6: Verify
if (-not $SkipRestart) {
    Test-PostDeploy
}

Write-Host "`n============================================" -ForegroundColor Green
Write-Host "  Deployment complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
