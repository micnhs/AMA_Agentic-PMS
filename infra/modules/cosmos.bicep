// Azure Cosmos DB for NoSQL, serverless, local (key) authentication disabled (ADR-004).
// Containers (partition key /pk): cases, signals, records and the append-only audit container.
// Data-plane RBAC for the app identity:
//   - Cosmos DB Built-in Data Reader on the account (read everything, account metadata for the SDK)
//   - Cosmos DB Built-in Data Contributor on cases, signals and records only
//   - custom role 'PMS audit append-only' on the audit container: create and read, but no replace,
//     upsert or delete. The hash chain (concept 8) makes any change outside the app detectable.
param location string
param tags object
param name string
param databaseName string

@description('Principal ID of the app managed identity.')
param appPrincipalId string

@description('Principal ID that gets read-only data access for verification; empty = none.')
param deployerPrincipalId string = ''

param logAnalyticsId string

var auditContainerName = 'audit'
var standardContainerNames = [
  'cases'
  'signals'
  'records'
]
var builtInDataReaderId = '00000000-0000-0000-0000-000000000001'
var builtInDataContributorId = '00000000-0000-0000-0000-000000000002'

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: name
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    disableLocalAuth: true
    disableKeyBasedMetadataWriteAccess: true
    minimalTlsVersion: 'Tls12'
    publicNetworkAccess: 'Enabled'
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    enableFreeTier: false
    // Continuous backup, 7-day tier: point-in-time restore into a new account in the same region.
    // Microsoft Learn (checked 11 Nov 2026): the 7-day tier has no charge for backup storage; a
    // restore is charged. Free recommendations are on by default (ADR-011, option B).
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: {
        tier: 'Continuous7Days'
      }
    }
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: databaseName
  tags: tags
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource standardContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [for containerName in standardContainerNames: {
  parent: database
  name: containerName
  tags: tags
  properties: {
    resource: {
      id: containerName
      partitionKey: {
        paths: [
          '/pk'
        ]
        kind: 'Hash'
        version: 2
      }
    }
  }
}]

// One logical partition for the chain (pk = 'audit-chain'); the unique key on /seq rejects a second
// record with the same sequence number (concurrent writers must retry with the next number).
resource auditContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: auditContainerName
  tags: tags
  properties: {
    resource: {
      id: auditContainerName
      partitionKey: {
        paths: [
          '/pk'
        ]
        kind: 'Hash'
        version: 2
      }
      uniqueKeyPolicy: {
        uniqueKeys: [
          {
            paths: [
              '/seq'
            ]
          }
        ]
      }
    }
  }
}

// Role assignments are serialised: parallel RBAC operations on one account can conflict.
resource appDataReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, appPrincipalId, builtInDataReaderId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${builtInDataReaderId}'
    principalId: appPrincipalId
    scope: account.id
  }
  dependsOn: [
    standardContainers
    auditContainer
  ]
}

@batchSize(1)
resource appDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = [for containerName in standardContainerNames: {
  parent: account
  name: guid(account.id, appPrincipalId, builtInDataContributorId, containerName)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${builtInDataContributorId}'
    principalId: appPrincipalId
    scope: '${account.id}/dbs/${databaseName}/colls/${containerName}'
  }
  dependsOn: [
    appDataReader
  ]
}]

resource auditAppendOnlyRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions@2024-11-15' = {
  parent: account
  name: guid(account.id, 'pms-audit-append-only')
  properties: {
    roleName: 'PMS audit append-only'
    type: 'CustomRole'
    assignableScopes: [
      account.id
    ]
    permissions: [
      {
        dataActions: [
          'Microsoft.DocumentDB/databaseAccounts/readMetadata'
          'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/items/create'
          'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/items/read'
          'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/executeQuery'
          'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/readChangeFeed'
        ]
      }
    ]
  }
  dependsOn: [
    appDataContributor
  ]
}

resource appAuditAppendOnly 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, appPrincipalId, 'pms-audit-append-only')
  properties: {
    roleDefinitionId: auditAppendOnlyRole.id
    principalId: appPrincipalId
    scope: '${account.id}/dbs/${databaseName}/colls/${auditContainerName}'
  }
}

resource deployerDataReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (!empty(deployerPrincipalId)) {
  parent: account
  name: guid(account.id, deployerPrincipalId, builtInDataReaderId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${builtInDataReaderId}'
    principalId: deployerPrincipalId
    scope: account.id
  }
  dependsOn: [
    appAuditAppendOnly
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
        category: 'Requests'
        enabled: true
      }
    ]
  }
}

output name string = account.name
output id string = account.id
output endpoint string = account.properties.documentEndpoint
output databaseName string = database.name
output auditContainerName string = auditContainerName
output containerNames array = concat(standardContainerNames, [
  auditContainerName
])
