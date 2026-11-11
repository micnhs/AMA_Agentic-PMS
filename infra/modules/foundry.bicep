// Microsoft Foundry: AIServices account with project management, one project (Foundry Agent Service,
// basic setup with Microsoft-managed storage), fixed model versions in the EU data zone (ADR-001,
// ADR-002), content filter with Prompt Shields (concept 6.6, OI-034) and the Application Insights
// connection used by Foundry tracing (ADR-009). Local (key) authentication is disabled.
param location string
param tags object
param accountName string
param projectName string

@description('Model object from models.json (deploymentName, modelName, modelVersion, modelFormat).')
param analysisModel object

@description('Model object from models.json (deploymentName, modelName, modelVersion, modelFormat).')
param generalModel object

param useGeneralModelForAll bool
param deploymentSku string
param analysisCapacity int
param generalCapacity int
param customContentFilter bool

@description('Application Insights resource connected to the account for Foundry tracing.')
param appInsightsName string

@description('Create the Application Insights connection (Foundry tracing).')
param connectAppInsights bool = true

param logAnalyticsId string

var customPolicyName = 'pms-prompt-shields'
var raiPolicyName = customContentFilter ? customPolicyName : 'Microsoft.DefaultV2'

var harmCategories = [
  'Hate'
  'Sexual'
  'Violence'
  'Selfharm'
]

var promptHarmFilters = [for category in harmCategories: {
  name: category
  blocking: true
  enabled: true
  severityThreshold: 'Medium'
  source: 'Prompt'
}]

var completionHarmFilters = [for category in harmCategories: {
  name: category
  blocking: true
  enabled: true
  severityThreshold: 'Medium'
  source: 'Completion'
}]

// Prompt Shields: 'Jailbreak' = direct attacks, 'Indirect Attack' = instructions hidden in documents
// such as letter L12. They complement the deterministic rule INJ-1.0; they do not replace it.
var shieldFilters = [
  {
    name: 'Jailbreak'
    blocking: true
    enabled: true
    source: 'Prompt'
  }
  {
    name: 'Indirect Attack'
    blocking: true
    enabled: true
    source: 'Prompt'
  }
  {
    name: 'Protected Material Text'
    blocking: true
    enabled: true
    source: 'Completion'
  }
  {
    name: 'Protected Material Code'
    blocking: false
    enabled: true
    source: 'Completion'
  }
]

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'PMS agent demo'
    description: 'Prompt agents A1-A4 and A6 of the PMS agent demo (synthetic data only).'
  }
}

// Child operations on one account are serialised (dependsOn chain) to avoid conflicting updates.
resource raiPolicy 'Microsoft.CognitiveServices/accounts/raiPolicies@2025-06-01' = if (customContentFilter) {
  parent: account
  name: customPolicyName
  properties: {
    basePolicyName: 'Microsoft.DefaultV2'
    contentFilters: concat(promptHarmFilters, completionHarmFilters, shieldFilters)
  }
  dependsOn: [
    project
  ]
}

resource analysisDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = if (!useGeneralModelForAll) {
  parent: account
  name: analysisModel.deploymentName
  sku: {
    name: deploymentSku
    capacity: analysisCapacity
  }
  properties: {
    model: {
      format: analysisModel.modelFormat
      name: analysisModel.modelName
      version: analysisModel.modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
    raiPolicyName: raiPolicyName
  }
  dependsOn: [
    project
    raiPolicy
  ]
}

resource generalDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: generalModel.deploymentName
  sku: {
    name: deploymentSku
    capacity: generalCapacity
  }
  properties: {
    model: {
      format: generalModel.modelFormat
      name: generalModel.modelName
      version: generalModel.modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
    raiPolicyName: raiPolicyName
  }
  dependsOn: [
    project
    raiPolicy
    analysisDeployment
  ]
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

// Foundry tracing reads the Application Insights connection (connection string, not a secret
// according to Azure Monitor guidance). Shared with all projects of the account.
resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/connections@2025-06-01' = if (connectAppInsights) {
  parent: account
  name: 'appinsights-tracing'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
  }
  dependsOn: [
    generalDeployment
  ]
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: account
  properties: {
    workspaceId: logAnalyticsId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output accountName string = account.name
output accountId string = account.id
output projectName string = project.name
output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'
output analysisDeploymentName string = useGeneralModelForAll ? generalModel.deploymentName : analysisModel.deploymentName
output generalDeploymentName string = generalModel.deploymentName
output raiPolicyName string = raiPolicyName
