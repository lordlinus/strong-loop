param name string
param location string
param tags object = {}
param backendResourceId string

resource staticWebApp 'Microsoft.Web/staticSites@2023-12-01' = {
  name: name
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    allowConfigFileUpdates: true
    buildProperties: {
      appLocation: 'docs'
      apiLocation: ''
      outputLocation: ''
    }
  }
}

resource linkedBackend 'Microsoft.Web/staticSites/linkedBackends@2022-09-01' = {
  parent: staticWebApp
  name: 'production'
  properties: {
    backendResourceId: backendResourceId
    region: location
  }
}

output name string = staticWebApp.name
output uri string = 'https://${staticWebApp.properties.defaultHostname}'
