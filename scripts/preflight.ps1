#Requires -Version 7.2

<#
.SYNOPSIS
    Preflight checks before 'azd provision' (also the azd preprovision hook).
.DESCRIPTION
    Every failed check stops with one plain line: the problem and the fix.
    Checks: tools, login, region, providers, rbac, entra, models, quota, agents, definitions, email,
    costapi (warning only), bicep, softdelete. Skip one only if you know why it is a false alarm:
    $env:PREFLIGHT_SKIP = 'quota,bicep'
.EXAMPLE
    ./scripts/preflight.ps1
#>
[CmdletBinding()]
param(
    [switch]$Hook
)

. (Join-Path $PSScriptRoot 'lib/Common.ps1')
Set-Location $script:PmsRepoRoot

$MinAzVersion = '2.67.0'
$MinAzdVersion = '1.18.0'
$EuRegions = @('swedencentral', 'germanywestcentral', 'francecentral', 'italynorth', 'polandcentral', 'spaincentral',
    'westeurope', 'northeurope', 'austriaeast', 'belgiumcentral', 'denmarkeast')
$Providers = @('Microsoft.App', 'Microsoft.CognitiveServices', 'Microsoft.Search', 'Microsoft.DocumentDB', 'Microsoft.Storage',
    'Microsoft.KeyVault', 'Microsoft.ContainerRegistry', 'Microsoft.OperationalInsights', 'Microsoft.Insights',
    'Microsoft.AlertsManagement', 'Microsoft.Communication', 'Microsoft.ManagedIdentity', 'Microsoft.Consumption',
    'Microsoft.CostManagement', 'Microsoft.PolicyInsights')
# Directory roles that may create app registrations: Global Administrator, Application Administrator,
# Cloud Application Administrator, Application Developer (role template IDs).
$AppCreatorRoles = @('62e90394-69f5-4237-9190-012177145e10', '9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3',
    '158c047a-c907-4556-b7ef-446551a6b5f7', 'cf1c38e5-3621-4004-a7cb-879624dced7c')
$EmailPattern = '^[^@\s,]+@[^@\s,]+\.[^@\s,]+$'

if ($env:PREFLIGHT_DONE -eq '1') {
    Write-PmsInfo 'Preflight already passed in this run (PREFLIGHT_DONE=1); skipping.'
    exit 0
}

function Test-Skipped([string]$Check) {
    $skip = @("$env:PREFLIGHT_SKIP".Split(',') | ForEach-Object { $_.Trim() })
    if ($skip -contains $Check) {
        Write-PmsWarn "Check '$Check' skipped (PREFLIGHT_SKIP)."
        return $true
    }
    return $false
}

$mode = if ($Hook) { 'azd preprovision hook' } else { 'standalone' }
Write-PmsInfo "Preflight ($mode) for pms-agent-demo"

# --- tools -------------------------------------------------------------------------------------
Assert-PmsCommand 'az' 'Install the Azure CLI: https://aka.ms/installazurecli'
Assert-PmsCommand 'azd' 'Install the Azure Developer CLI: https://aka.ms/azd-install'
$python = Get-PmsPython
if (-not (Test-Skipped 'tools')) {
    $azInfo = Invoke-PmsAzJson version
    $azVersion = if ($azInfo) { "$($azInfo.'azure-cli')" } else { '' }
    if (-not $azVersion) { Stop-Pms 'Could not read the Azure CLI version.' 'Reinstall the Azure CLI: https://aka.ms/installazurecli' }
    if (-not (Test-PmsVersionAtLeast $azVersion $MinAzVersion)) { Stop-Pms "Azure CLI $azVersion is older than $MinAzVersion." 'Run: az upgrade' }
    $azdVersionText = (@(& azd version 2>$null) | ForEach-Object { "$_" }) -join ' '
    if (-not (Test-PmsVersionAtLeast $azdVersionText $MinAzdVersion)) {
        Stop-Pms "Azure Developer CLI '$azdVersionText' is older than $MinAzdVersion (or unreadable)." 'Upgrade azd: https://aka.ms/azd-install'
    }
    $pythonVersion = (& $python -c 'import platform; print(platform.python_version())') -join ''
    Write-PmsInfo "Tools: az $azVersion, $($azdVersionText.Trim()), Python $pythonVersion"
}

