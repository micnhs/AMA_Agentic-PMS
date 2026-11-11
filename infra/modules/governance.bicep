// Governance on the resource group (concept 6.6, 6.7; ADR-009, ADR-010):
//   - Azure Policy 'Allowed locations' (built-in, enforced): only the selected EU region.
//   - Azure Policy 'Require a tag on resources' (built-in): demo tag, compliance reporting by default.
//   - Budget (default 250 USD per month) with 50/80/100 % actual and 100 % forecast notifications.
param location string
param environmentName string
param budgetAmount int

@description('First day of the budget period, yyyy-MM-dd.')
param budgetStartDate string

param deployerEmail string

@description('Tag every demo resource must carry (set by main.bicep on all resources).')
param requiredTagName string = 'workload'

@allowed([
  'Default'
  'DoNotEnforce'
])
param requiredTagPolicyEnforcement string = 'DoNotEnforce'

var policies = loadJsonContent('../roles.json').policyDefinitions

resource allowedLocations 'Microsoft.Authorization/policyAssignments@2022-06-01' = {
  name: 'pms-allowed-locations'
  properties: {
    displayName: 'PMS demo: allowed locations (${location} only)'
    description: 'Only the selected EU region is allowed in this resource group (pms-agent-demo, concept 3.4 and 6.6). Global services such as Communication Services are exempt in the built-in definition.'
    policyDefinitionId: tenantResourceId('Microsoft.Authorization/policyDefinitions', policies.allowedLocations.id)
    enforcementMode: 'Default'
    parameters: {
      listOfAllowedLocations: {
        value: [
          location
        ]
      }
    }
    nonComplianceMessages: [
      {
        message: 'pms-agent-demo: only ${location} is allowed in this resource group (one EU region).'
      }
    ]
    metadata: {
      assignedBy: 'pms-agent-demo (azd)'
    }
  }
}

resource requiredTag 'Microsoft.Authorization/policyAssignments@2022-06-01' = {
  name: 'pms-required-tag'
  properties: {
    displayName: 'PMS demo: required tag ${requiredTagName}'
    description: 'Every resource of the demo carries the tag ${requiredTagName} for cost attribution (pms-agent-demo, concept 6.6). Enforcement mode is a parameter; the default only reports compliance.'
    policyDefinitionId: tenantResourceId('Microsoft.Authorization/policyDefinitions', policies.requireTagOnResources.id)
    enforcementMode: requiredTagPolicyEnforcement
    parameters: {
      tagName: {
        value: requiredTagName
      }
    }
    nonComplianceMessages: [
      {
        message: 'pms-agent-demo: resources need the tag ${requiredTagName}.'
      }
    ]
    metadata: {
      assignedBy: 'pms-agent-demo (azd)'
    }
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'pms-budget-${environmentName}'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: '${budgetStartDate}T00:00:00Z'
    }
    notifications: {
      actual_50_percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [
          deployerEmail
        ]
      }
      actual_80_percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [
          deployerEmail
        ]
      }
      actual_100_percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: [
          deployerEmail
        ]
      }
      forecast_100_percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [
          deployerEmail
        ]
      }
    }
  }
}

output allowedLocationsAssignmentId string = allowedLocations.id
output requiredTagAssignmentId string = requiredTag.id
output budgetName string = budget.name
