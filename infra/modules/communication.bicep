// Azure Communication Services with an Email service and an Azure-managed domain (ADR-006, OI-012).
// Global services with data location Europe. The app sends critical-case e-mails only to the
// configured demo recipients, with case ID, reason and link only (no letter content), using its
// managed identity (custom role, see main.bicep and rbac.bicep).
param tags object
param acsName string
param emailServiceName string
param logAnalyticsId string

@description('Data location (data at rest) of Communication Services and the Email service.')
param dataLocation string = 'Europe'

resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: emailServiceName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
  }
}

resource emailDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource communication 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: acsName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
    linkedDomains: [
      emailDomain.id
    ]
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: communication
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

output acsName string = communication.name
output acsId string = communication.id
output endpoint string = 'https://${communication.properties.hostName}'
// Azure-managed domains come with the sender user name DoNotReply.
output senderAddress string = 'DoNotReply@${emailDomain.properties.mailFromSenderDomain}'
