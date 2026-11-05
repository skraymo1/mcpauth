targetScope = 'resourceGroup'

@description('Name of the existing API Management instance.')
param apiManagementName string

@description('Resource ID of the Azure Managed Redis cluster. API Management requires an absolute URL, so an ARM ID that starts with / is expanded with the Resource Manager endpoint.')
param redisResourceId string

@secure()
@description('TLS Redis connection string with the Managed Redis access key.')
param redisConnectionString string

@description('APIM region that should use the external cache.')
param useFromLocation string = resourceGroup().location

var policyXml = loadTextContent('apim-policy.xml')
var redisResourceUrl = startsWith(redisResourceId, '/') ? '${environment().resourceManager}${substring(redisResourceId, 1)}' : redisResourceId

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apiManagementName
}

resource foundryApi 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = {
  parent: apim
  name: 'foundry-mcp'
}

resource copilotApi 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = {
  parent: apim
  name: 'copilot-mcp'
}

resource externalCache 'Microsoft.ApiManagement/service/caches@2024-05-01' = {
  parent: apim
  name: 'aws-token-cache'
  properties: {
    connectionString: redisConnectionString
    description: 'External Redis cache for Cognito access tokens'
    resourceId: redisResourceUrl
    useFromLocation: useFromLocation
  }
}

resource foundryPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: foundryApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: policyXml
  }
  dependsOn: [
    externalCache
  ]
}

resource copilotPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: copilotApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: policyXml
  }
  dependsOn: [
    externalCache
  ]
}