# --- login and subscription --------------------------------------------------------------------
$account = Invoke-PmsAzJson account show
if (-not $account) { Stop-Pms 'The Azure CLI is not signed in.' 'Run: az login' }
if (-not (Test-Skipped 'login')) {
    & azd auth login --check-status *> $null
    if ($LASTEXITCODE -ne 0) { Stop-Pms 'The Azure Developer CLI is not signed in.' 'Run: azd auth login' }
}
$currentSub = "$($account.id)"
$targetSub = Get-PmsValue 'AZURE_SUBSCRIPTION_ID'
if (-not $targetSub) {
    $targetSub = $currentSub
}
elseif ($targetSub -ne $currentSub) {
    Stop-Pms "The Azure CLI uses subscription $currentSub but the azd environment targets $targetSub." "Run: az account set --subscription $targetSub"
}
$userType = "$($account.user.type)"
Write-PmsInfo "Subscription: $targetSub (signed in as $userType)"

# --- region ------------------------------------------------------------------------------------
$location = (Get-PmsValue 'AZURE_LOCATION').ToLowerInvariant()
if (-not $location) { Stop-Pms 'AZURE_LOCATION is not set.' 'Run: azd env set AZURE_LOCATION swedencentral' }
if (-not (Test-Skipped 'region')) {
    if ($EuRegions -notcontains $location) {
        Stop-Pms "Region '$location' is not an allowed EU region for this demo." 'Run: azd env set AZURE_LOCATION swedencentral (fallback: germanywestcentral)'
    }
    $regions = @(Invoke-PmsAzJson account list-locations)
    if (-not ($regions | Where-Object { $_.name -eq $location })) {
        Stop-Pms "Region '$location' is not available to subscription $targetSub." 'Choose another EU region: azd env set AZURE_LOCATION germanywestcentral'
    }
    Write-PmsInfo "Region: $location (EU)"
}

# --- resource providers ------------------------------------------------------------------------
if (-not (Test-Skipped 'providers')) {
    $registered = @(Invoke-PmsAzJson provider list --query "[?registrationState=='Registered'].namespace")
    foreach ($namespace in $Providers) {
        if ($registered -notcontains $namespace) {
            Stop-Pms "Resource provider $namespace is not registered in subscription $targetSub." "Run: az provider register --namespace $namespace --wait"
        }
    }
    Write-PmsInfo 'Resource providers: all registered'
}

# --- Azure RBAC rights of the deploying identity -----------------------------------------------
if ($userType -eq 'user') {
    $principalId = Invoke-PmsAz ad signed-in-user show --query id -o tsv
}
else {
    $principalId = Invoke-PmsAz ad sp show --id "$($account.user.name)" --query id -o tsv
}
if (-not (Test-Skipped 'rbac')) {
    if (-not $principalId) {
        Stop-Pms 'Could not resolve the object ID of the signed-in identity.' 'Sign in again (az login) or, if you are sure you are Owner, set PREFLIGHT_SKIP=rbac.'
    }
    $assignments = @(Invoke-PmsAzJson role assignment list --assignee $principalId --scope "/subscriptions/$targetSub" --include-inherited --include-groups)
    $roleNames = @($assignments | ForEach-Object { "$($_.roleDefinitionName)" })
    $sufficient = ($roleNames -contains 'Owner') -or (($roleNames -contains 'Contributor') -and ($roleNames -contains 'User Access Administrator'))
    if (-not $sufficient) {
        Stop-Pms "Your identity needs Owner (or Contributor plus User Access Administrator) on subscription $targetSub for role assignments, the policy assignment and the budget." 'Ask a subscription owner to assign the role, then re-run.'
    }
    Write-PmsInfo 'Azure rights: sufficient (Owner, or Contributor plus User Access Administrator)'
}

