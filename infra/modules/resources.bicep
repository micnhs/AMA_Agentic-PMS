// All Azure resources of the demo except the container apps and the Entra app registration
// (both depend on the Container Apps environment domain; see main.bicep). Also the entry point of
// the PSRule for Azure test (infra/tests/main.tests.bicep).
targetScope = 'resourceGroup'

param location string
param tags object
param resourceToken string
param environmentName string

@description('Deploying principal (azd AZURE_PRINCIPAL_ID); empty = no deployer role assignments.')
param principalId string = ''

@allowed([
  'User'
  'ServicePrincipal'
])
param principalType string = 'User'

param deployerEmail string
param analysisModel object
param generalModel object
param useGeneralModelForAll bool
param modelDeploymentSku string
param analysisModelCapacity int
param generalModelCapacity int
param customContentFilter bool
param searchSku string
param budgetAmount int
param budgetStartDate string
param storageSku string
param logRetentionDays int
param logDailyCapGb int

@description('Resource ID of the custom e-mail sender role (subscription-level definition).')
param acsEmailSenderRoleDefinitionId string

@description('Name of the knowledge index the app creates and queries in Azure AI Search (ADR-007).')
param searchIndexName string = 'pms-knowledge'

@description('Cosmos DB database name.')
param cosmosDatabaseName string = 'pms'

@description('Allow the app identity to create and fill the knowledge index (seed). false = read-only search access.')
param appSearchWriteAccess bool = true

@description('true = Application Insights accepts only Microsoft Entra ID authenticated telemetry (the app exporter must then use its managed identity).')
param appInsightsEntraIngestionOnly bool = false

@description('Grant the app identity Key Vault Secrets User (only needed if a secret becomes unavoidable, ADR-006).')
param keyVaultSecretsUserForApp bool = false

@description('Give the deploying principal read-only data access (Cosmos DB, Search, Blob) for verification and handover.')
param grantDeployerDataReader bool = true

@allowed([
  'Default'
  'DoNotEnforce'
])
@description('Enforcement of the required-tag policy assignment. DoNotEnforce = compliance reporting only.')
param requiredTagPolicyEnforcement string = 'DoNotEnforce'

@description('Evaluation frequency of the log alert rules (ISO 8601). 15 minutes keeps the cost low.')
param alertEvaluationFrequency string = 'PT15M'

var namePrefix = 'pms-${resourceToken}'

// User-assigned managed identity shared by the web app and the alert worker.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}'
  location: location
  tags: tags
}

module monitoring 'monitoring.bicep' = {
  name: 'pms-monitoring'
  params: {
    location: location
    tags: tags
    logAnalyticsName: 'log-${namePrefix}'
    appInsightsName: 'appi-${namePrefix}'
    retentionInDays: logRetentionDays
    dailyCapGb: logDailyCapGb
    disableLocalAuth: appInsightsEntraIngestionOnly
  }
}

