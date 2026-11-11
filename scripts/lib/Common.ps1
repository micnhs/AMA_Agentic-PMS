# Shared helpers for scripts/*.ps1 (PowerShell 7.2 or newer). Dot-sourced, not executed.
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

# Second guard behind '#Requires -Version 7.2' in every script. 'throw', not 'exit': an exit in a
# dot-sourced file ends only this file, and the calling script would carry on.
if ($PSVersionTable.PSVersion -lt [version]'7.2') {
    throw "[pms] FAILED: PowerShell $($PSVersionTable.PSVersion) is too old. Fix: Install PowerShell 7.2+ (https://aka.ms/powershell), start it with pwsh and run the script there."
}

$script:PmsRepoRoot = (Resolve-Path (Join-Path (Join-Path $PSScriptRoot '..') '..')).Path

# Python writes UTF-8 also when its output is piped (for example into Tee-Object for deploy.log).
$env:PYTHONUTF8 = '1'

function Write-PmsInfo([string]$Message) {
    Write-Host "[pms] $Message"
}

function Write-PmsWarn([string]$Message) {
    Write-Host "[pms] WARNING: $Message" -ForegroundColor Yellow
}

# Stops with one plain line: what is wrong and how to fix it.
function Stop-Pms([string]$Problem, [string]$Fix) {
    [Console]::Error.WriteLine("[pms] FAILED: $Problem Fix: $Fix")
    exit 1
}

function Test-PmsCommand([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Assert-PmsCommand([string]$Name, [string]$Fix) {
    if (-not (Test-PmsCommand $Name)) {
        Stop-Pms "'$Name' was not found on PATH." $Fix
    }
}

# Runs az with the given arguments; returns trimmed text output, or '' on failure.
function Invoke-PmsAz {
    $output = & az @args 2>$null
    if ($LASTEXITCODE -ne 0 -or $null -eq $output) {
        return ''
    }
    return ((@($output) | ForEach-Object { "$_" }) -join "`n").Trim()
}

# Runs az with the given arguments and '-o json'; returns parsed JSON, or $null on failure.
function Invoke-PmsAzJson {
    $output = & az @args -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or $null -eq $output) {
        return $null
    }
    $text = (@($output) | ForEach-Object { "$_" }) -join "`n"
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    return ($text | ConvertFrom-Json)
}

# Path of the first Python >= 3.12 interpreter, or stops.
function Get-PmsPython {
    # No double quotes in the probe: PowerShell 7.2 passes them to native programs unescaped.
    $probe = 'import sys; print(sys.executable if sys.version_info >= (3, 12) else str())'
    foreach ($candidate in @('python3', 'python', 'py -3')) {
        $parts = $candidate -split ' '
        $command = Get-Command $parts[0] -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }
        $extra = @($parts | Select-Object -Skip 1)
        $output = & $command.Source @extra -c $probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $output) {
            $path = "$(@($output)[-1])".Trim()
            if ($path) {
                return $path
            }
        }
    }
    Stop-Pms 'Python 3.12 or newer was not found.' 'Install Python 3.12+ (https://www.python.org/downloads/) and open a new terminal.'
}

# True if $Actual (e.g. '2.70.1' or 'azd version 1.21.1 (commit ...)') >= $Minimum.
function Test-PmsVersionAtLeast([string]$Actual, [string]$Minimum) {
    $match = [regex]::Match("$Actual", '\d+(\.\d+){0,2}')
    if (-not $match.Success) {
        return $false
    }
    $numbers = @($match.Value.Split('.') | ForEach-Object { [int]$_ })
    while ($numbers.Count -lt 3) {
        $numbers += 0
    }
    return ([version]::new($numbers[0], $numbers[1], $numbers[2]) -ge [version]$Minimum)
}

# Value of an environment variable, else of the selected azd environment, else ''.
function Get-PmsValue([string]$Key) {
    $value = [Environment]::GetEnvironmentVariable($Key)
    if ([string]::IsNullOrEmpty($value) -and (Test-PmsCommand 'azd')) {
        $output = & azd env get-value $Key 2>$null
        $value = (@($output) | ForEach-Object { "$_" }) -join ''
        if ($LASTEXITCODE -ne 0 -or $value -match 'ERROR|not found') {
            $value = ''
        }
    }
    if ($null -eq $value) {
        return ''
    }
    return $value.Trim()
}

# Stores a value in the selected azd environment; returns $true on success.
function Set-PmsAzdValue([string]$Key, [string]$Value) {
    & azd env set $Key $Value *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-PmsWarn "Could not store $Key in the azd environment (is one selected? azd env select <name>)."
        return $false
    }
    return $true
}

# Offline mode: no Azure endpoints, deterministic agents, local test users, in-process worker.
function Set-PmsLocalEnv {
    $azureNames = @(
        'FOUNDRY_PROJECT_ENDPOINT', 'AZURE_SEARCH_ENDPOINT', 'AZURE_SEARCH_INDEX', 'COSMOS_ENDPOINT', 'COSMOS_DATABASE',
        'STORAGE_ACCOUNT_URL', 'ACS_ENDPOINT', 'ACS_SENDER_ADDRESS', 'ALERT_RECIPIENTS', 'APPLICATIONINSIGHTS_CONNECTION_STRING',
        'COST_SCOPE', 'AZURE_CLIENT_ID', 'ENTRA_TENANT_ID', 'ENTRA_APP_CLIENT_ID', 'MODEL_DEPLOYMENT_ANALYSIS',
        'MODEL_DEPLOYMENT_GENERAL', 'PMS_API_TOKEN'
    )
    foreach ($name in $azureNames) {
        Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
    }
    $env:APP_ENV = 'local'
    $env:AGENT_BACKEND = 'offline'
    $env:LOCAL_TEST_USERS = 'true'
    $env:WORKER_MODE = 'inprocess'
    $env:PREVIEW_MONITORING = 'false'
    if (-not $env:DATA_DIR) {
        $env:DATA_DIR = './var'
    }
    Add-PmsSourcePath
}

function Add-PmsSourcePath {
    $src = Join-Path $script:PmsRepoRoot 'src'
    if ($env:PYTHONPATH) {
        $env:PYTHONPATH = "$src$([IO.Path]::PathSeparator)$($env:PYTHONPATH)"
    }
    else {
        $env:PYTHONPATH = $src
    }
}

# Sets $env:PMS_API_TOKEN: a Microsoft Entra access token for the demo API (never printed).
function Set-PmsApiToken {
    Assert-PmsCommand 'az' 'Install the Azure CLI: https://aka.ms/installazurecli'
    $scope = Get-PmsValue 'ENTRA_API_SCOPE'
    if (-not $scope) {
        $clientId = Get-PmsValue 'ENTRA_APP_CLIENT_ID'
        if (-not $clientId) {
            Stop-Pms 'The Entra app of the demo is unknown (ENTRA_APP_CLIENT_ID).' "Run 'azd provision' first or set ENTRA_API_SCOPE."
        }
        $scope = "api://$clientId/user_impersonation"
    }
    $token = Invoke-PmsAz account get-access-token --scope $scope --query accessToken -o tsv
    if (-not $token) {
        Stop-Pms "Could not get a Microsoft Entra token for $scope." "Run: az login --scope $scope (and make sure you have a PMS app role, e.g. PMS.Operator)."
    }
    $env:PMS_API_TOKEN = $token
}
