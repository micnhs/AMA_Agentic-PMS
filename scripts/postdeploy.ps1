<#
.SYNOPSIS
    azd postdeploy hook: waits until GET /healthz answers, builds the Azure AI Search knowledge index and
    publishes changed Foundry prompt agents (both idempotent), then prints the next steps.
.DESCRIPTION
    AUTO_SEED=true also loads the demo data through the API (needs the PMS.Operator role).
#>

. (Join-Path $PSScriptRoot 'lib/Common.ps1')
Set-Location $script:PmsRepoRoot

$webUrl = Get-PmsValue 'WEB_URL'
if (-not $webUrl) {
    Write-PmsWarn "WEB_URL is unknown; run 'azd provision' first."
    exit 0
}

Write-PmsInfo "Waiting for $webUrl/healthz (up to 5 minutes) ..."
$healthy = $false
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri "$webUrl/healthz" -TimeoutSec 10 -UseBasicParsing
        if ($response.StatusCode -eq 200) {
            $healthy = $true
            break
        }
    }
    catch {
        Write-Verbose "Health check not ready yet: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 10
}
if ($healthy) {
    Write-PmsInfo "Web app is healthy: $webUrl"
}
else {
    Write-PmsWarn "No healthy answer from $webUrl/healthz yet. Check: az containerapp logs show --name $(Get-PmsValue 'WEB_APP_NAME') --resource-group $(Get-PmsValue 'AZURE_RESOURCE_GROUP')"
}

# Knowledge index and Foundry prompt agents. Both steps are idempotent: only changed index documents are uploaded
# and an agent gets a new version only when its prompt/specification changed (versions are kept in Cosmos DB).
# The worker container runs the same steps on every start with its managed identity; here they run with your
# sign-in when the Azure Python packages are installed (pip install -r requirements.txt).
$py = Get-PmsPython
& $py -c 'import azure.identity, azure.cosmos, azure.search.documents, agent_framework' *> $null
if ($LASTEXITCODE -eq 0) {
    $names = @('APP_ENV', 'AGENT_BACKEND', 'LOCAL_TEST_USERS', 'WORKER_MODE', 'FOUNDRY_PROJECT_ENDPOINT',
        'MODEL_DEPLOYMENT_ANALYSIS', 'MODEL_DEPLOYMENT_GENERAL', 'AZURE_SEARCH_ENDPOINT', 'AZURE_SEARCH_INDEX',
        'COSMOS_ENDPOINT', 'COSMOS_DATABASE')
    foreach ($name in $names) {
        $value = Get-PmsValue $name
        if ($value) {
            [Environment]::SetEnvironmentVariable($name, $value)
        }
    }
    Add-PmsSourcePath
    $workerLogs = "az containerapp logs show --name $(Get-PmsValue 'WORKER_APP_NAME') --resource-group $(Get-PmsValue 'AZURE_RESOURCE_GROUP')"
    Write-PmsInfo 'Knowledge index: python -m pms seed --index-only'
    & $py -m pms seed --index-only
    if ($LASTEXITCODE -ne 0) {
        Write-PmsWarn "Index not updated from here (role 'Search Index Data Contributor' missing?). The worker container does it on start: $workerLogs"
    }
    Write-PmsInfo 'Foundry prompt agents: python -m pms publish-agents'
    & $py -m pms publish-agents
    if ($LASTEXITCODE -ne 0) {
        Write-PmsWarn "Agents not published from here. The worker container publishes changed agents on start: $workerLogs"
    }
}
else {
    Write-PmsInfo 'Knowledge index and Foundry agents: done by the worker container on start (managed identity). To run them here: pip install -r requirements.txt'
}

if ((Get-PmsValue 'AUTO_SEED') -eq 'true') {
    & (Join-Path $PSScriptRoot 'seed.ps1') -Azure
    if ($LASTEXITCODE -ne 0) {
        Write-PmsWarn 'Seeding failed; run ./scripts/seed.ps1 -Azure after checking your app role.'
    }
}

Write-PmsInfo 'Next steps:'
Write-PmsInfo '  1. Assign demo users their app roles (Entra admin center > Enterprise applications > PMS agent demo).'
Write-PmsInfo '  2. Load demo data: ./scripts/seed.ps1 -Azure'
Write-PmsInfo '  3. Smoke test: ./scripts/smoke-test.ps1 -Part 1'
Write-PmsInfo '  4. When done: ./scripts/teardown.ps1'
exit 0
