output "vm_id" {
  description = "Proxmox VM ID"
  value       = proxmox_virtual_environment_vm.this.vm_id
}

output "name" {
  description = "Proxmox VM name"
  value       = proxmox_virtual_environment_vm.this.name
}

output "ipv4_addresses" {
  description = "IPv4 addresses reported by QEMU Guest Agent"
  value       = proxmox_virtual_environment_vm.this.ipv4_addresses
}
