<#
    Pre-flight port check for the UT1 launcher.

    Detects a stale instance already listening on the dashboard port, shows it,
    and (with confirmation) stops it so the new instance can bind cleanly.

    Exit codes:
      0  -> port is free (either it always was, or the stale instance was stopped)
      1  -> user declined to stop the existing instance (abort launch)
      2  -> stop attempted but the port is still in use (abort launch)
#>
param([int]$Port = 8001)

$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Host "[INFO] Port $Port is free."
    exit 0
}

$procIds = $conns.OwningProcess | Sort-Object -Unique
Write-Host "[WARNING] Port $Port is already in use (likely a previous UT1 instance still running):" -ForegroundColor Yellow
foreach ($procId in $procIds) {
    try {
        $p = Get-Process -Id $procId -ErrorAction Stop
        Write-Host ("           PID {0}: {1}  |  started {2}  |  {3}" -f $p.Id, $p.ProcessName, $p.StartTime, $p.Path)
    } catch {
        Write-Host ("           PID {0}: (process info unavailable)" -f $procId)
    }
}

$answer = Read-Host "Stop the existing instance and continue? (Y/N)"
if ($answer -notmatch '^[Yy]') {
    Write-Host "[INFO] Leaving the existing instance running. Aborting launch."
    exit 1
}

foreach ($procId in $procIds) {
    try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Write-Host "[INFO] Stopped PID $procId."
    } catch {
        Write-Host "[ERROR] Failed to stop PID $procId : $($_.Exception.Message)" -ForegroundColor Red
    }
}

Start-Sleep -Milliseconds 1500
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($still) {
    $stillPids = ($still.OwningProcess | Sort-Object -Unique) -join ', '
    Write-Host "[ERROR] Port $Port is still in use by PID(s): $stillPids. Close it manually and retry." -ForegroundColor Red
    exit 2
}

Write-Host "[INFO] Port $Port is now free."
exit 0
