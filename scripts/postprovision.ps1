<#
.SYNOPSIS
    azd postprovision hook. Best effort: it never fails the deployment and prints the fix instead.
.DESCRIPTION
    1. App registration created by Bicep: pre-authorise Azure CLI for the API scope, so
       'az account get-access-token' works for scripts/seed.ps1 and scripts/smoke-test.ps1.
    2. Bring-your-own app registration: print the settings it must have.
    3. Assign the signed-in user the app roles in DEPLOYER_APP_ROLES (default PMS.Operator).
#>

. (Join-Path $PSScriptRoot 'lib/Common.ps1')
Set-Location $script:PmsRepoRoot

$graph = 'https://graph.microsoft.com/v1.0'
$azureCliAppId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'
$python = Get-PmsPython
$clientId = Get-PmsValue 'ENTRA_APP_CLIENT_ID'
$created = Get-PmsValue 'ENTRA_APP_CREATED'
$redirectUri = Get-PmsValue 'WEB_REDIRECT_URI'
$apiScope = Get-PmsValue 'ENTRA_API_SCOPE'
$userType = Invoke-PmsAz account show --query user.type -o tsv

if (-not $clientId) {
    Write-PmsWarn 'ENTRA_APP_CLIENT_ID is empty; skipping the Entra follow-up.'
    exit 0
}

# Sends a Microsoft Graph request with a JSON body from a file (avoids shell quoting differences).
function Invoke-PmsGraphWrite([string]$Method, [string]$Url, [string]$Body) {
    $bodyFile = New-TemporaryFile
    try {
        Set-Content -Path $bodyFile -Value $Body -Encoding utf8NoBOM
        & az rest --method $Method --url $Url --headers 'Content-Type=application/json' --body "@$bodyFile" -o none 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        Remove-Item -Path $bodyFile -ErrorAction SilentlyContinue
    }
}

if ($created -eq 'true') {
    $appObjectId = Invoke-PmsAz ad app show --id $clientId --query id -o tsv
    $apiJson = Invoke-PmsAz ad app show --id $clientId --query api -o json
    if (-not $appObjectId -or -not $apiJson) {
        Write-PmsWarn "Could not read app registration $clientId. Fix: pre-authorise Azure CLI ($azureCliAppId) for user_impersonation under 'Expose an API'."
    }
    else {
        $patchBody = ($apiJson | & $python (Join-Path $PSScriptRoot 'lib/entra_api_patch.py') 2>$null) -join ''
        if (-not $patchBody) {
            Write-PmsInfo "Azure CLI is already pre-authorised for $apiScope."
        }
        elseif (Invoke-PmsGraphWrite 'PATCH' "$graph/applications/$appObjectId" $patchBody) {
            Write-PmsInfo "Pre-authorised Azure CLI for $apiScope (tokens for seed and smoke test)."
        }
        else {
            Write-PmsWarn "Could not pre-authorise Azure CLI. Fix: Entra admin center > App registrations > $clientId > Expose an API > add client application $azureCliAppId for user_impersonation."
        }
    }
}
else {
    Write-PmsInfo "Bring-your-own app registration ${clientId}: make sure it has"
    Write-PmsInfo "  - Web redirect URI: $redirectUri"
    Write-PmsInfo '  - ID tokens enabled (Authentication > Implicit grant and hybrid flows), single tenant'
    Write-PmsInfo '  - app roles PMS.Viewer, PMS.Analyst, PMS.Regulatory, PMS.MedicalReviewer, PMS.Operator (allowed member type User)'
    Write-PmsInfo "  - Expose an API: scope user_impersonation, with Azure CLI ($azureCliAppId) as authorised client"
    Write-PmsInfo '  - access token version 2 (manifest: requestedAccessTokenVersion = 2)'
}

if ($userType -ne 'user') {
    Write-PmsInfo 'Signed in as a service principal: assign demo users their app roles in the Entra admin center.'
    exit 0
}

$roles = Get-PmsValue 'DEPLOYER_APP_ROLES'
if (-not $roles) { $roles = 'PMS.Operator' }
$servicePrincipal = Invoke-PmsAzJson ad sp show --id $clientId
$me = Invoke-PmsAz ad signed-in-user show --query id -o tsv
if (-not $servicePrincipal -or -not $me) {
    Write-PmsWarn "Could not read the enterprise application or your user. Fix: Entra admin center > Enterprise applications > $clientId > Users and groups > assign yourself $roles."
    exit 0
}
$assignments = Invoke-PmsAzJson rest --method GET --url "$graph/servicePrincipals/$($servicePrincipal.id)/appRoleAssignedTo"
$existing = @()
if ($assignments -and $assignments.value) { $existing = @($assignments.value) }
foreach ($roleValue in $roles.Split(',')) {
    $role = $roleValue.Trim()
    if (-not $role) { continue }
    $appRole = @($servicePrincipal.appRoles | Where-Object { $_.value -eq $role }) | Select-Object -First 1
    if (-not $appRole) {
        Write-PmsWarn "App role $role does not exist on $clientId."
        continue
    }
    if (@($existing | Where-Object { $_.principalId -eq $me -and $_.appRoleId -eq $appRole.id }).Count -gt 0) {
        Write-PmsInfo "You already have the app role $role."
        continue
    }
    $body = @{ principalId = $me; resourceId = $servicePrincipal.id; appRoleId = $appRole.id } | ConvertTo-Json -Compress
    if (Invoke-PmsGraphWrite 'POST' "$graph/servicePrincipals/$($servicePrincipal.id)/appRoleAssignedTo" $body) {
        Write-PmsInfo "Assigned you the app role $role (sign in again to get it in your token)."
    }
    else {
        Write-PmsWarn "Could not assign $role to you. Fix: Entra admin center > Enterprise applications > $clientId > Users and groups > Add user/group ($role)."
    }
}
exit 0
