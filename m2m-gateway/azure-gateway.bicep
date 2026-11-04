// Azure side of the cross-cloud MCP M2M demo: the APIM bridge and its Key Vault.
// Deploy into the demo resource group after the Entra gateway API registration exists.
// Until the AWS stack exists, either keep the Cognito and AWS placeholders (calls stop at the AWS hop with 502)
// or set deployMockAws=true to route the unchanged bridge policy to a mock Cognito + MCP API in the same APIM.
targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Publisher email required by API Management.')
param publisherEmail string

@description('Publisher name shown in API Management.')
param publisherName string = 'M2M MCP demo'

@description('Application (client) ID of the Entra app registration that represents the APIM gateway API.')
param entraApiClientId string

@description('Principal (object) ID of the Foundry project managed identity.')
param foundryPrincipalId string

@description('Amazon Cognito token endpoint (TokenEndpoint output of cognito.yaml).')
param cognitoTokenUrl string = 'https://replace-with-cognito-domain.auth.us-east-1.amazoncognito.com/oauth2/token'

@description('Cognito app client ID used for Foundry traffic (FoundryClientId output).')
param cognitoFoundryClientId string = 'replace-with-cognito-foundry-client-id'

@description('Cognito app client ID used for Copilot Studio traffic (CopilotClientId output).')
param cognitoCopilotClientId string = 'replace-with-cognito-copilot-client-id'

@secure()
@description('Cognito Foundry app client secret, stored in Key Vault.')
param cognitoFoundryClientSecret string

@secure()
@description('Cognito Copilot app client secret, stored in Key Vault.')
param cognitoCopilotClientSecret string

@description('Origin of the AWS-hosted MCP server, without /mcp.')
param awsMcpOrigin string = 'https://mcp.aws.example.com'

@description('Demo only: add a mock of Amazon Cognito and the AWS MCP server to this APIM (fictional data) and point the bridge at it. Overrides cognitoTokenUrl and awsMcpOrigin.')
param deployMockAws bool = false

var suffix = take(uniqueString(resourceGroup().id), 6)
var apimName = 'apim-m2mmcp-${suffix}'
var keyVaultName = 'kv-m2mmcp-${suffix}'
// Consumption gateways use the default hostname; computed here so it is known before APIM exists.
var mockAwsOrigin = 'https://${apimName}.azure-api.net/mock-aws'
var effectiveCognitoTokenUrl = deployMockAws ? '${mockAwsOrigin}/oauth2/token' : cognitoTokenUrl
var effectiveAwsMcpOrigin = deployMockAws ? mockAwsOrigin : awsMcpOrigin
var copilotSubscriptionName = 'copilot-studio-mcp'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var policyXml = loadTextContent('apim-policy.xml')
var mcpOperations = [
  { name: 'mcp-post', method: 'POST' }
  { name: 'mcp-get', method: 'GET' }
  { name: 'mcp-delete', method: 'DELETE' }
]
var plainNamedValues = {
  'entra-tenant-id': tenant().tenantId
  'entra-api-client-id': entraApiClientId
  'foundry-principal-id': foundryPrincipalId
  'copilot-subscription-id': copilotSubscriptionName
  'cognito-token-url': effectiveCognitoTokenUrl
  'cognito-foundry-client-id': cognitoFoundryClientId
  'cognito-copilot-client-id': cognitoCopilotClientId
  'aws-mcp-origin': effectiveAwsMcpOrigin
}
var mockAwsOperations = [
  { name: 'mock-token', method: 'POST', urlTemplate: '/oauth2/token' }
  { name: 'mock-mcp-post', method: 'POST', urlTemplate: '/mcp' }
  { name: 'mock-mcp-get', method: 'GET', urlTemplate: '/mcp' }
  { name: 'mock-mcp-delete', method: 'DELETE', urlTemplate: '/mcp' }
]

resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: location
  sku: {
    name: 'Consumption'
    capacity: 0
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource foundrySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'cognito-foundry-client-secret'
  properties: {
    value: cognitoFoundryClientSecret
  }
}

resource copilotSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'cognito-copilot-client-secret'
  properties: {
    value: cognitoCopilotClientSecret
  }
}

// APIM's managed identity can read only these two secrets.
resource foundrySecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundrySecret.id, apim.id, keyVaultSecretsUserRoleId)
  scope: foundrySecret
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource copilotSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(copilotSecret.id, apim.id, keyVaultSecretsUserRoleId)
  scope: copilotSecret
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: apim.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource namedValues 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = [
  for nv in items(plainNamedValues): {
    parent: apim
    name: nv.key
    properties: {
      displayName: nv.key
      value: nv.value
      secret: false
    }
  }
]

