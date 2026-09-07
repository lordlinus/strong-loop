param name string
param planName string
param location string
param tags object
param identityId string
param identityClientId string
param foundryAgentEndpoint string
param applicationInsightsConnectionString string

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  kind: 'linux'
  tags: tags
  sku: {
    name: 'B1'
    tier: 'Basic'
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: name
  location: location
  kind: 'app,linux'
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      alwaysOn: true
      linuxFxVersion: 'NODE|22-lts'
      appCommandLine: 'npm start'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/api/health'
    }
  }
}

resource settings 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: app
  name: 'appsettings'
  properties: {
    APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsightsConnectionString
    AZURE_CLIENT_ID: identityClientId
    ENABLE_ORYX_BUILD: 'true'
    FOUNDRY_AGENT_ENDPOINT: foundryAgentEndpoint
    SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
  }
}

output name string = app.name
output resourceId string = app.id
output uri string = 'https://${app.properties.defaultHostName}'