# --- Entra app registration right (or bring your own) ------------------------------------------
$existingApp = Get-PmsValue 'ENTRA_EXISTING_APP_CLIENT_ID'
if (-not (Test-Skipped 'entra')) {
    if ($existingApp) {
        $found = (Invoke-PmsAz ad app show --id $existingApp --query appId -o tsv)
        if (-not $found) { $found = (Invoke-PmsAz ad sp show --id $existingApp --query appId -o tsv) }
        if (-not $found) {
            Stop-Pms "The app registration $existingApp (ENTRA_EXISTING_APP_CLIENT_ID) was not found in this tenant." 'Check the client ID with the admin who created it.'
        }
        Write-PmsInfo "Entra: using your existing app registration $existingApp (bring your own; see README)"
    }
    elseif ($userType -ne 'user') {
        Stop-Pms 'A service principal usually cannot create Entra app registrations.' 'Create one as described in the README and set ENTRA_EXISTING_APP_CLIENT_ID (repository variable for the pipeline).'
    }
    else {
        $policy = Invoke-PmsAzJson rest --method GET --url 'https://graph.microsoft.com/v1.0/policies/authorizationPolicy'
        $canCreate = $false
        if ($policy -and $policy.defaultUserRolePermissions) {
            $canCreate = [bool]$policy.defaultUserRolePermissions.allowedToCreateApps
        }
        if ($canCreate) {
            Write-PmsInfo 'Entra: users may create app registrations in this tenant'
        }
        else {
            $memberOf = Invoke-PmsAzJson rest --method GET --url 'https://graph.microsoft.com/v1.0/me/memberOf/microsoft.graph.directoryRole?$select=roleTemplateId'
            $templateIds = @()
            if ($memberOf -and $memberOf.value) {
                $templateIds = @($memberOf.value | ForEach-Object { "$($_.roleTemplateId)" })
            }
            $allowed = @($AppCreatorRoles | Where-Object { $templateIds -contains $_ }).Count -gt 0
            if (-not $allowed) {
                Stop-Pms 'You may not create Entra app registrations in this tenant.' 'Ask an admin to create one (README: bring your own app registration), then run: azd env set ENTRA_EXISTING_APP_CLIENT_ID <client-id>'
            }
            Write-PmsInfo 'Entra: your directory role may create app registrations'
        }
    }
}

# --- models, deployment type and quota ---------------------------------------------------------
$sku = Get-PmsValue 'MODEL_DEPLOYMENT_SKU'
if (-not $sku) { $sku = 'DataZoneStandard' }
if (@('DataZoneStandard', 'Standard') -cnotcontains $sku) {
    Stop-Pms "Model deployment type '$sku' is not allowed (EU processing only; never GlobalStandard)." 'Run: azd env set MODEL_DEPLOYMENT_SKU DataZoneStandard'
}
$fallback = (Get-PmsValue 'MODEL_FALLBACK_ALL_GENERAL').ToLowerInvariant() -eq 'true'
$resourceGroup = Get-PmsValue 'AZURE_RESOURCE_GROUP'
$foundryAccount = Get-PmsValue 'AZURE_FOUNDRY_ACCOUNT_NAME'
$catalog = $null
$usages = $null

