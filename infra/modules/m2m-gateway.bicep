// The whole Azure side of the M2M demo in one module: the APIM bridge with Key Vault (azure-gateway.bicep),
// then optionally the Managed Redis token cache and its registration in APIM.
// It composes the standalone templates unchanged, so they keep working on their own.
targetScope = 'resourceGroup'

param location string
@minLength(5)
param publisherEmail string
param publisherName string
@minLength(36)
@maxLength(36)
@description('Application (client) ID of the gateway API registration (created by the preprovision hook). APIM validates it as the token audience.')
param entraApiClientId string
param foundryPrincipalId string
param cognitoTokenUrl string
param cognitoFoundryClientId string
param cognitoCopilotClientId string
@secure()
@minLength(32)
param cognitoFoundryClientSecret string
@secure()
@minLength(32)
param cognitoCopilotClientSecret string
param awsMcpOrigin string
param deployMockAws bool

@description('Also deploy Azure Managed Redis and register it as the APIM external cache for Cognito tokens.')
param enableTokenCache bool

module gateway '../../m2m-gateway/azure-gateway.bicep' = {
  name: 'azure-gateway'
  params: {
    location: location
    publisherEmail: publisherEmail
    publisherName: publisherName
    entraApiClientId: entraApiClientId
    foundryPrincipalId: foundryPrincipalId
    cognitoTokenUrl: cognitoTokenUrl
    cognitoFoundryClientId: cognitoFoundryClientId
    cognitoCopilotClientId: cognitoCopilotClientId
    cognitoFoundryClientSecret: cognitoFoundryClientSecret
    cognitoCopilotClientSecret: cognitoCopilotClientSecret
    awsMcpOrigin: awsMcpOrigin
    deployMockAws: deployMockAws
  }
}

module redis '../../m2m-gateway/redis-token-cache.bicep' = if (enableTokenCache) {
  name: 'redis-token-cache'
  params: {
    location: location
  }
}

module redisConnection 'redis-connection-string.bicep' = if (enableTokenCache) {
  name: 'redis-connection-string'
  params: {
    redisName: redis!.outputs.redisName
    redisFqdn: redis!.outputs.redisFqdn
  }
}

module externalCache '../../m2m-gateway/apim-external-cache.bicep' = if (enableTokenCache) {
  name: 'apim-external-cache'
  params: {
    apiManagementName: gateway.outputs.apimName
    redisResourceId: redis!.outputs.redisResourceId
    // Secure outputs cannot use the "!" access; the module exists whenever this one does.
    #disable-next-line BCP318
    redisConnectionString: redisConnection.outputs.connectionString
    useFromLocation: location
  }
}

output apimName string = gateway.outputs.apimName
output apimPrincipalId string = gateway.outputs.apimPrincipalId
output gatewayUrl string = gateway.outputs.gatewayUrl
output foundryMcpUrl string = gateway.outputs.foundryMcpUrl
output copilotMcpUrl string = gateway.outputs.copilotMcpUrl
output keyVaultName string = gateway.outputs.keyVaultName
output copilotSubscriptionName string = gateway.outputs.copilotSubscriptionName
output mockAwsOrigin string = gateway.outputs.mockAwsOrigin
output redisName string = enableTokenCache ? redis!.outputs.redisName : ''
