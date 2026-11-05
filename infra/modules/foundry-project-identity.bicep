// Reads the Foundry project's managed identity. The foundry-project module exports the account's identity but
// not the project's, and the project identity is the one that calls the gateway (and gets the Mcp.Invoke role).
targetScope = 'resourceGroup'

@description('Name of the AI Services account that hosts the project.')
param accountName string

@description('Name of the Foundry project.')
param projectName string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName

  resource project 'projects' existing = {
    name: projectName
  }
}

output principalId string = account::project.identity.principalId