foreach ($line in @(& $python (Join-Path $PSScriptRoot 'lib/preflight_data.py') models)) {
    $role, $modelName, $modelVersion, $deploymentName = "$line".Split("`t")
    if ($role -eq 'analysis' -and $fallback) {
        Write-PmsInfo "Model: $modelName not deployed (fallback MODEL_FALLBACK_ALL_GENERAL=true: gpt-6.1-sol for all agents)"
        continue
    }
    if ($role -eq 'analysis') {
        $capacityText = Get-PmsValue 'MODEL_ANALYSIS_CAPACITY'
        $noModelFix = 'Use the documented fallback: azd env set MODEL_FALLBACK_ALL_GENERAL true (or another EU region, e.g. germanywestcentral).'
    }
    else {
        $capacityText = Get-PmsValue 'MODEL_GENERAL_CAPACITY'
        $noModelFix = 'Choose another EU region where it is offered: azd env set AZURE_LOCATION germanywestcentral'
    }
    $capacity = if ($capacityText) { [double]$capacityText } else { 50 }
    if ($null -eq $catalog) {
        $catalog = @(Invoke-PmsAzJson cognitiveservices model list --location $location)
    }
    $skus = @($catalog | Where-Object { $_.model -and $_.model.name -eq $modelName -and $_.model.version -eq $modelVersion } |
            ForEach-Object { $_.model.skus } | Where-Object { $_ })
    $offered = @($skus | Where-Object { $_.name -eq $sku })
    if (-not (Test-Skipped 'models')) {
        if ($offered.Count -eq 0) {
            Stop-Pms "Model $modelName version $modelVersion is not offered as $sku in $location." $noModelFix
        }
        Write-PmsInfo "Model: $modelName $modelVersion offered as $sku in $location"
    }
    if (-not (Test-Skipped 'quota')) {
        $usageName = if ($offered.Count -gt 0 -and $offered[0].usageName) { "$($offered[0].usageName)" } else { "OpenAI.$sku.$modelName" }
        if ($null -eq $usages) {
            $usages = @(Invoke-PmsAzJson cognitiveservices usage list --location $location)
        }
        $usage = @($usages | Where-Object { $_.name -and $_.name.value -eq $usageName }) | Select-Object -First 1
        if (-not $usage) {
            Stop-Pms "No $sku quota ($usageName) exists for $modelName in $location." 'Request quota in the Foundry portal (Operate > Quota) or lower the capacity; then re-run.'
        }
        $own = 0
        if ($resourceGroup -and $foundryAccount) {
            $existing = Invoke-PmsAz cognitiveservices account deployment show --resource-group $resourceGroup --name $foundryAccount --deployment-name $deploymentName --query sku.capacity -o tsv
            if ($existing) { $own = [double]$existing }
        }
        $free = [double]$usage.limit - [double]$usage.currentValue + $own
        if ($free -lt $capacity) {
            Stop-Pms "Only $free (thousand tokens per minute) of $sku quota is free for $modelName in $location; $capacity is requested." "Lower the capacity (azd env set MODEL_$($role.ToUpperInvariant())_CAPACITY 20) or request more quota."
        }
        Write-PmsInfo "Quota: $modelName $capacity requested, limit $($usage.limit), in use $($usage.currentValue)"
    }
}

# --- Foundry Agent Service (Foundry projects) in the region ------------------------------------
if (-not (Test-Skipped 'agents')) {
    $provider = Invoke-PmsAzJson provider show --namespace Microsoft.CognitiveServices
    $projectType = $null
    if ($provider -and $provider.resourceTypes) {
        $projectType = @($provider.resourceTypes | Where-Object { $_.resourceType -eq 'accounts/projects' }) | Select-Object -First 1
    }
    if (-not $projectType) {
        Write-PmsWarn "Could not read the regions of Foundry projects; confirm Foundry Agent Service availability for $location in the documentation."
    }
    else {
        $normalized = @($projectType.locations | ForEach-Object { ("$_" -replace '\s', '').ToLowerInvariant() })
        if ($normalized -notcontains $location) {
            Stop-Pms "Foundry projects (Agent Service) are not offered in $location." 'Run: azd env set AZURE_LOCATION swedencentral'
        }
        Write-PmsInfo "Foundry Agent Service: Foundry projects are offered in $location"
    }
}

# --- built-in role and policy definitions used by the templates --------------------------------
if (-not (Test-Skipped 'definitions')) {
    foreach ($line in @(& $python (Join-Path $PSScriptRoot 'lib/preflight_data.py') definitions)) {
        $kind, $definitionId, $expectedName = "$line".Split("`t")
        if ($kind -eq 'role') {
            $actual = Invoke-PmsAz role definition list --name $definitionId --query '[0].roleName' -o tsv
        }
        else {
            $actual = Invoke-PmsAz policy definition show --name $definitionId --query displayName -o tsv
        }
        if (-not $actual) {
            Stop-Pms "Built-in $kind definition '$expectedName' ($definitionId) was not found." 'Update infra/roles.json with the current ID from the Azure built-in definitions list.'
        }
        if ($actual -ne $expectedName) {
            Write-PmsWarn "Built-in $kind $definitionId is now called '$actual' (expected '$expectedName')."
        }
    }
    Write-PmsInfo 'Built-in role and policy definitions: found'
}