resource foundrySecretNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'cognito-foundry-client-secret'
  properties: {
    displayName: 'cognito-foundry-client-secret'
    secret: true
    keyVault: {
      secretIdentifier: foundrySecret.properties.secretUri
    }
  }
  dependsOn: [
    foundrySecretReader
  ]
}

resource copilotSecretNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'cognito-copilot-client-secret'
  properties: {
    displayName: 'cognito-copilot-client-secret'
    secret: true
    keyVault: {
      secretIdentifier: copilotSecret.properties.secretUri
    }
  }
  dependsOn: [
    copilotSecretReader
  ]
}

resource foundryApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'foundry-mcp'
  properties: {
    displayName: 'Foundry to AWS MCP'
    description: 'Transparent MCP proxy for the Foundry project managed identity.'
    path: 'foundry'
    protocols: [
      'https'
    ]
    serviceUrl: effectiveAwsMcpOrigin
    subscriptionRequired: false
  }
}

resource copilotApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'copilot-mcp'
  properties: {
    displayName: 'Copilot Studio to AWS MCP'
    description: 'Transparent MCP proxy for the Copilot Studio API-key connection.'
    path: 'copilot'
    protocols: [
      'https'
    ]
    serviceUrl: effectiveAwsMcpOrigin
    subscriptionRequired: true
    subscriptionKeyParameterNames: {
      header: 'Ocp-Apim-Subscription-Key'
      query: 'subscription-key'
    }
  }
}

resource foundryOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for op in mcpOperations: {
    parent: foundryApi
    name: op.name
    properties: {
      displayName: '${op.method} /mcp'
      method: op.method
      urlTemplate: '/mcp'
    }
  }
]

resource copilotOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for op in mcpOperations: {
    parent: copilotApi
    name: op.name
    properties: {
      displayName: '${op.method} /mcp'
      method: op.method
      urlTemplate: '/mcp'
    }
  }
]

// The same policy serves both APIs and branches on the trusted API identifier.
resource foundryPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: foundryApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: policyXml
  }
  dependsOn: [
    namedValues
    foundrySecretNamedValue
    copilotSecretNamedValue
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
    namedValues
    foundrySecretNamedValue
    copilotSecretNamedValue
  ]
}

// API-scoped subscription for the Copilot Studio connection only.
resource copilotSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = {
  parent: apim
  name: copilotSubscriptionName
  properties: {
    displayName: 'Copilot Studio MCP connector'
    scope: '/apis/${copilotApi.name}'
    state: 'active'
    allowTracing: false
  }
}

// Demo only: mock Cognito token endpoint + mock AWS MCP server (see apim-mock-aws-policy.xml).
resource mockAwsApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (deployMockAws) {
  parent: apim
  name: 'mock-aws'
  properties: {
    displayName: 'MOCK AWS (Cognito + MCP) - demo only'
    description: 'Stands in for Amazon Cognito and the AWS-hosted MCP server. Fictional data. Remove when the AWS stack exists.'
    path: 'mock-aws'
    protocols: [
      'https'
    ]
    subscriptionRequired: false
  }
}

resource mockAwsApiOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for op in mockAwsOperations: if (deployMockAws) {
    parent: mockAwsApi
    name: op.name
    properties: {
      displayName: '${op.method} ${op.urlTemplate}'
      method: op.method
      urlTemplate: op.urlTemplate
    }
  }
]

resource mockAwsPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = if (deployMockAws) {
  parent: mockAwsApi
  name: 'policy'
  properties: {
    format: 'xml'
    value: loadTextContent('apim-mock-aws-policy.xml')
  }
  dependsOn: [
    namedValues
    foundrySecretNamedValue
    copilotSecretNamedValue
    mockAwsApiOperations
  ]
}

output apimName string = apim.name
output apimPrincipalId string = apim.identity.principalId
output gatewayUrl string = apim.properties.gatewayUrl
output foundryMcpUrl string = '${apim.properties.gatewayUrl}/foundry/mcp'
output copilotMcpUrl string = '${apim.properties.gatewayUrl}/copilot/mcp'
output keyVaultName string = keyVault.name
output copilotSubscriptionName string = copilotSubscription.name
output mockAwsOrigin string = deployMockAws ? mockAwsOrigin : ''
