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

resource existingApp 'Microsoft.App/containerApps@2024-03-01' existing = if (exists) {
  name: name
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  // azd matches the "web" service in azure.yaml to this app through the tag.
  tags: union(tags, { 'azd-service-name': 'web' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
      secrets: [
        {
          name: 'oidc-client-secret'
          value: oidcClientSecret
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: exists ? existingApp!.properties.template.containers[0].image : placeholderImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'AUTH_MODE', value: 'entra' }
            { name: 'ENTRA_TENANT_ID', value: entraTenantId }
            { name: 'OIDC_CLIENT_ID', value: oidcClientId }
            { name: 'OIDC_CLIENT_SECRET', secretRef: 'oidc-client-secret' }
            { name: 'API_AUDIENCE', value: apiAudience }
            { name: 'WEB_URL', value: webUrl }
            { name: 'WEB_PORT', value: '8000' }
            { name: 'WEB_BIND_HOST', value: '0.0.0.0' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: openAiEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: openAiDeployment }
            { name: 'MODEL_IDENTITY_CLIENT_ID', value: identityClientId }
          ]
        }
      ]
      // Sessions and tokens live in this process's memory, so run exactly one replica.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output name string = app.name
output fqdn string = app.properties.configuration.ingress.fqdn
