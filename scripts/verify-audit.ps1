#Requires -Version 7.2

<#
.SYNOPSIS
    Recomputes the audit hash chain and reports the first break (concept 8).
.DESCRIPTION
    Without -Azure: the local offline database. With -Azure: the Azure deployment, through the API.
.EXAMPLE
    ./scripts/verify-audit.ps1
.EXAMPLE
    ./scripts/verify-audit.ps1 -Azure
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
    & $python -m pms verify-audit
    exit $LASTEXITCODE
}

if (-not $BaseUrl) { $BaseUrl = Get-PmsValue 'WEB_URL' }
if (-not $BaseUrl) { Stop-Pms 'The web app URL is unknown.' "Run 'azd up' first, or pass -BaseUrl https://<web-app-fqdn>" }
Set-PmsApiToken
Add-PmsSourcePath
& $python -m pms verify-audit --base-url $BaseUrl
exit $LASTEXITCODE
