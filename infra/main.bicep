// Azure resources are deployed by Bicep from one `azd up`; the pre/postprovision hooks manage the Entra registrations
// and the Foundry agent as the signed-in az user. The AWS side is a mock inside API Management, so nothing is
// deployed to AWS.
//
//   Azure resources     Foundry account, project and model deployment   (foundry-project\infra\core\ai\ai-project.bicep)
//                       API Management bridge, its Key Vault and named values, the optional AWS mock
//                                                                       (m2m-gateway\azure-gateway.bicep)
//                       Managed Redis token cache and its APIM registration (m2m-gateway\redis-token-cache.bicep)
//                       Foundry project connection to the gateway (managed identity)
//                       Chat app platform: Log Analytics, ACR, Container Apps, identity (entra-chat-demo\infra\modules)
//   Microsoft Entra ID  The gateway, chat API and chat web app registrations, service principals and role
//                       assignments, managed by infra\hooks with the signed-in user's az token
//   Secrets             The chat app's client secret and the mock Cognito secrets are created by the preprovision hook,
//                       kept in the azd environment and passed in below as secure parameters
//   Foundry agent       Created by the postprovision hook through the project's data plane (infra\hooks\create-agent.ps1)
//
// There are no deployment scripts: they need storage account shared keys, which many subscriptions forbid by policy.
// The standalone templates are used unchanged as modules, so each demo still deploys on its own.
// The README explains the required Entra permissions, what is not verified and how the deployment is staged.
targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('Name of the azd environment. It names the resource group and keeps resource names unique.')
param environmentName string

// usageName makes azd offer only regions that have quota for the model deployment (capacity 30).
// The region must also support Foundry, APIM Consumption, Azure Managed Redis, and Container Apps.
@minLength(1)
@description('Region for all resources.')
@metadata({
  azd: {
    type: 'location'
    usageName: [
      'OpenAI.GlobalStandard.gpt-5-mini,30'
    ]
  }
})
param location string

@description('Object ID of the user or service principal running the deployment. Set by azd.')
param principalId string

@description('Principal type of principalId. Set by azd. The chat demo roles are assigned only to a User.')
param principalType string = 'User'

@description('Application (client) ID of the gateway API registration created by the preprovision hook.')
param gatewayApiClientId string

@description('Application (client) ID of the chat API registration created by the preprovision hook.')
param chatApiClientId string

@description('Application (client) ID of the chat web registration created by the preprovision hook.')
param chatWebClientId string

@minLength(5)
@description('Publisher email that API Management requires. Run: azd env set APIM_PUBLISHER_EMAIL you@example.com')
param publisherEmail string

@description('Name shown as publisher in API Management.')
param publisherName string = 'M2M MCP demo'

@minLength(1)
@description('Client secret created for the chat web app by the preprovision hook using the signed-in user.')
@secure()
param oidcClientSecret string

// ---- AWS side. The default is a mock inside APIM. The cognito* and awsMcpOrigin values only matter when you point
// ---- the bridge at a real Amazon Cognito and MCP server that you deployed yourself (m2m-gateway\cognito.yaml).

@description('Route the bridge to a mock Cognito and AWS MCP server inside APIM (fictional data). Set false only when you supply the cognito* and awsMcpOrigin values for a real AWS stack.')
param deployMockAws bool = true

@description('Amazon Cognito token endpoint (TokenEndpoint output of cognito.yaml). Required when deployMockAws is false.')
param cognitoTokenUrl string = ''

@description('Cognito client ID for the Foundry route. Required when deployMockAws is false.')
param cognitoFoundryClientId string = ''

@description('Cognito client ID for the Copilot Studio route. Required when deployMockAws is false.')
param cognitoCopilotClientId string = ''

@description('Cognito client secret for the Foundry route. Required when deployMockAws is false; the mock ignores it.')
@secure()
param cognitoFoundryClientSecret string = ''

@description('Cognito client secret for the Copilot Studio route. Required when deployMockAws is false; the mock ignores it.')
@secure()
param cognitoCopilotClientSecret string = ''

