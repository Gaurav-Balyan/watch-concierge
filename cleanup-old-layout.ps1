# Run ONCE, from the project folder, BEFORE unzipping the new layout over it.
# Removes the files that moved into packages (concierge/, servers/, common/, tools/, baselines/, docs/).
# Your .env, .webos_key, .login.json, .state\, logs\, db\ and keycloak\ are NOT touched.
$old = @(
  "agent.py","graph.py","router.py","workflows.py","skills.py","multi_agent.py","routed.py",
  "config.py","auth.py","login.py","tracing.py","resilience.py","logsetup.py",
  "catalog_mcp.py","lookup.py","device_mcp.py","tv.py","taste_mcp.py",
  "cli.py","experiments.py","profile_probe.py","mcp_probe.py",
  "PLAN.md","HANDOFF.md","SETUP.md","FLOWS.md","Watch Concierge Flow.html",
  "agent_inline_tools.py","concierge.py"
)
foreach ($f in $old) { if (Test-Path $f) { Remove-Item $f; Write-Host "removed $f" } }
foreach ($d in "services","skills","__pycache__") { if (Test-Path $d) { Remove-Item -Recurse $d; Write-Host "removed $d\" } }
Write-Host "Done. Now unzip watch-concierge-restructured.zip over this folder (overwrite), then: python -m pytest tests"
