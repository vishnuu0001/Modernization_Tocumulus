from __future__ import annotations

import re


def requested_bicep(user_prompt: str, deployment_kind: str = "") -> bool:
    text = f"{user_prompt}\n{deployment_kind}".casefold()
    return bool(re.search(r"\bbicep\b|azure\s+infrastructure\s+as\s+code", text))


def azure_bicep_scaffold(project_name: str) -> dict[str, str]:
    """Return a bounded, deterministic Azure platform Bicep deployment.

    The scaffold is deliberately independent of the generated application
    language. Application-specific image tags, secrets and identity bindings
    remain deployment parameters instead of being invented by an LLM.
    """
    normalized = re.sub(r"[^a-z0-9]", "", project_name.casefold())[:18] or "application"
    main = """targetScope = 'subscription'

@description('Azure region for the platform resources.')
param location string = deployment().location

@minLength(2)
@maxLength(12)
@description('Short environment name such as dev, test, or prod.')
param environmentName string

@description('Stable lowercase workload identifier.')
param workloadName string

@description('Microsoft Entra object ID used as the Azure SQL administrator.')
param sqlEntraAdminObjectId string

@description('Kubernetes version approved for the target AKS region.')
param kubernetesVersion string

@allowed([
  'Standard_D4s_v5'
  'Standard_D8s_v5'
])
param aksNodeVmSize string = 'Standard_D4s_v5'

@minValue(2)
@maxValue(10)
param aksSystemNodeCount int = 3

var suffix = uniqueString(subscription().subscriptionId, workloadName, environmentName)
var resourceGroupName = '${workloadName}-${environmentName}-rg'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    workload: workloadName
    environment: environmentName
    managedBy: 'bicep'
  }
}

module platform './modules/platform.bicep' = {
  name: 'platform-${environmentName}'
  scope: resourceGroup
  params: {
    location: location
    environmentName: environmentName
    workloadName: workloadName
    suffix: suffix
    sqlEntraAdminObjectId: sqlEntraAdminObjectId
    kubernetesVersion: kubernetesVersion
    aksNodeVmSize: aksNodeVmSize
    aksSystemNodeCount: aksSystemNodeCount
  }
}

output resourceGroupName string = resourceGroup.name
output aksClusterName string = platform.outputs.aksClusterName
output containerRegistryName string = platform.outputs.containerRegistryName
output keyVaultName string = platform.outputs.keyVaultName
output sqlServerName string = platform.outputs.sqlServerName
output applicationGatewayPublicIp string = platform.outputs.applicationGatewayPublicIp
"""
    platform = """targetScope = 'resourceGroup'

param location string
param environmentName string
param workloadName string
param suffix string
param sqlEntraAdminObjectId string
param kubernetesVersion string
param aksNodeVmSize string
param aksSystemNodeCount int

var compactName = take(replace('${workloadName}${environmentName}', '-', ''), 12)
var commonTags = {
  workload: workloadName
  environment: environmentName
  managedBy: 'bicep'
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${workloadName}-${environmentName}-law'
  location: location
  tags: commonTags
  properties: {
    retentionInDays: environmentName == 'prod' ? 90 : 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${workloadName}-${environmentName}-appi'
  location: location
  kind: 'web'
  tags: commonTags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    DisableLocalAuth: true
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: take('${compactName}${suffix}', 50)
  location: location
  tags: commonTags
  sku: {
    name: environmentName == 'prod' ? 'Premium' : 'Standard'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: environmentName == 'prod' ? 'Disabled' : 'Enabled'
    policies: {
      retentionPolicy: {
        days: 30
        status: 'enabled'
      }
      trustPolicy: {
        type: 'Notary'
        status: 'disabled'
      }
    }
  }
}

resource workloadIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${workloadName}-${environmentName}-workload-mi'
  location: location
  tags: commonTags
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('${compactName}-${suffix}-kv', 24)
  location: location
  tags: commonTags
  properties: {
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enablePurgeProtection: environmentName == 'prod'
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: environmentName == 'prod' ? 'Disabled' : 'Enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    accessPolicies: []
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${workloadName}-${environmentName}-vnet'
  location: location
  tags: commonTags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.40.0.0/16']
    }
    subnets: [
      {
        name: 'aks'
        properties: {
          addressPrefix: '10.40.0.0/20'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'application-gateway'
        properties: {
          addressPrefix: '10.40.16.0/24'
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.40.17.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource aksSubnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' existing = {
  parent: virtualNetwork
  name: 'aks'
}

resource aks 'Microsoft.ContainerService/managedClusters@2024-02-01' = {
  name: '${workloadName}-${environmentName}-aks'
  location: location
  tags: commonTags
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'Base'
    tier: environmentName == 'prod' ? 'Standard' : 'Free'
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: '${workloadName}-${environmentName}'
    enableRBAC: true
    disableLocalAccounts: true
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
      defender: {
        logAnalyticsWorkspaceResourceId: logAnalytics.id
        securityMonitoring: {
          enabled: environmentName == 'prod'
        }
      }
    }
    addonProfiles: {
      azureKeyvaultSecretsProvider: {
        enabled: true
        config: {
          enableSecretRotation: 'true'
          rotationPollInterval: '2m'
        }
      }
      omsAgent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalytics.id
        }
      }
    }
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        count: aksSystemNodeCount
        vmSize: aksNodeVmSize
        osType: 'Linux'
        type: 'VirtualMachineScaleSets'
        vnetSubnetID: aksSubnet.id
        enableAutoScaling: true
        minCount: 2
        maxCount: 10
        maxPods: 50
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkPolicy: 'azure'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
      serviceCidr: '10.41.0.0/16'
      dnsServiceIP: '10.41.0.10'
    }
  }
}

resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: take('${compactName}-${suffix}-sql', 63)
  location: location
  tags: commonTags
  properties: {
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      login: '${workloadName}-${environmentName}-sql-admin'
      principalType: 'Group'
      sid: sqlEntraAdminObjectId
      tenantId: subscription().tenantId
    }
    minimalTlsVersion: '1.2'
    publicNetworkAccess: environmentName == 'prod' ? 'Disabled' : 'Enabled'
  }
}

resource sqlDatabase 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sqlServer
  name: 'banking'
  location: location
  tags: commonTags
  sku: {
    name: environmentName == 'prod' ? 'GP_S_Gen5_2' : 'GP_S_Gen5_1'
    tier: 'GeneralPurpose'
  }
  properties: {
    autoPauseDelay: environmentName == 'prod' ? -1 : 60
    minCapacity: 0.5
    zoneRedundant: environmentName == 'prod'
    requestedBackupStorageRedundancy: environmentName == 'prod' ? 'Geo' : 'Local'
  }
}

resource gatewayPublicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: '${workloadName}-${environmentName}-agw-pip'
  location: location
  tags: commonTags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

resource gateway 'Microsoft.Network/applicationGateways@2023-11-01' = {
  name: '${workloadName}-${environmentName}-agw'
  location: location
  tags: commonTags
  properties: {
    autoscaleConfiguration: {
      minCapacity: 1
      maxCapacity: environmentName == 'prod' ? 10 : 2
    }
    gatewayIPConfigurations: [
      {
        name: 'gateway-ip-config'
        properties: {
          subnet: {
            id: resourceId('Microsoft.Network/virtualNetworks/subnets', virtualNetwork.name, 'application-gateway')
          }
        }
      }
    ]
    frontendIPConfigurations: [
      {
        name: 'public-frontend'
        properties: {
          publicIPAddress: {
            id: gatewayPublicIp.id
          }
        }
      }
    ]
    frontendPorts: [
      {
        name: 'http'
        properties: {
          port: 80
        }
      }
    ]
    backendAddressPools: [
      {
        name: 'aks-ingress'
        properties: {}
      }
    ]
    backendHttpSettingsCollection: [
      {
        name: 'aks-http'
        properties: {
          port: 80
          protocol: 'Http'
          cookieBasedAffinity: 'Disabled'
          requestTimeout: 30
        }
      }
    ]
    httpListeners: [
      {
        name: 'http-listener'
        properties: {
          frontendIPConfiguration: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendIPConfigurations', '${workloadName}-${environmentName}-agw', 'public-frontend')
          }
          frontendPort: {
            id: resourceId('Microsoft.Network/applicationGateways/frontendPorts', '${workloadName}-${environmentName}-agw', 'http')
          }
          protocol: 'Http'
        }
      }
    ]
    requestRoutingRules: [
      {
        name: 'default-route'
        properties: {
          ruleType: 'Basic'
          priority: 100
          httpListener: {
            id: resourceId('Microsoft.Network/applicationGateways/httpListeners', '${workloadName}-${environmentName}-agw', 'http-listener')
          }
          backendAddressPool: {
            id: resourceId('Microsoft.Network/applicationGateways/backendAddressPools', '${workloadName}-${environmentName}-agw', 'aks-ingress')
          }
          backendHttpSettings: {
            id: resourceId('Microsoft.Network/applicationGateways/backendHttpSettingsCollection', '${workloadName}-${environmentName}-agw', 'aks-http')
          }
        }
      }
    ]
    webApplicationFirewallConfiguration: {
      enabled: true
      firewallMode: environmentName == 'prod' ? 'Prevention' : 'Detection'
      ruleSetType: 'OWASP'
      ruleSetVersion: '3.2'
      requestBodyCheck: true
      maxRequestBodySizeInKb: 128
      fileUploadLimitInMb: 10
    }
  }
}

output aksClusterName string = aks.name
output containerRegistryName string = registry.name
output keyVaultName string = keyVault.name
output sqlServerName string = sqlServer.name
output workloadIdentityClientId string = workloadIdentity.properties.clientId
output applicationInsightsConnectionString string = applicationInsights.properties.ConnectionString
output applicationGatewayPublicIp string = gatewayPublicIp.properties.ipAddress
"""

    files = {
        "infrastructure/bicep/main.bicep": main,
        "infrastructure/bicep/modules/platform.bicep": platform,
    }
    for environment, node_count, vm_size in (
        ("dev", 2, "Standard_D4s_v5"),
        ("test", 2, "Standard_D4s_v5"),
        ("prod", 3, "Standard_D8s_v5"),
    ):
        files[f"infrastructure/bicep/parameters/{environment}.bicepparam"] = f"""using '../main.bicep'

param location = readEnvironmentVariable('AZURE_LOCATION', 'westeurope')
param environmentName = '{environment}'
param workloadName = '{normalized}'
param sqlEntraAdminObjectId = readEnvironmentVariable('AZURE_SQL_ENTRA_ADMIN_OBJECT_ID')
param kubernetesVersion = readEnvironmentVariable('AKS_KUBERNETES_VERSION')
param aksNodeVmSize = '{vm_size}'
param aksSystemNodeCount = {node_count}
"""
    return files