@description('Secret that the mock Cognito accepts for the Foundry route. The preprovision hook generates it. Used only when deployMockAws is true.')
@secure()
param mockFoundryClientSecret string = ''

@description('Secret that the mock Cognito accepts for the Copilot Studio route. The preprovision hook generates it. Used only when deployMockAws is true.')
@secure()
param mockCopilotClientSecret string = ''

@description('Origin of the AWS-hosted MCP server, without /mcp. Required when deployMockAws is false.')
param awsMcpOrigin string = ''

@description('Deploy Azure Managed Redis as the shared token cache (ongoing cost). Without it APIM requests a Cognito token per call.')
param enableTokenCache bool = true

// ---- Foundry and model options

@description('Name of the model deployment that the agent and the chat app both use.')
param modelDeploymentName string = 'gpt-5-mini'
param modelName string = 'gpt-5-mini'
param modelVersion string = '2025-08-07'

@description('Capacity in thousands of tokens per minute.')
param modelCapacity int = 30

@description('Create Application Insights and Log Analytics for the Foundry project.')
param enableFoundryMonitoring bool = true

@description('Name of the Foundry project connection that points at the gateway.')
param foundryMcpConnectionName string = 'aws-inventory-mcp'

@description('Name of the Foundry agent that the postprovision hook creates.')
param agentName string = 'aws-inventory-agent'

@description('"always" makes the agent ask for approval before each MCP tool call; "never" lets it call the tool without asking. It applies when the agent is first created; an existing agent is left as it is.')
param agentRequireApproval string = 'always'

@description('Set by azd: true once the web service has been deployed, so provisioning keeps its image.')
param webExists bool = false

var tags = { 'azd-env-name': environmentName }
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var foundryProjectName = 'ai-project-${environmentName}'
var appName = 'ca-web-${resourceToken}'
var webUrl = 'https://${appName}.${platform.outputs.environmentDefaultDomain}'
var mockFoundryClientId = 'mock-foundry-m2m-client'
var mockCopilotClientId = 'mock-copilot-m2m-client'
var requireApproval = agentRequireApproval == 'never' ? 'never' : 'always'

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

// ---- Foundry

module foundry '../foundry-project/infra/core/ai/ai-project.bicep' = {
  name: 'foundry'
  scope: rg
  params: {
    tags: tags
    location: location
    aiFoundryProjectName: foundryProjectName
    deployments: [
      {
        name: modelDeploymentName
        model: {
          name: modelName
          format: 'OpenAI'
          version: modelVersion
        }
        sku: {
          name: 'GlobalStandard'
          capacity: modelCapacity
        }
      }
    ]
    principalId: principalId
    principalType: principalType
    additionalDependentResources: []
    enableMonitoring: enableFoundryMonitoring
    enableHostedAgents: false
    enableCapabilityHost: false
  }
}

module foundryIdentity 'modules/foundry-project-identity.bicep' = {
  name: 'foundry-project-identity'
  scope: rg
  params: {
    accountName: foundry.outputs.aiServicesAccountName
    projectName: foundry.outputs.projectName
  }
}

// ---- Entra chat app platform (first, because the chat web registration needs the app's URL)

module platform '../entra-chat-demo/infra/modules/platform.bicep' = {
  name: 'platform'
  scope: rg
  params: {
    resourceToken: resourceToken
    location: location
    tags: tags
  }
}

// ---- APIM bridge, Key Vault, optional AWS mock, Redis token cache

