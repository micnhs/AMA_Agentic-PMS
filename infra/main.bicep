// pms-agent-demo – Azure entry point for azd (concept 6.9, 6.11, ADR-001/002/005/007/009/010/011).
// One EU region and one resource group. Synthetic data only. The template illustrates a
// Well-Architected-aligned demo baseline; it makes no compliance claim.
//
// Order: resource group -> Azure resources (modules/resources.bicep) -> Entra app registration
// (needs the Container Apps domain for its redirect URI) -> container apps with built-in auth.
targetScope = 'subscription'

// ---------------------------------------------------------------------------------------------
// Parameters (mapped from azd environment values in main.parameters.json)
// ---------------------------------------------------------------------------------------------

@minLength(1)
@maxLength(40)
@description('azd environment name. Used for the resource group name, tags and display names.')
param environmentName string

@description('Single EU region for all resources (concept 3.4, ADR-002). Default Sweden Central; Germany West Central is the documented fallback after preflight.')
@allowed([
  'swedencentral'
  'germanywestcentral'
  'francecentral'
  'italynorth'
  'polandcentral'
  'spaincentral'
  'westeurope'
  'northeurope'
  'austriaeast'
  'belgiumcentral'
  'denmarkeast'
])
@metadata({
  azd: {
    type: 'location'
  }
})
param location string = 'swedencentral'

@description('Object ID of the deploying principal (set by azd as AZURE_PRINCIPAL_ID). Receives Azure AI User on Foundry and optional read-only data roles.')
param principalId string = ''

@allowed([
  'User'
  'ServicePrincipal'
])
@description('Type of principalId: User for an interactive azd run, ServicePrincipal for the OIDC pipeline.')
param principalType string = 'User'

@minLength(3)
@description('Deployer e-mail address for budget notifications and operational alerts.')
param deployerEmail string

@description('Comma-separated demo recipients of critical-case e-mails sent by the app (never an authority). Empty = deployerEmail.')
param alertRecipients string = ''

@description('Bring your own app registration: client ID of an existing Entra app registration. Empty = create one with the Microsoft Graph Bicep extension.')
param existingEntraAppClientId string = ''

@description('Bring your own app registration only: its Application ID URI. Empty = api://<client ID>.')
param existingEntraAppIdentifierUri string = ''

@description('Model fallback (ADR-002): deploy only gpt-6.1-sol and use it for all agents.')
param useGeneralModelForAll bool = false

@allowed([
  'DataZoneStandard'
  'Standard'
])
@description('Model deployment type. DataZoneStandard (EU data zone) is the ADR-002 decision; Standard processes in the selected EU region only. GlobalStandard is never allowed.')
param modelDeploymentSku string = 'DataZoneStandard'

@minValue(1)
@description('Capacity of the gpt-6-astra deployment in thousands of tokens per minute.')
param analysisModelCapacity int = 50

@minValue(1)
@description('Capacity of the gpt-6.1-sol deployment in thousands of tokens per minute.')
param generalModelCapacity int = 50

@description('Custom content filter with Prompt Shields for direct (jailbreak) and indirect attacks (concept 6.6, OI-034). false = built-in Microsoft.DefaultV2 policy.')
param customContentFilter bool = true

@allowed([
  'free'
  'basic'
  'standard'
])
@description('Azure AI Search tier (OI-035). basic is the approved default and the largest fixed cost item.')
param searchSku string = 'basic'

@minValue(1)
@description('Monthly budget of the resource group in USD (ADR-010).')
param budgetAmount int = 250

@description('Optional override of the budget start (yyyy-MM-01). Empty = first day of the current month at each provision; Azure only accepts a past start date within the current month.')
param budgetStartDate string = ''

@description('Fallback start of the budget period when budgetStartDate is empty.')
param budgetStartDateDefault string = utcNow('yyyy-MM-01')

@minValue(0)
@maxValue(3)
@description('Minimum replicas of the web app (0 = scale to zero when idle; the alert worker always keeps 1).')
param webMinReplicas int = 0

@description('HTTP health probes on GET /healthz (port 8000) for the alert worker (concept 6.11). The worker process must serve /healthz.')
param workerHealthProbes bool = true

