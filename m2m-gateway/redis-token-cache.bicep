targetScope = 'resourceGroup'

@description('Azure region for the Redis cache. Use the same region as the APIM gateway.')
param location string = resourceGroup().location

var suffix = take(uniqueString(resourceGroup().id), 6)
var redisName = 'redis-m2mmcp-${suffix}'

resource redis 'Microsoft.Cache/redisEnterprise@2025-07-01' = {
  name: redisName
  location: location
  sku: {
    name: 'Balanced_B0'
  }
  properties: {
    highAvailability: 'Enabled'
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    encryption: {}
  }
}

resource database 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' = {
  parent: redis
  name: 'default'
  properties: {
    accessKeysAuthentication: 'Enabled'
    clientProtocol: 'Encrypted'
    clusteringPolicy: 'OSSCluster'
    evictionPolicy: 'VolatileLRU'
    modules: []
    port: 10000
  }
}

output redisName string = redis.name
output redisFqdn string = '${redis.name}.${location}.redis.azure.net'
output redisResourceId string = redis.id
