output "control01_vm_id" {
  description = "VMID of the AI company control-plane VM"
  value       = proxmox_virtual_environment_vm.control01.vm_id
}

output "control01_ipv4_addresses" {
  description = "IPv4 addresses reported by the control-plane guest agent"
  value       = proxmox_virtual_environment_vm.control01.ipv4_addresses
}

output "control01_mac_addresses" {
  description = "MAC addresses reported by the control-plane guest agent"
  value       = proxmox_virtual_environment_vm.control01.mac_addresses
}