@description('Preview monitoring features (agent monitoring dashboard, continuous evaluation, red teaming). Off until approved (OI-032).')
param previewMonitoring bool = false

@description('Set by azd (SERVICE_WEB_RESOURCE_EXISTS): keeps the deployed image on re-provision.')
param webExists bool = false

@description('Set by azd (SERVICE_WORKER_RESOURCE_EXISTS): keeps the deployed image on re-provision.')
param workerExists bool = false

@allowed([
  'Standard_LRS'
  'Standard_ZRS'
])
@description('Storage redundancy. Standard_ZRS is a low-cost zone-redundancy option (OI-037).')
param storageSku string = 'Standard_LRS'

@minValue(30)
@maxValue(730)
@description('Log Analytics retention in days (traces contain prompt content; concept 6.6, OI-033).')
param logRetentionDays int = 30

@description('Daily Log Analytics ingestion cap in GB (cost guard). -1 = no cap.')
param logDailyCapGb int = 1

// ---------------------------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------------------------

var models = loadJsonContent('models.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  'azd-env-name': environmentName
  workload: 'pms-agent-demo'
  'cost-center': 'demo'
}
var createEntraApp = empty(existingEntraAppClientId)
var webAppName = 'ca-web-${resourceToken}'
var workerAppName = 'ca-worker-${resourceToken}'
var apiScopeId = guid(subscription().id, environmentName, 'pms-user-impersonation')
var createdIdentifierUri = 'api://${tenant().tenantId}/pms-agent-demo-${resourceToken}'
var effectiveAlertRecipients = empty(alertRecipients) ? deployerEmail : alertRecipients
var effectiveBudgetStart = empty(budgetStartDate) ? budgetStartDateDefault : budgetStartDate

// ---------------------------------------------------------------------------------------------
// Resource group and the subscription-level custom role for e-mail sending
// ---------------------------------------------------------------------------------------------

resource rg 'Microsoft.Resources/resourceGroups@2022-09-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

// Least-privilege role to send e-mail through Azure Communication Services with Microsoft Entra ID
// (no access keys). Custom role definitions live at subscription level; this one is assignable to
// the demo resource group only. scripts/teardown.* deletes it after 'azd down'.
resource acsEmailSenderRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, rg.name, 'pms-acs-email-sender')
  properties: {
    roleName: 'PMS demo ACS e-mail sender (${resourceToken})'
    description: 'Send e-mail through Azure Communication Services with Microsoft Entra ID (pms-agent-demo, environment ${environmentName}).'
    type: 'customRole'
    permissions: [
      {
        actions: [
          'Microsoft.Communication/CommunicationServices/Read'
          'Microsoft.Communication/CommunicationServices/Write'
          'Microsoft.Communication/EmailServices/write'
        ]
        notActions: []
        dataActions: []
        notDataActions: []
      }
    ]
    assignableScopes: [
      rg.id
    ]
  }
}

// ---------------------------------------------------------------------------------------------
// Modules
// ---------------------------------------------------------------------------------------------

module resources 'modules/resources.bicep' = {
  name: 'pms-resources'
  scope: rg
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    environmentName: environmentName
    principalId: principalId
    principalType: principalType
    deployerEmail: deployerEmail
    analysisModel: models.analysis
    generalModel: models.general
    useGeneralModelForAll: useGeneralModelForAll
    modelDeploymentSku: modelDeploymentSku
    analysisModelCapacity: analysisModelCapacity
    generalModelCapacity: generalModelCapacity
    customContentFilter: customContentFilter
    searchSku: searchSku
    budgetAmount: budgetAmount
    budgetStartDate: effectiveBudgetStart
    storageSku: storageSku
    logRetentionDays: logRetentionDays
    logDailyCapGb: logDailyCapGb
    acsEmailSenderRoleDefinitionId: acsEmailSenderRole.id
  }
}

// Public URL of the web app, predicted from the Container Apps environment domain (an external app's FQDN is
// '<app name>.<environment default domain>'), so the Entra app and both container apps can use it without a
// dependency on the web app itself.
var webBaseUrl = 'https://${webAppName}.${resources.outputs.containerAppsDefaultDomain}'

