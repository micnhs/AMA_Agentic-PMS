#Requires -Version 7.2

<#
.SYNOPSIS
    Creates the demo's Microsoft Entra app registration outside the deployment (bring your own).
.DESCRIPTION
    For a deployment that may not create the app registration itself (the preflight or the deployment
    stops with a message about Microsoft Graph). Run it yourself if the preflight says that you may
    create app registrations, or ask someone who may (for example an Entra admin) to run it in your
    tenant with -AssignTo and -Owner set to your account.

    It creates a single-tenant registration with the settings of infra/modules/entra-app.bicep: the
    app roles PMS.Viewer, PMS.Analyst, PMS.Regulatory, PMS.MedicalReviewer and PMS.Operator, the scope
    user_impersonation with Azure CLI pre-authorised, ID tokens and access tokens v2, and its
    enterprise application with 'assignment required'. It assigns the app roles in -Roles (default:
    DEPLOYER_APP_ROLES, else PMS.Operator) and stores the client ID in the selected azd environment
    (ENTRA_EXISTING_APP_CLIENT_ID). Running it again completes the same registration. After
    provisioning, the postprovision hook adds the Web redirect URI.
.EXAMPLE
    ./scripts/create-app-registration.ps1
.EXAMPLE
    ./scripts/create-app-registration.ps1 -EnvironmentName pms-demo -AssignTo user@contoso.com -Owner user@contoso.com
#>
param(
    [string]$EnvironmentName = '',
    [string]$AssignTo = '',
    [string]$Owner = '',
    [string]$Roles = ''
)

. (Join-Path $PSScriptRoot 'lib/Common.ps1')
Set-Location $script:PmsRepoRoot

Assert-PmsCommand 'az' 'Install the Azure CLI: https://aka.ms/installazurecli'
$python = Get-PmsPython
$selected = Get-PmsValue 'AZURE_ENV_NAME'
if (-not $EnvironmentName) { $EnvironmentName = $selected }
if (-not $EnvironmentName) {
    Stop-Pms 'No azd environment is selected.' 'Run: azd env select pms-demo, or pass -EnvironmentName pms-demo'
}
$inSelected = ($EnvironmentName -eq $selected)
if (-not $Roles) { $Roles = Get-PmsValue 'DEPLOYER_APP_ROLES' }
if (-not $Roles) { $Roles = 'PMS.Operator' }
if (-not $AssignTo) { $AssignTo = 'me' }

$arguments = @((Join-Path $PSScriptRoot 'lib/entra_app.py'), 'create', '--shell', 'ps1',
    '--environment', $EnvironmentName, '--roles', $Roles, '--assign-to', $AssignTo)
if ($inSelected) {
    # Same environment again: complete the registration of the first run instead of creating a second one.
    $existing = Get-PmsValue 'ENTRA_EXISTING_APP_CLIENT_ID'
    if ($existing) { $arguments += @('--client-id', $existing) }
    $redirectUri = Get-PmsValue 'WEB_REDIRECT_URI'
    if ($redirectUri) { $arguments += @('--redirect-uri', $redirectUri) }
}
if ($Owner) { $arguments += @('--owner', $Owner) }

$token = Invoke-PmsAz account get-access-token --resource-type ms-graph --query accessToken -o tsv
if (-not $token) { Stop-Pms 'Could not get a Microsoft Graph token from the Azure CLI.' 'Run: az login' }
$resultFile = New-TemporaryFile
$env:PMS_GRAPH_TOKEN = $token
try {
    & $python @arguments --result $resultFile.FullName
    $code = $LASTEXITCODE
    $result = $null
    if ($code -eq 0) { $result = Get-Content -Raw -Path $resultFile.FullName | ConvertFrom-Json }
}
finally {
    Remove-Item Env:PMS_GRAPH_TOKEN -ErrorAction SilentlyContinue
    Remove-Item -Path $resultFile.FullName -ErrorAction SilentlyContinue
}
if ($code -ne 0) { exit $code }

$clientId = "$($result.clientId)"
if ($inSelected -and (Set-PmsAzdValue 'ENTRA_EXISTING_APP_CLIENT_ID' $clientId)) {
    Write-PmsInfo "Stored ENTRA_EXISTING_APP_CLIENT_ID=$clientId in the azd environment '$EnvironmentName'."
    Write-PmsInfo "Next: ./scripts/deploy.ps1 -EnvironmentName $EnvironmentName (it reuses a resource group left by an earlier attempt)."
}
else {
    Write-PmsInfo "Give the deployer this command for the azd environment '$EnvironmentName':"
    Write-PmsInfo "  azd env set ENTRA_EXISTING_APP_CLIENT_ID $clientId"
}
exit 0
