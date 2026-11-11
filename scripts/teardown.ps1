#Requires -Version 7.2

<#
.SYNOPSIS
    Deletes every Azure resource of the selected azd environment ('azd down --purge') after an explicit confirmation.
.DESCRIPTION
    Afterwards it removes what lives outside the resource group: the custom e-mail sender role and the
    Entra app registration created by the deployment (never a bring-your-own registration).
.EXAMPLE
    ./scripts/teardown.ps1
.EXAMPLE
    ./scripts/teardown.ps1 -ConfirmEnvironment pms-demo
#>
param(
    [string]$ConfirmEnvironment = ''
)

. (Join-Path $PSScriptRoot 'lib/Common.ps1')
Set-Location $script:PmsRepoRoot

Assert-PmsCommand 'azd' 'Install the Azure Developer CLI: https://aka.ms/azd-install'
Assert-PmsCommand 'az' 'Install the Azure CLI: https://aka.ms/installazurecli'

$envName = Get-PmsValue 'AZURE_ENV_NAME'
if (-not $envName) { Stop-Pms 'No azd environment is selected.' 'Run: azd env select <env-name>' }
$resourceGroup = Get-PmsValue 'AZURE_RESOURCE_GROUP'
$subscription = Get-PmsValue 'AZURE_SUBSCRIPTION_ID'
$appCreated = Get-PmsValue 'ENTRA_APP_CREATED'
$appClientId = Get-PmsValue 'ENTRA_APP_CLIENT_ID'
$roleId = Get-PmsValue 'AZURE_ACS_EMAIL_ROLE_ID'
$vault = Get-PmsValue 'AZURE_KEY_VAULT_NAME'
$groupName = if ($resourceGroup) { $resourceGroup } else { "rg-$envName" }

Write-PmsInfo "This permanently deletes environment '$envName':"
Write-PmsInfo "  - resource group '$groupName' with all resources and data (azd down --purge)"
if ($roleId) { Write-PmsInfo "  - the custom role 'PMS demo ACS e-mail sender' ($roleId)" }
if ($appCreated -eq 'true' -and $appClientId) { Write-PmsInfo "  - the Entra app registration $appClientId created by this deployment" }

$confirmation = $ConfirmEnvironment
if (-not $confirmation) {
    if ([Console]::IsInputRedirected) {
        Stop-Pms 'No confirmation given in a non-interactive shell; nothing was deleted.' "Run: ./scripts/teardown.ps1 -ConfirmEnvironment $envName"
    }
    $confirmation = Read-Host "[pms] Type the environment name '$envName' to confirm"
}
if ($confirmation -cne $envName) {
    Stop-Pms "The confirmation did not match '$envName'; nothing was deleted." 'Re-run and type the exact environment name.'
}

& azd down --purge --force
$downStatus = $LASTEXITCODE
if ($resourceGroup -and ((Invoke-PmsAz group exists --name $resourceGroup) -ne 'false')) {
    Stop-Pms "Resource group $resourceGroup still exists (azd down exit code $downStatus)." 'Check the error above and re-run ./scripts/teardown.ps1'
}
if ($downStatus -ne 0) {
    Write-PmsWarn "azd down reported exit code $downStatus, but the resource group is gone (a purge step may have been refused)."
}

if ($roleId -and $subscription) {
    & az role definition delete --name $roleId --scope "/subscriptions/$subscription" -o none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-PmsInfo 'Deleted the custom e-mail sender role.'
    }
    else {
        Write-PmsWarn "Could not delete custom role $roleId; delete it in the portal (Subscription > Access control > Roles)."
    }
}

if ($appCreated -eq 'true' -and $appClientId) {
    $appObjectId = Invoke-PmsAz ad app show --id $appClientId --query id -o tsv
    & az ad app delete --id $appClientId 2>$null
    if ($appObjectId -and $LASTEXITCODE -eq 0) {
        & az rest --method DELETE --url "https://graph.microsoft.com/v1.0/directory/deletedItems/$appObjectId" -o none 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-PmsWarn 'The app registration is in the Entra recycle bin for 30 days (could not delete it permanently).'
        }
        Write-PmsInfo "Deleted the Entra app registration $appClientId."
    }
    else {
        Write-PmsWarn "Could not delete the Entra app registration $appClientId; delete it in the Entra admin center."
    }
}

if ($vault) {
    Write-PmsInfo "Note: Key Vault $vault has purge protection and stays soft-deleted for 7 days; redeploy within that time with a new environment name."
}
Write-PmsInfo 'Teardown finished.'
exit 0
