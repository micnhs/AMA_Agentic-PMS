// Entra app registration for sign-in with app roles (ADR-005, D-17, D-39), created with the
// Microsoft Graph Bicep extension (see ../bicepconfig.json). Single tenant; ID token flow for the
// built-in authentication of Container Apps (no client secret). Deployers without the right to
// create app registrations use an existing registration instead (ENTRA_EXISTING_APP_CLIENT_ID).
// scripts/postprovision.* pre-authorises Azure CLI for the API scope (seed and smoke-test tokens)
// and assigns the deployer's demo role; both are best effort and print the fix if not allowed.
extension microsoftGraphV1

@description('Client-provided unique key of the application (immutable).')
param uniqueName string

param displayName string

@description('Application ID URI, api://<tenant ID>/<name>.')
param identifierUri string

@description('Redirect URI of the Container Apps built-in authentication.')
param redirectUri string

param homePageUrl string

@description('Stable ID of the delegated scope user_impersonation.')
param apiScopeId string

var appRoles = [
  {
    value: 'PMS.Viewer'
    displayName: 'PMS Viewer'
    description: 'View cases, signals, reports, cost and governance views (read only).'
  }
  {
    value: 'PMS.Analyst'
    displayName: 'PMS Analyst'
    description: 'Upload letters, add signals, edit metadata with a reason, take triage, raise urgency.'
  }
  {
    value: 'PMS.Regulatory'
    displayName: 'PMS Regulatory'
    description: 'Record a submission made outside the system, for the approved version only.'
  }
  {
    value: 'PMS.MedicalReviewer'
    displayName: 'PMS Medical Reviewer'
    description: 'The only role that approves, rejects, requests information, closes, closes as non-reportable or lowers urgency, always with a reason.'
  }
  {
    value: 'PMS.Operator'
    displayName: 'PMS Operator'
    description: 'Load demo data, start a new demo run, read operational views and traces.'
  }
]

resource application 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: uniqueName
  displayName: displayName
  signInAudience: 'AzureADMyOrg'
  identifierUris: [
    identifierUri
  ]
  appRoles: [for role in appRoles: {
    id: guid('pms-agent-demo-app-role', role.value)
    allowedMemberTypes: [
      'User'
    ]
    displayName: role.displayName
    description: role.description
    value: role.value
    isEnabled: true
  }]
  api: {
    requestedAccessTokenVersion: 2
    oauth2PermissionScopes: [
      {
        id: apiScopeId
        type: 'User'
        value: 'user_impersonation'
        adminConsentDisplayName: 'Access the PMS agent demo API'
        adminConsentDescription: 'Allows the signed-in user to call the PMS agent demo API with their own app roles.'
        userConsentDisplayName: 'Access the PMS agent demo API'
        userConsentDescription: 'Allows the app to call the PMS agent demo API on your behalf with your app roles.'
        isEnabled: true
      }
    ]
  }
  web: {
    homePageUrl: homePageUrl
    redirectUris: [
      redirectUri
    ]
    implicitGrantSettings: {
      enableIdTokenIssuance: true
      enableAccessTokenIssuance: false
    }
  }
  requiredResourceAccess: [
    {
      // Microsoft Graph, delegated User.Read (sign-in and basic profile).
      resourceAppId: '00000003-0000-0000-c000-000000000000'
      resourceAccess: [
        {
          id: 'e1fe6dd8-ba31-4d61-89e7-88639da4683d'
          type: 'Scope'
        }
      ]
    }
  ]
}

// Only users and groups with an app role can sign in (no role, no access).
resource servicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: application.appId
  appRoleAssignmentRequired: true
}

output clientId string = application.appId
output objectId string = application.id
output servicePrincipalId string = servicePrincipal.id
output identifierUri string = identifierUri
