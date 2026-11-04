targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('Name of the azd environment. It names the resource group and keeps resource names unique.')
param environmentName string

// usageName makes azd offer only regions that have quota for the default model deployment (capacity 30).
@minLength(1)
@description('Region for all resources. It must offer the model deployment unless you reuse an existing account.')
@metadata({
  azd: {
    type: 'location'
    usageName: [
      'OpenAI.GlobalStandard.gpt-5-mini,30'
    ]
  }
})
param location string

// The four parameters below have empty defaults only so azd does not prompt for them before the
// preprovision hook runs; the hook fills them in, and the web module rejects a deployment where it did not.
@description('Entra tenant that holds the demo app registrations. Set by the preprovision hook.')
param entraTenantId string = ''

@description('Client ID of the web app registration. Set by the preprovision hook.')
param oidcClientId string = ''

@description('Application ID of the API registration. Set by the preprovision hook.')
param apiAudience string = ''

@description('Client secret of the web app registration. Set by the preprovision hook.')
@secure()
param oidcClientSecret string = ''

@description('Optional resource ID of an existing Azure OpenAI or Foundry account to reuse instead of creating one.')
param existingOpenAiResourceId string = ''

@description('Name of the model deployment to create, or of the existing deployment to use.')
param modelDeploymentName string = 'gpt-5-mini'

@description('Used only when an account is created.')
param modelName string = 'gpt-5-mini'
param modelVersion string = '2025-08-07'

@description('Capacity in thousands of tokens per minute. Used only when an account is created.')
param modelCapacity int = 30

@description('Set by azd: true once the web service has been deployed, so provisioning keeps its image.')
param webExists bool = false

var tags = { 'azd-env-name': environmentName }
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var appName = 'ca-web-${resourceToken}'

var reuseOpenAi = !empty(existingOpenAiResourceId)
var existingOpenAiParts = split(existingOpenAiResourceId, '/')
var openAiSubscriptionId = reuseOpenAi ? existingOpenAiParts[2] : subscription().subscriptionId
var openAiResourceGroupName = reuseOpenAi ? existingOpenAiParts[4] : 'rg-${environmentName}'
var openAiAccountName = reuseOpenAi ? existingOpenAiParts[8] : 'oai-${resourceToken}'

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module platform 'modules/platform.bicep' = {
  name: 'platform'
  scope: rg
  params: {
    resourceToken: resourceToken
    location: location
    tags: tags
  }
}

module openAi 'modules/openai.bicep' = {
  name: 'openai'
  scope: resourceGroup(openAiSubscriptionId, openAiResourceGroupName)
  params: {
    accountName: openAiAccountName
    create: !reuseOpenAi
    location: location
    tags: tags
    deploymentName: modelDeploymentName
    modelName: modelName
    modelVersion: modelVersion
    modelCapacity: modelCapacity
    principalId: platform.outputs.identityPrincipalId
  }
}

module web 'modules/containerapp.bicep' = {
  name: 'web'
  scope: rg
  params: {
    name: appName
    location: location
    tags: tags
    environmentId: platform.outputs.environmentId
    identityId: platform.outputs.identityId
    identityClientId: platform.outputs.identityClientId
    registryServer: platform.outputs.registryLoginServer
    exists: webExists
    webUrl: 'https://${appName}.${platform.outputs.environmentDefaultDomain}'
    entraTenantId: entraTenantId
    oidcClientId: oidcClientId
    apiAudience: apiAudience
    oidcClientSecret: oidcClientSecret
    openAiEndpoint: openAi.outputs.endpoint
    openAiDeployment: modelDeploymentName
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = platform.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = platform.outputs.registryName
output SERVICE_WEB_NAME string = web.outputs.name
output WEB_URL string = 'https://${web.outputs.fqdn}'
output AZURE_OPENAI_ENDPOINT string = openAi.outputs.endpoint
output AZURE_OPENAI_DEPLOYMENT string = modelDeploymentName
