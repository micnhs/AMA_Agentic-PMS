#Requires -Version 7.2

<#
.SYNOPSIS
    Loads the 12 golden letters and the 6 seed messages (synthetic data only).
.DESCRIPTION
    Without -Azure: directly into the local offline database.
    With -Azure: through the web API of the Azure deployment with your Microsoft Entra token
    (needs the app role PMS.Operator).
.EXAMPLE
    ./scripts/seed.ps1
.EXAMPLE
    ./scripts/seed.ps1 -Azure
#>
param(
    [switch]$Azure,
    [string]$BaseUrl = ''
)

. (Join-Path $PSScriptRoot 'lib/Common.ps1')
Set-Location $script:PmsRepoRoot

$python = Get-PmsPython
if (-not $Azure -and -not $BaseUrl) {
    Set-PmsLocalEnv
    Write-PmsInfo "Seeding the offline database in $($env:DATA_DIR)"
    & $python -m pms seed
    exit $LASTEXITCODE
}

if (-not $BaseUrl) { $BaseUrl = Get-PmsValue 'WEB_URL' }
if (-not $BaseUrl) { Stop-Pms 'The web app URL is unknown.' "Run 'azd up' first, or pass -BaseUrl https://<web-app-fqdn>" }
Set-PmsApiToken
Add-PmsSourcePath
Write-PmsInfo "Seeding $BaseUrl through the API (operator role required)"
& $python -m pms seed --base-url $BaseUrl
exit $LASTEXITCODE
