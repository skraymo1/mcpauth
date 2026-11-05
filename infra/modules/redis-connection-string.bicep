// Builds the StackExchange.Redis-style connection string that APIM's external cache needs. The key is read
// in this module (listKeys needs a name known at the start of the deployment) and leaves only as a secure output.
targetScope = 'resourceGroup'

@description('Name of the Azure Managed Redis cluster.')
param redisName string

@description('Host name of the cluster (redis-token-cache.bicep output).')
param redisFqdn string

resource redis 'Microsoft.Cache/redisEnterprise@2025-07-01' existing = {
  name: redisName

  resource database 'databases' existing = {
    name: 'default'
  }
}

@secure()
output connectionString string = '${redisFqdn}:10000,password=${redis::database.listKeys().primaryKey},ssl=True,abortConnect=False'
