# Clean unreadable pytest-tmp-base left by orphaned pytest processes.
# Run from repo root:
#   powershell -ExecutionPolicy Bypass -File scripts/cleanup_pytest_tmp_base.ps1
# Optional force resets ACLs before retrying. It never stops Python processes:
#   powershell -ExecutionPolicy Bypass -File scripts/cleanup_pytest_tmp_base.ps1 -Force

param([switch]$Force)

$ErrorActionPreference = "Continue"
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$target = [System.IO.Path]::GetFullPath((Join-Path $root "pytest-tmp-base"))
$rootPrefix = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar

if (-not $target.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Host "FAIL: refusing cleanup outside workspace: $target"
    exit 2
}

if (-not (Test-Path -LiteralPath $target)) {
    Write-Host "OK: pytest-tmp-base does not exist"
    exit 0
}

Write-Host "Found: $target"
if ($Force) {
    Write-Host "Force: resetting pytest-tmp-base ownership/ACL only (no processes will be stopped)..."
    try {
        takeown /F $target /R /D Y 2>$null | Out-Null
        icacls $target /grant "${env:USERNAME}:(OI)(CI)F" /T /C 2>$null | Out-Null
    } catch {
        Write-Host "ACL reset skipped: $_"
    }
}

try {
    Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
    Write-Host "OK: removed pytest-tmp-base"
    exit 0
} catch {
    Write-Host "WARN: could not remove fully: $_"
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $leaf = "pytest-tmp-base.quarantine_$stamp"
    try {
        Rename-Item -LiteralPath $target -NewName $leaf -ErrorAction Stop
        Write-Host "OK: quarantined as $leaf"
        exit 0
    } catch {
        Write-Host "FAIL: still locked. Stop only the pytest process that created this directory, then retry."
        Write-Host "The script intentionally never stops the trading engine or unrelated Python processes."
        Write-Host "  powershell -ExecutionPolicy Bypass -File scripts/cleanup_pytest_tmp_base.ps1 -Force"
        exit 1
    }
}
