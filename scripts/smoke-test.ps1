<#
.SYNOPSIS
    Live smoke test against the Azure deployment (Gate C): python -m pms smoke-test --part 1|2.
.DESCRIPTION
    Uses your Microsoft Entra token for the demo API (az login; an app role is needed).
.EXAMPLE
    ./scripts/smoke-test.ps1 -Part 1
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('1', '2')]
    [string]$Part,
    [string]$Out = 'smoke-test-result.json',
    [string]$BaseUrl = ''
)

. (Join-Path $PSScriptRoot 'lib/Common.ps1')
Set-Location $script:PmsRepoRoot

$python = Get-PmsPython
if (-not $BaseUrl) { $BaseUrl = Get-PmsValue 'WEB_URL' }
if (-not $BaseUrl) { Stop-Pms 'The web app URL is unknown.' "Run 'azd up' first, or pass -BaseUrl https://<web-app-fqdn>" }
Set-PmsApiToken
Add-PmsSourcePath
Write-PmsInfo "Smoke test part $Part against $BaseUrl -> $Out"
& $python -m pms smoke-test --base-url $BaseUrl --part $Part --out $Out
exit $LASTEXITCODE
