<#
.SYNOPSIS
    Deploys the demo to Azure: preflight, then 'azd up' (provision + deploy).
.DESCRIPTION
    Nothing is sent to any authority; e-mails go only to the configured demo recipients.
    Extra arguments are passed to 'azd up'.
.EXAMPLE
    ./scripts/deploy.ps1 -EnvironmentName pms-demo -Location swedencentral
#>
param(
    [string]$EnvironmentName = '',
    [string]$Location = '',
    [string]$Subscription = '',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AzdArgs = @()
)

. (Join-Path $PSScriptRoot 'lib/Common.ps1')
Set-Location $script:PmsRepoRoot

Assert-PmsCommand 'azd' 'Install the Azure Developer CLI: https://aka.ms/azd-install'
Assert-PmsCommand 'az' 'Install the Azure CLI: https://aka.ms/installazurecli'

if ($EnvironmentName) {
    $environments = @()
    $listing = & azd env list --output json 2>$null
    if ($LASTEXITCODE -eq 0 -and $listing) {
        $environments = @((@($listing) -join "`n") | ConvertFrom-Json)
    }
    $names = @($environments | ForEach-Object { if ($_.PSObject.Properties['Name']) { "$($_.Name)" } elseif ($_.PSObject.Properties['name']) { "$($_.name)" } })
    if ($names -contains $EnvironmentName) {
        & azd env select $EnvironmentName
    }
    else {
        $newArgs = @('env', 'new', $EnvironmentName)
        if ($Location) { $newArgs += @('--location', $Location) }
        if ($Subscription) { $newArgs += @('--subscription', $Subscription) }
        & azd @newArgs
    }
    if ($LASTEXITCODE -ne 0) { Stop-Pms "Could not select or create the azd environment '$EnvironmentName'." 'Run: azd env list, then retry.' }
}

$currentEnv = Get-PmsValue 'AZURE_ENV_NAME'
if (-not $currentEnv) { Stop-Pms 'No azd environment is selected.' 'Run: ./scripts/deploy.ps1 -EnvironmentName pms-demo -Location swedencentral' }
if ($Location) { [void](Set-PmsAzdValue 'AZURE_LOCATION' $Location) }
if ($Subscription) { [void](Set-PmsAzdValue 'AZURE_SUBSCRIPTION_ID' $Subscription) }

Write-PmsInfo "Environment: $currentEnv"
& (Join-Path $PSScriptRoot 'preflight.ps1')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:PREFLIGHT_DONE = '1'
& azd up @AzdArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-PmsInfo "Deployed. Web app: $(Get-PmsValue 'WEB_URL')"
Write-PmsInfo 'Next: assign app roles to demo users (README), then ./scripts/seed.ps1 -Azure and ./scripts/smoke-test.ps1 -Part 1'
Write-PmsInfo 'Remove everything when done: ./scripts/teardown.ps1'
exit 0
