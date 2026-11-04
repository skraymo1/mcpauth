@description('Account to create, or the existing account to reuse. It is also the custom subdomain when created.')
param accountName string
@description('True creates the account and model deployment; false only grants access to an existing account.')
param create bool
param location string
param tags object
param deploymentName string
param modelName string
param modelVersion string
@description('Capacity in thousands of tokens per minute.')
param modelCapacity int
@description('Principal that calls the model.')
param principalId string

var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource created 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (create) {
  name: accountName
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (create) {
  parent: created
  name: deploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
  }
}

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: accountName
}

resource callModel 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, principalId, openAiUserRoleId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
  dependsOn: [
    created
  ]
}

output endpoint string = 'https://${account.properties.customSubDomainName}.openai.azure.com'