// Created only when no existing app registration is supplied (ADR-005, OI-013).
module entraApp 'modules/entra-app.bicep' = if (createEntraApp) {
  name: 'pms-entra-app'
  scope: rg
  params: {
    uniqueName: 'pms-agent-demo-${resourceToken}'
    displayName: 'PMS agent demo (${environmentName})'
    identifierUri: createdIdentifierUri
    redirectUri: '${webBaseUrl}/.auth/login/aad/callback'
    homePageUrl: webBaseUrl
    apiScopeId: apiScopeId
  }
}

var effectiveClientId = createEntraApp ? entraApp.outputs.clientId : existingEntraAppClientId
var effectiveIdentifierUri = createEntraApp ? createdIdentifierUri : (empty(existingEntraAppIdentifierUri) ? 'api://${existingEntraAppClientId}' : existingEntraAppIdentifierUri)

// Application settings (BUILD_SPEC 4.2). The same values become container environment variables
// and azd environment values (outputs below).
var appSettings = {
  APP_ENV: 'azure'
  AGENT_BACKEND: 'foundry'
  LOCAL_TEST_USERS: 'false'
  DATA_DIR: '/app/var'
  PORT: '8000'
  WORKER_MODE: 'separate'
  FOUNDRY_PROJECT_ENDPOINT: resources.outputs.foundryProjectEndpoint
  MODEL_DEPLOYMENT_ANALYSIS: resources.outputs.analysisDeploymentName
  MODEL_DEPLOYMENT_GENERAL: resources.outputs.generalDeploymentName
  AZURE_SEARCH_ENDPOINT: resources.outputs.searchEndpoint
  AZURE_SEARCH_INDEX: resources.outputs.searchIndexName
  COSMOS_ENDPOINT: resources.outputs.cosmosEndpoint
  COSMOS_DATABASE: resources.outputs.cosmosDatabaseName
  STORAGE_ACCOUNT_URL: resources.outputs.storageBlobEndpoint
  ACS_ENDPOINT: resources.outputs.acsEndpoint
  ACS_SENDER_ADDRESS: resources.outputs.acsSenderAddress
  ALERT_RECIPIENTS: effectiveAlertRecipients
  AZURE_CLIENT_ID: resources.outputs.identityClientId
  APPLICATIONINSIGHTS_CONNECTION_STRING: resources.outputs.appInsightsConnectionString
  COST_SCOPE: rg.id
  BUDGET_USD: string(budgetAmount)
  ENTRA_TENANT_ID: tenant().tenantId
  ENTRA_APP_CLIENT_ID: effectiveClientId
  PREVIEW_MONITORING: previewMonitoring ? 'true' : 'false'
  // Absolute case links in alert e-mails (src/pms/notify.py).
  PUBLIC_BASE_URL: webBaseUrl
  // Read by src/pms/config.py; MODEL_DEPLOYMENT_ANALYSIS already names gpt-6.1-sol when true.
  MODEL_FALLBACK_ALL_GENERAL: useGeneralModelForAll ? 'true' : 'false'
}

module apps 'modules/container-apps.bicep' = {
  name: 'pms-container-apps'
  scope: rg
  params: {
    location: location
    tags: tags
    environmentId: resources.outputs.containerAppsEnvironmentId
    webAppName: webAppName
    workerAppName: workerAppName
    identityId: resources.outputs.identityId
    registryServer: resources.outputs.registryLoginServer
    appSettings: appSettings
    entraClientId: effectiveClientId
    entraTenantId: tenant().tenantId
    entraAllowedAudiences: union([
      effectiveClientId
      'api://${effectiveClientId}'
    ], [
      effectiveIdentifierUri
    ])
    webMinReplicas: webMinReplicas
    workerHealthProbes: workerHealthProbes
    webExists: webExists
    workerExists: workerExists
  }
}