# --- notification addresses ---------------------------------------------------------------------
$deployerEmail = Get-PmsValue 'DEPLOYER_EMAIL'
if (-not $deployerEmail -and $userType -eq 'user') {
    $detected = Invoke-PmsAz ad signed-in-user show --query mail -o tsv
    if ($detected -match $EmailPattern) {
        $deployerEmail = $detected
        [void](Set-PmsAzdValue 'DEPLOYER_EMAIL' $deployerEmail)
        Write-PmsInfo "DEPLOYER_EMAIL set to your account address $deployerEmail (budget and alert notifications)"
    }
}
if (-not (Test-Skipped 'email')) {
    if (-not $deployerEmail) { Stop-Pms 'DEPLOYER_EMAIL is not set (budget and alert notifications).' 'Run: azd env set DEPLOYER_EMAIL you@example.com' }
    if ($deployerEmail -notmatch $EmailPattern) { Stop-Pms "DEPLOYER_EMAIL '$deployerEmail' is not an e-mail address." 'Run: azd env set DEPLOYER_EMAIL you@example.com' }
    $recipients = Get-PmsValue 'ALERT_RECIPIENTS'
    if ($recipients) {
        foreach ($recipient in $recipients.Split(',')) {
            $address = $recipient.Trim()
            if ($address -notmatch $EmailPattern) {
                Stop-Pms "ALERT_RECIPIENTS contains '$address', which is not an e-mail address." 'Run: azd env set ALERT_RECIPIENTS demo1@example.com,demo2@example.com (demo mailboxes only)'
            }
        }
    }
    $criticalTo = if ($recipients) { $recipients } else { $deployerEmail }
    Write-PmsInfo "Notifications: budget and alerts to $deployerEmail; critical-case e-mails to $criticalTo"
}

# --- Cost Management API (warning only, OI-030) --------------------------------------------------
if (-not (Test-Skipped 'costapi')) {
    $costQuery = '{"type":"ActualCost","timeframe":"MonthToDate","dataset":{"granularity":"None","aggregation":{"totalCost":{"name":"Cost","function":"Sum"}}}}'
    $costBodyFile = New-TemporaryFile
    try {
        Set-Content -Path $costBodyFile -Value $costQuery -Encoding utf8NoBOM
        & az rest --method POST --url "https://management.azure.com/subscriptions/$targetSub/providers/Microsoft.CostManagement/query?api-version=2023-11-01" --body "@$costBodyFile" -o none 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-PmsInfo 'Cost Management API: available'
        }
        else {
            Write-PmsWarn 'The Cost Management API did not answer for this subscription (offer type or rights). The cost view will show token-based model costs only, with a notice (OI-030).'
        }
    }
    finally {
        Remove-Item -Path $costBodyFile -ErrorAction SilentlyContinue
    }
}

# --- Bicep compiles (also restores the Microsoft Graph extension) --------------------------------
if (-not (Test-Skipped 'bicep')) {
    $bicepOutput = & az bicep build --file infra/main.bicep --stdout 2>&1
    if ($LASTEXITCODE -ne 0) {
        $firstError = @($bicepOutput | ForEach-Object { "$_" } | Where-Object { $_ -match 'error' }) | Select-Object -First 1
        if (-not $firstError) { $firstError = "see 'az bicep build --file infra/main.bicep'" }
        Stop-Pms "infra/main.bicep does not compile: $firstError." 'Run: az bicep upgrade; if the Graph extension tag in infra/bicepconfig.json is unknown, set the current one (see docs/handover).'
    }
    Write-PmsInfo 'Bicep: infra/main.bicep compiles'
}

# --- names reserved by soft-deleted resources of an earlier deployment ---------------------------
if (-not (Test-Skipped 'softdelete') -and $resourceGroup) {
    $groupExists = Invoke-PmsAz group exists --name $resourceGroup
    if ($groupExists -eq 'false') {
        $vault = Get-PmsValue 'AZURE_KEY_VAULT_NAME'
        if ($vault -and (Invoke-PmsAz keyvault show-deleted --name $vault --query name -o tsv)) {
            Stop-Pms "Key Vault $vault of an earlier deployment is soft-deleted with purge protection, so its name is reserved for up to 7 days." "Use a new environment (azd env new <name>) or recover it: az keyvault recover --name $vault"
        }
        if ($foundryAccount) {
            $deleted = @(Invoke-PmsAzJson cognitiveservices account list-deleted) | Where-Object { $_.name -eq $foundryAccount }
            if ($deleted) {
                Stop-Pms "Foundry account $foundryAccount of an earlier deployment is soft-deleted." "Run: az cognitiveservices account purge --name $foundryAccount --resource-group $resourceGroup --location $location"
            }
        }
    }
}

Write-PmsInfo 'Preflight passed.'
exit 0
