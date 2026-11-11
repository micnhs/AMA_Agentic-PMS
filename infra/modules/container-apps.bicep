// Two container apps from one image (azure.yaml services 'web' and 'worker'):
//   web    - 'python -m pms serve --host 0.0.0.0', WORKER_MODE=separate, external HTTPS ingress,
//            Container Apps built-in authentication with Microsoft Entra ID (sign-in required,
//            single tenant, ID token flow without client secret; ADR-005, D-39).
//   worker - 'python -m pms worker', no ingress (not reachable from outside), exactly one replica so
//            critical-case alerts meet the 60-second pilot target (ADR-006).
// The image entrypoint (docker/entrypoint.sh) selects the process from PMS_ROLE, so 'azd deploy'
// can swap images without touching commands.
param location string
param tags object
param environmentId string
param webAppName string
param workerAppName string

@description('Resource ID of the user-assigned managed identity.')
param identityId string

param registryServer string

@description('Application settings (BUILD_SPEC 4.2) passed as environment variables.')
param appSettings object

param entraClientId string
param entraTenantId string
param entraAllowedAudiences array

@minValue(0)
@maxValue(3)
param webMinReplicas int = 0

@minValue(1)
@maxValue(10)
param webMaxReplicas int = 3

param workerHealthProbes bool = true
param webExists bool = false
param workerExists bool = false

@description('Public placeholder image used until azd deploys the application image.')
param placeholderImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

var appPort = 8000

var baseEnv = [for setting in items(appSettings): {
  name: setting.key
  value: string(setting.value)
}]

var webEnv = concat(baseEnv, [
  {
    name: 'PMS_ROLE'
    value: 'web'
  }
])

var workerEnv = concat(baseEnv, [
  {
    name: 'PMS_ROLE'
    value: 'worker'
  }
])

var healthProbes = [
  {
    type: 'Startup'
    httpGet: {
      path: '/healthz'
      port: appPort
      scheme: 'HTTP'
    }
    initialDelaySeconds: 5
    periodSeconds: 10
    timeoutSeconds: 5
    failureThreshold: 10
  }
  {
    type: 'Liveness'
    httpGet: {
      path: '/healthz'
      port: appPort
      scheme: 'HTTP'
    }
    periodSeconds: 30
    timeoutSeconds: 5
    failureThreshold: 3
  }
  {
    type: 'Readiness'
    httpGet: {
      path: '/healthz'
      port: appPort
      scheme: 'HTTP'
    }
    periodSeconds: 10
    timeoutSeconds: 5
    failureThreshold: 3
  }
]

var containerResources = {
  cpu: json('0.5')
  memory: '1Gi'
}

module webImage 'fetch-container-image.bicep' = {
  name: 'pms-web-image'
  params: {
    exists: webExists
    name: webAppName
    fallbackImage: placeholderImage
  }
}

module workerImage 'fetch-container-image.bicep' = {
  name: 'pms-worker-image'
  params: {
    exists: workerExists
    name: workerAppName
    fallbackImage: placeholderImage
  }
}

resource web 'Microsoft.App/containerApps@2024-03-01' = {
  name: webAppName
  location: location
  tags: union(tags, {
    'azd-service-name': 'web'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: appPort
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: webImage.outputs.image
          env: webEnv
          resources: containerResources
          probes: healthProbes
        }
      ]
      scale: {
        minReplicas: webMinReplicas
        maxReplicas: webMaxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
}

// Sign-in with Microsoft Entra ID is required for every path except the health endpoint.
resource webAuth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: web
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: [
        '/healthz'
      ]
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: entraAllowedAudiences
        }
      }
    }
    login: {
      preserveUrlFragmentsForLogins: false
    }
    httpSettings: {
      requireHttps: true
    }
  }
}

resource worker 'Microsoft.App/containerApps@2024-03-01' = {
  name: workerAppName
  location: location
  tags: union(tags, {
    'azd-service-name': 'worker'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: workerImage.outputs.image
          env: workerEnv
          resources: containerResources
          probes: workerHealthProbes ? healthProbes : []
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output webName string = web.name
output workerName string = worker.name
output webFqdn string = web.properties.configuration.ingress.fqdn
