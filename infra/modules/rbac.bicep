// Least-privilege Azure RBAC for the app's user-assigned managed identity and (optionally) the
// deploying principal. Cosmos DB data-plane roles are in cosmos.bicep. Role IDs: ../roles.json.
param principalId string

@description('Deploying principal; empty = no deployer role assignments.')
param deployerPrincipalId string = ''

@allowed([
  'User'
  'ServicePrincipal'
])
param deployerPrincipalType string = 'User'

param foundryAccountName string
param searchName string
param storageName string
param registryName string
param acsName string
param appInsightsName string
param keyVaultName string
param acsEmailSenderRoleDefinitionId string
param appSearchWriteAccess bool
param appInsightsEntraIngestionOnly bool
param keyVaultSecretsUserForApp bool
param grantDeployerDataReader bool

var roles = loadJsonContent('../roles.json').azureRbac
var deployerEnabled = !empty(deployerPrincipalId)
var deployerReaderEnabled = deployerEnabled && grantDeployerDataReader

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource search 'Microsoft.Search/searchServices@2023-11-01' existing = {
  name: searchName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource communication 'Microsoft.Communication/communicationServices@2023-04-01' existing = {
  name: acsName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// --- App identity ---------------------------------------------------------------------------

// Foundry Agent Service and model deployments (prompt agents, Responses API).
resource appFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, principalId, roles.azureAiUser.id)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAiUser.id)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: run Foundry prompt agents and model deployments'
  }
}

resource appSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, principalId, roles.searchIndexDataReader.id)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader.id)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: read-only search tool (A6)'
  }
}

resource appSearchIndexWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (appSearchWriteAccess) {
  name: guid(search.id, principalId, roles.searchIndexDataContributor.id)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataContributor.id)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: upload the fictional corpus into the knowledge index (seed)'
  }
}

resource appSearchIndexManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (appSearchWriteAccess) {
  name: guid(search.id, principalId, roles.searchServiceContributor.id)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor.id)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: create the knowledge index definition (seed)'
  }
}

resource appBlobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, principalId, roles.storageBlobDataContributor.id)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor.id)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: store letters and reports'
  }
}

resource appAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, roles.acrPull.id)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull.id)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: pull the container image'
  }
}

resource appEmailSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(communication.id, principalId, acsEmailSenderRoleDefinitionId)
  scope: communication
  properties: {
    roleDefinitionId: acsEmailSenderRoleDefinitionId
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: send critical-case e-mails to configured demo recipients'
  }
}

// Platform cost and forecast for the cost view (ADR-010).
resource appCostReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, principalId, roles.costManagementReader.id)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.costManagementReader.id)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: read cost and forecast of this resource group'
  }
}

resource appTelemetryPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (appInsightsEntraIngestionOnly) {
  name: guid(appInsights.id, principalId, roles.monitoringMetricsPublisher.id)
  scope: appInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher.id)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: send telemetry with Microsoft Entra ID'
  }
}

resource appSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (keyVaultSecretsUserForApp) {
  name: guid(vault.id, principalId, roles.keyVaultSecretsUser.id)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser.id)
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'pms-agent-demo app: read secrets (only if a secret becomes unavoidable)'
  }
}

// --- Deploying principal (operator of the demo) ------------------------------------------------

resource deployerFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployerEnabled) {
  name: guid(foundry.id, deployerPrincipalId, roles.azureAiUser.id)
  scope: foundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAiUser.id)
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    description: 'pms-agent-demo deployer: Foundry portal, traces and evaluations (Gate C)'
  }
}

resource deployerSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployerReaderEnabled) {
  name: guid(search.id, deployerPrincipalId, roles.searchIndexDataReader.id)
  scope: search
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader.id)
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    description: 'pms-agent-demo deployer: read-only check of the knowledge index'
  }
}

resource deployerBlobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployerReaderEnabled) {
  name: guid(storage.id, deployerPrincipalId, roles.storageBlobDataReader.id)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataReader.id)
    principalId: deployerPrincipalId
    principalType: deployerPrincipalType
    description: 'pms-agent-demo deployer: read-only check of letters and reports'
  }
}
