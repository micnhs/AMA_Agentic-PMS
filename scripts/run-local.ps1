<#
.SYNOPSIS
    Starts the demo offline: no Azure, no model calls, Python 3.12 standard library only, nothing to install.
.DESCRIPTION
    Loads the demo data on start (python -m pms serve --seed) and runs the worker in-process.
    Extra arguments are passed to 'python -m pms serve', e.g. --port 8080.
.EXAMPLE
    ./scripts/run-local.ps1
#>
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ServeArgs = @()
)

. (Join-Path $PSScriptRoot 'lib/Common.ps1')
Set-Location $script:PmsRepoRoot

$python = Get-PmsPython
Set-PmsLocalEnv
Write-PmsInfo "Offline mode: APP_ENV=local, AGENT_BACKEND=offline, data in $($env:DATA_DIR) (synthetic data only)."
Write-PmsInfo 'The server prints the URL to open (default http://127.0.0.1:8000; if 8000 is busy, the next free port). Stop with Ctrl+C.'
& $python -m pms serve --seed @ServeArgs
exit $LASTEXITCODE
