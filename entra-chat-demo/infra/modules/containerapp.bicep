param name string
param location string
param tags object
param environmentId string
param identityId string
param identityClientId string
param registryServer string
@description('True once azd has deployed the app, so a re-provision keeps the deployed image.')
param exists bool

@description('Public URL of the app; it must equal the registered redirect URI host.')
param webUrl string
@minLength(1)
param entraTenantId string
@minLength(1)
param oidcClientId string
@minLength(1)
param apiAudience string
@secure()
@minLength(1)
param oidcClientSecret string
param openAiEndpoint string
param openAiDeployment string

var placeholderImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

// The deployed image is read here and the app is declared in a child module. ARM reports a circular dependency
// when one template both declares a Container App and reads that app's properties.
resource existingApp 'Microsoft.App/containerApps@2024-03-01' existing = if (exists) {
  name: name
}

module app 'containerapp-resource.bicep' = {
  name: '${deployment().name}-app'
  params: {
    name: name
    location: location
    tags: tags
    environmentId: environmentId
    identityId: identityId
    identityClientId: identityClientId
    registryServer: registryServer
    image: exists ? existingApp!.properties.template.containers[0].image : placeholderImage
    webUrl: webUrl
    entraTenantId: entraTenantId
    oidcClientId: oidcClientId
    apiAudience: apiAudience
    oidcClientSecret: oidcClientSecret
    openAiEndpoint: openAiEndpoint
    openAiDeployment: openAiDeployment
  }
}

output name string = app.outputs.name
output fqdn string = app.outputs.fqdn
