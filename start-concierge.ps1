# Starts the Household Watch Concierge in full mode (see SETUP.md, section 2).
# Run it from the project folder, with the environment already active:
#   conda activate watch-concierge
#   .\start-concierge.ps1
# 1. starts the Docker containers keycloak and household-db
# 2. waits until Keycloak answers
# 3. opens one PowerShell window per MCP server (catalog, device, taste)
#    + the catalog-agent (A2A, idea 2) window, but ONLY when CATALOG_AGENT_URL is set in .env
# 4. shows who is logged in
# Stop: close the server windows, then  docker stop keycloak household-db jaeger

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
# Jaeger (traces) is optional: start it when it exists, otherwise say how to create it.
if ($existing -contains "jaeger") {
    docker start jaeger | Out-Null
    Write-Host "Started jaeger: traces at http://localhost:16686"
} else {
    Write-Host "No 'jaeger' container (optional). To see traces, create it once (SETUP.md 1.5), or set TRACING=print in .env." -ForegroundColor Yellow
}

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
    @{ Title = "catalog-mcp :8001"; Module = "servers.catalog.server" },
    @{ Title = "device-mcp :8002";  Module = "servers.device.server" },
    @{ Title = "taste-mcp :8003";   Module = "servers.taste.server" }
)
foreach ($s in $servers) {
    $command = "`$host.UI.RawUI.WindowTitle = '$($s.Title)'; conda activate watch-concierge; python -m $($s.Module) --http"
    Start-Process powershell -WorkingDirectory $root -ArgumentList "-NoExit", "-Command", $command
    Start-Sleep -Seconds 1
}
Write-Host "Opened three server windows. The device window's first line says LIVE or DRY RUN."

# The catalog-agent (A2A server, idea 2): only needed when the Supervisor is set to call it over the network.
# Not set = the Catalog agent runs inside the concierge, as before, and no window is opened.
$agentUrl = $env:CATALOG_AGENT_URL
if (-not $agentUrl -and (Test-Path "$root\.env")) {
    $line = Select-String -Path "$root\.env" -Pattern '^\s*CATALOG_AGENT_URL\s*=\s*(\S+)' | Select-Object -First 1
    if ($line) { $agentUrl = $line.Matches[0].Groups[1].Value.Trim('"', "'") }
}
if ($agentUrl) {
    $command = "`$host.UI.RawUI.WindowTitle = 'catalog-agent (A2A) :8004'; conda activate watch-concierge; python -m servers.catalog_agent.server"
    Start-Process powershell -WorkingDirectory $root -ArgumentList "-NoExit", "-Command", $command
    Write-Host "Opened the catalog-agent window (A2A, $agentUrl): CATALOG_AGENT_URL is set."
} else {
    Write-Host "catalog-agent not started: CATALOG_AGENT_URL is not set (the Catalog agent runs in-process)."
}

# ---- 4. Who is logged in (uses the environment active in THIS window)
python -m common.login --status
Write-Host ""
Write-Host "Next (in this window):  python -m concierge.graph ""play The Friend 2025""      (all commands: COMMANDS.ps1)"
Write-Host "Not logged in?          python -m common.login"
Write-Host "Agent still using stdio? Add the three *_MCP_URL lines to .env (SETUP.md, section 1.3)."