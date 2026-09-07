targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('Deployment environment name used for deterministic resource naming.')
param environmentName string = 'production'

@allowed([
  'centralus'
  'eastasia'
  'eastus2'
  'westeurope'
  'westus2'
])
@description('Azure Static Web Apps and Function App region.')
param location string = 'eastasia'

@description('Existing Foundry hosted-agent Responses endpoint.')
param foundryAgentEndpoint string

@description('Optional resource group name.')
param resourceGroupName string = ''

@description('Optional principal ID that may deploy Function packages during bootstrap.')
param deployerPrincipalId string = deployer().objectId

var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = {
  app: 'strong-loop'
  'azd-env-name': environmentName
  'managed-by': 'bicep'
}
var rgName = !empty(resourceGroupName) ? resourceGroupName : 'rg-strong-loop-web-${environmentName}'
var staticWebAppName = 'stapp-strong-loop-${resourceToken}'
var functionAppName = 'func-strong-loop-${resourceToken}'
var functionPlanName = 'plan-strong-loop-${resourceToken}'
var identityName = 'id-strong-loop-api-${resourceToken}'
var storageName = 'st${take(replace(resourceToken, '-', ''), 18)}loop'
var logAnalyticsName = 'log-strong-loop-${resourceToken}'
var appInsightsName = 'appi-strong-loop-${resourceToken}'
var deploymentStorageContainerName = 'app-package-${take(resourceToken, 20)}'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: rgName
  location: location
  tags: tags
}

module apiIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.1' = {
  name: 'api-identity'
  scope: rg
  params: {
    name: identityName
    location: location
    tags: tags
  }
}

module functionPlan 'br/public:avm/res/web/serverfarm:0.1.1' = {
  name: 'function-plan'
  scope: rg
  params: {
    name: functionPlanName
    location: location
    reserved: true
    sku: {
      name: 'FC1'
      tier: 'FlexConsumption'
    }
    tags: tags
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.8.3' = {
  name: 'storage'
  scope: rg
  params: {
    name: storageName
    location: location
    tags: tags
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    dnsEndpointType: 'Standard'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    blobServices: {
      containers: [
        { name: deploymentStorageContainerName }
      ]
    }
    minimumTlsVersion: 'TLS1_2'
  }
}

module logAnalytics 'br/public:avm/res/operational-insights/workspace:0.11.1' = {
  name: 'log-analytics'
  scope: rg
  params: {
    name: logAnalyticsName
    location: location
    tags: tags
    dataRetention: 30
  }
}

module monitoring 'br/public:avm/res/insights/component:0.6.0' = {
  name: 'application-insights'
  scope: rg
  params: {
    name: appInsightsName
    location: location
    tags: tags
    workspaceResourceId: logAnalytics.outputs.resourceId
    disableLocalAuth: true
  }
}

module api './app/api.bicep' = {
  name: 'api'
  scope: rg
  params: {
    name: functionAppName
    location: location
    tags: tags
    applicationInsightsName: monitoring.outputs.name
    appServicePlanId: functionPlan.outputs.resourceId
    runtimeName: 'node'
    runtimeVersion: '22'
    storageAccountName: storage.outputs.name
    deploymentStorageContainerName: deploymentStorageContainerName
    identityType: 'UserAssigned'
    identityId: apiIdentity.outputs.resourceId
    identityClientId: apiIdentity.outputs.clientId
    instanceMemoryMB: 2048
    maximumInstanceCount: 10
    appSettings: {
      AZURE_CLIENT_ID: apiIdentity.outputs.clientId
      FOUNDRY_AGENT_ENDPOINT: foundryAgentEndpoint
    }
  }
}

module storageRbac './app/rbac.bicep' = {
  name: 'storage-rbac'
  scope: rg
  params: {
    storageAccountName: storage.outputs.name
    appInsightsName: monitoring.outputs.name
    managedIdentityPrincipalId: apiIdentity.outputs.principalId
    userIdentityPrincipalId: deployerPrincipalId
    enableBlob: true
    enableQueue: true
    enableTable: true
    allowUserIdentityPrincipal: true
  }
}

module web './app/web.bicep' = {
  name: 'web'
  scope: rg
  params: {
    name: staticWebAppName
    location: location
    tags: tags
    backendResourceId: api.outputs.SERVICE_API_RESOURCE_ID
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output SERVICE_API_NAME string = api.outputs.SERVICE_API_NAME
output SERVICE_API_IDENTITY_CLIENT_ID string = apiIdentity.outputs.clientId
output SERVICE_API_IDENTITY_PRINCIPAL_ID string = apiIdentity.outputs.principalId
output SERVICE_WEB_NAME string = web.outputs.name
output SERVICE_WEB_URI string = web.outputs.uri