module gateway 'modules/m2m-gateway.bicep' = {
  name: 'm2m-gateway'
  scope: rg
  params: {
    location: location
    publisherEmail: publisherEmail
    publisherName: publisherName
    entraApiClientId: gatewayApiClientId
    foundryPrincipalId: foundryIdentity.outputs.principalId
    cognitoTokenUrl: cognitoTokenUrl
    cognitoFoundryClientId: deployMockAws ? mockFoundryClientId : cognitoFoundryClientId
    cognitoCopilotClientId: deployMockAws ? mockCopilotClientId : cognitoCopilotClientId
    // The module requires at least 32 characters, so a missing secret fails validation instead of reaching APIM.
    cognitoFoundryClientSecret: deployMockAws ? mockFoundryClientSecret : cognitoFoundryClientSecret
    cognitoCopilotClientSecret: deployMockAws ? mockCopilotClientSecret : cognitoCopilotClientSecret
    awsMcpOrigin: awsMcpOrigin
    deployMockAws: deployMockAws
    enableTokenCache: enableTokenCache
  }
}

// The agent's MCP tool reads this connection: the project's managed identity requests a token for the gateway API.
module foundryMcpConnection '../foundry-project/infra/core/ai/connection.bicep' = {
  name: 'foundry-mcp-connection'
  scope: rg
  params: {
    aiServicesAccountName: foundry.outputs.aiServicesAccountName
    aiProjectName: foundry.outputs.projectName
    connectionConfig: {
      name: foundryMcpConnectionName
      category: 'RemoteTool'
      target: gateway.outputs.foundryMcpUrl
      authType: 'ProjectManagedIdentity'
      isSharedToAll: true
      audience: 'api://${gatewayApiClientId}'
    }
  }
}

// ---- Entra chat app (reuses the Foundry account and model deployment; no keys)

module openAi '../entra-chat-demo/infra/modules/openai.bicep' = {
  name: 'openai'
  scope: rg
  params: {
    accountName: foundry.outputs.aiServicesAccountName
    create: false
    location: location
    tags: tags
    deploymentName: modelDeploymentName
    modelName: modelName
    modelVersion: modelVersion
    modelCapacity: modelCapacity
    principalId: platform.outputs.identityPrincipalId
  }
}

module web '../entra-chat-demo/infra/modules/containerapp.bicep' = {
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
    webUrl: webUrl
    entraTenantId: tenant().tenantId
    oidcClientId: chatWebClientId
    apiAudience: chatApiClientId
    oidcClientSecret: oidcClientSecret
    openAiEndpoint: openAi.outputs.endpoint
    openAiDeployment: modelDeploymentName
  }
}

// azd writes every output to the environment (.azure\<env>\.env), where the README tells you to read them.
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = platform.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = platform.outputs.registryName
output SERVICE_WEB_NAME string = web.outputs.name
output WEB_URL string = 'https://${web.outputs.fqdn}'
output AZURE_OPENAI_ENDPOINT string = openAi.outputs.endpoint
output AZURE_OPENAI_DEPLOYMENT string = modelDeploymentName

output AZURE_AI_ACCOUNT_NAME string = foundry.outputs.aiServicesAccountName
output AZURE_AI_PROJECT_NAME string = foundry.outputs.projectName
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.FOUNDRY_PROJECT_ENDPOINT
output FOUNDRY_PROJECT_PRINCIPAL_ID string = foundryIdentity.outputs.principalId
output FOUNDRY_MCP_CONNECTION_NAME string = foundryMcpConnection.outputs.connectionName
output FOUNDRY_AGENT_NAME string = agentName
output FOUNDRY_AGENT_REQUIRE_APPROVAL string = requireApproval

output APIM_NAME string = gateway.outputs.apimName
output APIM_GATEWAY_URL string = gateway.outputs.gatewayUrl
output APIM_FOUNDRY_MCP_URL string = gateway.outputs.foundryMcpUrl
output APIM_COPILOT_MCP_URL string = gateway.outputs.copilotMcpUrl
output APIM_COPILOT_SUBSCRIPTION_NAME string = gateway.outputs.copilotSubscriptionName
output KEY_VAULT_NAME string = gateway.outputs.keyVaultName
output REDIS_NAME string = gateway.outputs.redisName

output ENTRA_GATEWAY_API_CLIENT_ID string = gatewayApiClientId
output ENTRA_CHAT_API_CLIENT_ID string = chatApiClientId
output ENTRA_CHAT_WEB_CLIENT_ID string = chatWebClientId
