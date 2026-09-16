variable "proxmox_endpoint" {
  description = "API endpoint for the nested Proxmox node"
  type        = string
  default     = "https://192.168.3.20:8006/"
}

variable "proxmox_insecure" {
  description = "Allow the nested Proxmox self-signed certificate"
  type        = bool
  default     = true
}

variable "proxmox_node_name" {
  description = "Nested Proxmox node name"
  type        = string
  default     = "aicorp-pve01"
}

variable "datastore_id" {
  description = "Nested Proxmox datastore for VM disks"
  type        = string
  default     = "local-lvm"
}

variable "network_bridge" {
  description = "Nested Proxmox bridge for the control-plane VM"
  type        = string
  default     = "vmbr0"
}

variable "ubuntu_template_vmid" {
  description = "VMID of the Ubuntu cloud-init template inside nested Proxmox"
  type        = number
  default     = 9008
}

variable "control01_vmid" {
  description = "VMID of the AI Company control-plane VM"
  type        = number
  default     = 1200
}

variable "control01_ipv4_address" {
  description = "Static IPv4 address (no prefix) for the control-plane VM"
  type        = string
  default     = "192.168.3.101"
}

variable "control01_ipv4_gateway" {
  description = "Default gateway for the control-plane VM's network"
  type        = string
  default     = "192.168.3.1"
}

variable "ssh_public_key" {
  description = "SSH public key installed in the control-plane VM"
  type        = string
}