// ---------------------------------------------------------------------------------------------
// Outputs (azd writes them to .azure/<env>/.env; names follow BUILD_SPEC 4.2)
// ---------------------------------------------------------------------------------------------

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = resources.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = resources.outputs.registryName
output AZURE_CONTAINER_ENVIRONMENT_NAME string = resources.outputs.containerAppsEnvironmentName
output AZURE_KEY_VAULT_NAME string = resources.outputs.keyVaultName
output AZURE_FOUNDRY_ACCOUNT_NAME string = resources.outputs.foundryAccountName
output AZURE_FOUNDRY_PROJECT_NAME string = resources.outputs.foundryProjectName
output AZURE_AI_SEARCH_NAME string = resources.outputs.searchName
output AZURE_COSMOS_ACCOUNT_NAME string = resources.outputs.cosmosAccountName
output AZURE_STORAGE_ACCOUNT_NAME string = resources.outputs.storageAccountName
output AZURE_APP_INSIGHTS_NAME string = resources.outputs.appInsightsName
output AZURE_LOG_ANALYTICS_NAME string = resources.outputs.logAnalyticsName
output AZURE_WORKBOOK_ID string = resources.outputs.workbookId
output AZURE_POLICY_ASSIGNMENT_ID string = resources.outputs.allowedLocationsPolicyAssignmentId
output AZURE_CONTENT_FILTER string = resources.outputs.contentFilterName
output AZURE_ACS_EMAIL_ROLE_ID string = acsEmailSenderRole.name
output WEB_APP_NAME string = apps.outputs.webName
output WORKER_APP_NAME string = apps.outputs.workerName
output WEB_URL string = 'https://${apps.outputs.webFqdn}'
output WEB_REDIRECT_URI string = 'https://${apps.outputs.webFqdn}/.auth/login/aad/callback'
output ENTRA_APP_CREATED string = createEntraApp ? 'true' : 'false'
output ENTRA_APP_IDENTIFIER_URI string = effectiveIdentifierUri
output ENTRA_API_SCOPE string = '${effectiveIdentifierUri}/user_impersonation'

output APP_ENV string = appSettings.APP_ENV
output AGENT_BACKEND string = appSettings.AGENT_BACKEND
output LOCAL_TEST_USERS string = appSettings.LOCAL_TEST_USERS
output DATA_DIR string = appSettings.DATA_DIR
output PORT string = appSettings.PORT
output WORKER_MODE string = appSettings.WORKER_MODE
output FOUNDRY_PROJECT_ENDPOINT string = appSettings.FOUNDRY_PROJECT_ENDPOINT
output MODEL_DEPLOYMENT_ANALYSIS string = appSettings.MODEL_DEPLOYMENT_ANALYSIS
output MODEL_DEPLOYMENT_GENERAL string = appSettings.MODEL_DEPLOYMENT_GENERAL
output AZURE_SEARCH_ENDPOINT string = appSettings.AZURE_SEARCH_ENDPOINT
output AZURE_SEARCH_INDEX string = appSettings.AZURE_SEARCH_INDEX
output COSMOS_ENDPOINT string = appSettings.COSMOS_ENDPOINT
output COSMOS_DATABASE string = appSettings.COSMOS_DATABASE
output STORAGE_ACCOUNT_URL string = appSettings.STORAGE_ACCOUNT_URL
output ACS_ENDPOINT string = appSettings.ACS_ENDPOINT
output ACS_SENDER_ADDRESS string = appSettings.ACS_SENDER_ADDRESS
output ALERT_RECIPIENTS string = appSettings.ALERT_RECIPIENTS
output AZURE_CLIENT_ID string = appSettings.AZURE_CLIENT_ID
output APPLICATIONINSIGHTS_CONNECTION_STRING string = appSettings.APPLICATIONINSIGHTS_CONNECTION_STRING
output COST_SCOPE string = appSettings.COST_SCOPE
output BUDGET_USD string = appSettings.BUDGET_USD
output ENTRA_TENANT_ID string = appSettings.ENTRA_TENANT_ID
output ENTRA_APP_CLIENT_ID string = appSettings.ENTRA_APP_CLIENT_ID
output PREVIEW_MONITORING string = appSettings.PREVIEW_MONITORING
output PUBLIC_BASE_URL string = appSettings.PUBLIC_BASE_URL
