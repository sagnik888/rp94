param(
    [string]$Url = "http://127.0.0.1:8001/",
    [int]$Port = 8001,
    [int]$TimeoutSeconds = 120
)

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$configUrl = $Url.TrimEnd('/') + "/api/client_config"
$apiUrl = $Url.TrimEnd('/') + "/api/state"
$token = ""

while ((Get-Date) -lt $deadline) {
    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        try {
            if (-not $token) {
                $config = Invoke-RestMethod -Uri $configUrl -TimeoutSec 3
                if ($config.dashboard_token) {
                    $token = $config.dashboard_token
                }
            }
            if ($token) {
                $headers = @{"X-Dashboard-Token" = $token}
                $state = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 3
                
                $instrumentCount = 0
                if ($state.instruments) {
                    $instrumentCount = @($state.instruments.PSObject.Properties).Count
                }
                $scanCount = 0
                if ($state.scan_count) {
                    $scanCount = [int]$state.scan_count
                } elseif ($state.dashboard_heartbeat -and $state.dashboard_heartbeat.scan_count) {
                    $scanCount = [int]$state.dashboard_heartbeat.scan_count
                }
                if ($instrumentCount -gt 0 -or $scanCount -gt 0) {
                    Start-Process $Url
                    exit 0
                }
            }
        } catch {
            # Server is listening but the app is still booting or unauthorized.
        }
    }
    Start-Sleep -Milliseconds 500
}

# Fall back to opening the dashboard if startup takes unusually long.
Start-Process $Url
exit 0
