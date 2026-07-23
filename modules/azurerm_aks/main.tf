```tf
resource "azurerm_kubernetes_cluster" "aks" {
    for_each = var.aks_var
    name = each.value.aks_name
    location = each.value.location
    resource_group_name = each.value.resource_group_name
    dns_prefix = each.value.dns_prefix

    default_node_pool {
        name = "default"
        node_count = each.value.node_count
        vm_size = each.value.vm_size
    }

    identity {
        type = "SystemAssigned"
    }

    network_profile {
        network_plugin = "azure"
        network_policy = "calico"
    }
    tags = each.value.tags
}
```