module registry 'registry.bicep' = {
  name: 'pms-registry'
  params: {
    location: location
    tags: tags
    name: 'crpms${resourceToken}'
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

module foundry 'foundry.bicep' = {
  name: 'pms-foundry'
  params: {
    location: location
    tags: tags
    accountName: 'aif-${namePrefix}'
    projectName: 'proj-pms-demo'
    analysisModel: analysisModel
    generalModel: generalModel
    useGeneralModelForAll: useGeneralModelForAll
    deploymentSku: modelDeploymentSku
    analysisCapacity: analysisModelCapacity
    generalCapacity: generalModelCapacity
    customContentFilter: customContentFilter
    appInsightsName: monitoring.outputs.appInsightsName
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

module search 'search.bicep' = {
  name: 'pms-search'
  params: {
    location: location
    tags: tags
    name: 'srch-${namePrefix}'
    sku: searchSku
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

module cosmos 'cosmos.bicep' = {
  name: 'pms-cosmos'
  params: {
    location: location
    tags: tags
    name: 'cosmos-${namePrefix}'
    databaseName: cosmosDatabaseName
    appPrincipalId: identity.properties.principalId
    deployerPrincipalId: grantDeployerDataReader ? principalId : ''
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

module storage 'storage.bicep' = {
  name: 'pms-storage'
  params: {
    location: location
    tags: tags
    name: 'stpms${resourceToken}'
    sku: storageSku
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

module keyVault 'keyvault.bicep' = {
  name: 'pms-keyvault'
  params: {
    location: location
    tags: tags
    name: 'kv-${namePrefix}'
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

module communication 'communication.bicep' = {
  name: 'pms-communication'
  params: {
    tags: tags
    acsName: 'acs-${namePrefix}'
    emailServiceName: 'ecs-${namePrefix}'
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

module containerEnvironment 'container-env.bicep' = {
  name: 'pms-container-env'
  params: {
    location: location
    tags: tags
    name: 'cae-${namePrefix}'
    logAnalyticsId: monitoring.outputs.logAnalyticsId
  }
}

module rbac 'rbac.bicep' = {
  name: 'pms-rbac'
  params: {
    principalId: identity.properties.principalId
    deployerPrincipalId: principalId
    deployerPrincipalType: principalType
    foundryAccountName: foundry.outputs.accountName
    searchName: search.outputs.name
    storageName: storage.outputs.name
    registryName: registry.outputs.name
    acsName: communication.outputs.acsName
    appInsightsName: monitoring.outputs.appInsightsName
    keyVaultName: keyVault.outputs.name
    acsEmailSenderRoleDefinitionId: acsEmailSenderRoleDefinitionId
    appSearchWriteAccess: appSearchWriteAccess
    appInsightsEntraIngestionOnly: appInsightsEntraIngestionOnly
    keyVaultSecretsUserForApp: keyVaultSecretsUserForApp
    grantDeployerDataReader: grantDeployerDataReader
  }
}

module governance 'governance.bicep' = {
  name: 'pms-governance'
  params: {
    location: location
    environmentName: environmentName
    budgetAmount: budgetAmount
    budgetStartDate: budgetStartDate
    deployerEmail: deployerEmail
    requiredTagPolicyEnforcement: requiredTagPolicyEnforcement
  }
}

module observability 'observability.bicep' = {
  name: 'pms-observability'
  params: {
    location: location
    tags: tags
    namePrefix: namePrefix
    appInsightsId: monitoring.outputs.appInsightsId
    deployerEmail: deployerEmail
    evaluationFrequency: alertEvaluationFrequency
  }
}

output identityId string = identity.id
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output registryName string = registry.outputs.name
output registryLoginServer string = registry.outputs.loginServer
output foundryAccountName string = foundry.outputs.accountName
output foundryProjectName string = foundry.outputs.projectName
output foundryProjectEndpoint string = foundry.outputs.projectEndpoint
output analysisDeploymentName string = foundry.outputs.analysisDeploymentName
output generalDeploymentName string = foundry.outputs.generalDeploymentName
output contentFilterName string = foundry.outputs.raiPolicyName
output searchName string = search.outputs.name
output searchEndpoint string = search.outputs.endpoint
output searchIndexName string = searchIndexName
output cosmosAccountName string = cosmos.outputs.name
output cosmosEndpoint string = cosmos.outputs.endpoint
output cosmosDatabaseName string = cosmos.outputs.databaseName
output storageAccountName string = storage.outputs.name
output storageBlobEndpoint string = storage.outputs.blobEndpoint
output keyVaultName string = keyVault.outputs.name
output acsName string = communication.outputs.acsName
output acsEndpoint string = communication.outputs.endpoint
output acsSenderAddress string = communication.outputs.senderAddress
output appInsightsName string = monitoring.outputs.appInsightsName
output appInsightsConnectionString string = monitoring.outputs.appInsightsConnectionString
output logAnalyticsName string = monitoring.outputs.logAnalyticsName
output containerAppsEnvironmentId string = containerEnvironment.outputs.id
output containerAppsEnvironmentName string = containerEnvironment.outputs.name
output containerAppsDefaultDomain string = containerEnvironment.outputs.defaultDomain
output workbookId string = observability.outputs.workbookId
output allowedLocationsPolicyAssignmentId string = governance.outputs.allowedLocationsAssignmentId
output budgetName string = governance.outputs.budgetName
