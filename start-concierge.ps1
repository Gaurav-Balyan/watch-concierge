# Starts the Household Watch Concierge in full mode (see SETUP.md, section 2).
# Run it from the project folder, with the environment already active:
#   conda activate watch-concierge
#   .\start-concierge.ps1
# 1. starts the Docker containers keycloak and household-db
# 2. waits until Keycloak answers
# 3. opens one PowerShell window per MCP server (catalog, device, taste)
# 4. shows who is logged in
# Stop: close the three server windows, then  docker stop keycloak household-db

# "Continue", not "Stop": in Windows PowerShell 5.1, a native command (docker) writing to stderr
# would otherwise abort the script. We check $LASTEXITCODE ourselves instead.
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root

# ---- 1. Docker containers
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker is not running. Start Docker Desktop, wait until it is ready, then run this again." -ForegroundColor Red
    exit 1
}
$existing = docker ps -a --format "{{.Names}}"
foreach ($name in "keycloak", "household-db") {
    if ($existing -notcontains $name) {
        Write-Host "Container '$name' does not exist yet. Create it once with the command in SETUP.md, section 1.5." -ForegroundColor Red
        exit 1
    }
}
Write-Host "Starting containers keycloak and household-db..."
docker start keycloak household-db | Out-Null

# ---- 2. Wait for Keycloak (up to 90 s)
$url = "http://localhost:8080/realms/household/.well-known/openid-configuration"
$ready = $false
for ($i = 0; $i -lt 45; $i++) {
    try {
        Invoke-RestMethod $url -TimeoutSec 2 | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $ready) {
    Write-Host "Keycloak did not answer within 90 s. Check: docker logs keycloak --tail 20" -ForegroundColor Red
    exit 1
}
Write-Host "Keycloak is ready."

# ---- 3. One window per server (each activates the conda env itself)
$servers = @(
    @{ Title = "catalog-mcp :8001"; Script = "catalog_mcp.py" },
    @{ Title = "device-mcp :8002";  Script = "device_mcp.py" },
    @{ Title = "taste-mcp :8003";   Script = "taste_mcp.py" }
)
foreach ($s in $servers) {
    $command = "`$host.UI.RawUI.WindowTitle = '$($s.Title)'; conda activate watch-concierge; python $($s.Script) --http"
    Start-Process powershell -WorkingDirectory $root -ArgumentList "-NoExit", "-Command", $command
    Start-Sleep -Seconds 1
}
Write-Host "Opened three server windows. The device window's first line says LIVE or DRY RUN."

# ---- 4. Who is logged in (uses the environment active in THIS window)
python login.py --status
Write-Host ""
Write-Host "Next (in this window):  python agent.py --tools      then e.g.  python agent.py ""play The Friend 2025"""
Write-Host "Not logged in?          python login.py"
Write-Host "Agent still using stdio? Add the three *_MCP_URL lines to .env (SETUP.md, section 1.3)."
