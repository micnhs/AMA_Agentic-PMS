// PSRule for Azure test entry point (concept T31). Expands the Azure resources of the demo with
// fixed test values. The Entra app registration (Microsoft Graph extension, not an Azure resource)
// and the subscription-level custom role are outside this test; see ../main.bicep.
// Not deployed by azd; only used by 'Assert-PSRule' in .github/workflows/ci.yml.
targetScope = 'resourceGroup'

var location = 'swedencentral'
var resourceToken = 'psruletest001'
var tags = {
  'azd-env-name': 'psrule'
  workload: 'pms-agent-demo'
  'cost-center': 'demo'
}
var models = loadJsonContent('../models.json')

module resources '../modules/resources.bicep' = {
  name: 'psrule-resources'
  params: {
    location: location
    tags: tags
    resourceToken: resourceToken
    environmentName: 'psrule'
    principalId: '11111111-1111-1111-1111-111111111111'
    principalType: 'User'
    deployerEmail: 'deployer@example.com'
    analysisModel: models.analysis
    generalModel: models.general
    useGeneralModelForAll: false
    modelDeploymentSku: 'DataZoneStandard'
    analysisModelCapacity: 50
    generalModelCapacity: 50
    customContentFilter: true
    searchSku: 'basic'
    budgetAmount: 250
    budgetStartDate: '2026-11-01'
    storageSku: 'Standard_LRS'
    logRetentionDays: 30
    logDailyCapGb: 1
    acsEmailSenderRoleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', guid('psrule-acs-email-sender'))
  }
}

module apps '../modules/container-apps.bicep' = {
  name: 'psrule-container-apps'
  params: {
    location: location
    tags: tags
    environmentId: resources.outputs.containerAppsEnvironmentId
    webAppName: 'ca-web-${resourceToken}'
    workerAppName: 'ca-worker-${resourceToken}'
    identityId: resources.outputs.identityId
    registryServer: resources.outputs.registryLoginServer
    appSettings: {
      APP_ENV: 'azure'
      AGENT_BACKEND: 'foundry'
      LOCAL_TEST_USERS: 'false'
      WORKER_MODE: 'separate'
      PREVIEW_MONITORING: 'false'
    }
    entraClientId: '22222222-2222-2222-2222-222222222222'
    entraTenantId: tenant().tenantId
    entraAllowedAudiences: [
      '22222222-2222-2222-2222-222222222222'
      'api://22222222-2222-2222-2222-222222222222'
    ]
    webMinReplicas: 0
    workerHealthProbes: true
    webExists: false
    workerExists: false
  }
}
