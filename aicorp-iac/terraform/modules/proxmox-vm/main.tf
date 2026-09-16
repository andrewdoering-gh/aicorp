resource "proxmox_virtual_environment_vm" "this" {
  name        = var.name
  description = var.description
  node_name   = var.node_name
  vm_id       = var.vm_id
  started     = var.started
  tags        = var.tags

  clone {
    vm_id = var.template_vm_id
    full  = var.full_clone
  }

  agent {
    enabled = var.agent_enabled
  }

  cpu {
    cores   = var.cpu_cores
    sockets = var.cpu_sockets
    type    = var.cpu_type
  }

  memory {
    dedicated = var.memory_mb
    floating  = coalesce(var.memory_floating_mb, var.memory_mb)
  }

  dynamic "initialization" {
    for_each = var.cloud_init_enabled ? [1] : []

    content {
      datastore_id = var.cloud_init_datastore_id

      ip_config {
        ipv4 {
          address = var.ipv4_address
          gateway = var.ipv4_gateway
        }
      }

      user_account {
        username = var.cloud_init_username
        keys     = var.cloud_init_ssh_keys
      }
    }
  }

  lifecycle {
    ignore_changes = [
      clone,
    ]

    precondition {
      condition     = var.vm_id != var.template_vm_id
      error_message = "The destination VM ID must differ from the template VM ID."
    }
  }
}
