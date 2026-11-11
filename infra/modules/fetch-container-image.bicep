// Returns the image of an existing container app, or the fallback image on the first provision.
// azd sets SERVICE_<NAME>_RESOURCE_EXISTS, so a re-provision keeps the image deployed by 'azd deploy'.
param exists bool
param name string
param fallbackImage string

resource existingApp 'Microsoft.App/containerApps@2024-03-01' existing = if (exists) {
  name: name
}

output image string = exists ? existingApp.properties.template.containers[0].image : fallbackImage
