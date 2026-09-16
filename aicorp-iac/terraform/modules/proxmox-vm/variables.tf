variable "name" {
  description = "Name assigned to the Proxmox VM"
  type        = string
}

variable "description" {
  description = "Description assigned to the Proxmox VM"
  type        = string
  default     = "Managed by Terraform"
}

variable "node_name" {
  description = "Proxmox node on which to create the VM"
  type        = string
}

variable "vm_id" {
  description = "Unique Proxmox VM ID assigned to the clone"
  type        = number

  validation {
    condition     = var.vm_id >= 100 && var.vm_id <= 999999999
    error_message = "vm_id must be between 100 and 999999999."
  }

  validation {
    condition     = var.vm_id < 9000 || var.vm_id > 9099
    error_message = "VM IDs 9000 through 9099 are reserved for templates."
  }
}

variable "template_vm_id" {
  description = "VM ID of the source Proxmox template"
  type        = number
}

variable "started" {
  description = "Whether the VM should be running"
  type        = bool
  default     = false
}

variable "full_clone" {
  description = "Whether to create a full clone"
  type        = bool
  default     = true
}

variable "agent_enabled" {
  description = "Enable QEMU Guest Agent integration"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags assigned to the VM"
  type        = list(string)
  default     = ["terraform"]
}

variable "cpu_cores" {
  description = "Number of CPU cores assigned to the VM"
  type        = number
  default     = 2

  validation {
    condition     = var.cpu_cores >= 1 && var.cpu_cores <= 128
    error_message = "cpu_cores must be between 1 and 128."
  }
}

variable "cpu_sockets" {
  description = "Number of CPU sockets assigned to the VM"
  type        = number
  default     = 1

  validation {
    condition     = var.cpu_sockets >= 1 && var.cpu_sockets <= 4
    error_message = "cpu_sockets must be between 1 and 4."
  }
}

variable "cpu_type" {
  description = "Proxmox CPU model exposed to the VM"
  type        = string
  default     = "x86-64-v2-AES"
}

variable "memory_mb" {
  description = "Dedicated VM memory in MiB"
  type        = number
  default     = 4096

  validation {
    condition     = var.memory_mb >= 512 && var.memory_mb % 128 == 0
    error_message = "memory_mb must be at least 512 MiB and divisible by 128."
  }
}

variable "memory_floating_mb" {
  description = "Maximum ballooned memory in MiB; set equal to memory_mb to enable ballooning at that size"
  type        = number
  default     = null

  validation {
    condition = (
      var.memory_floating_mb == null ||
      (
        var.memory_floating_mb >= 512 &&
        var.memory_floating_mb % 128 == 0
      )
    )

    error_message = "memory_floating_mb must be null or at least 512 MiB and divisible by 128."
  }
}

variable "cloud_init_enabled" {
  description = "Whether to configure the VM using Proxmox cloud-init"
  type        = bool
  default     = false
}

variable "cloud_init_datastore_id" {
  description = "Datastore used for the cloud-init disk"
  type        = string
  default     = "local-lvm"
}

variable "cloud_init_username" {
  description = "Username configured by cloud-init"
  type        = string
  default     = null
}

variable "cloud_init_ssh_keys" {
  description = "SSH public keys configured by cloud-init"
  type        = list(string)
  default     = []
}

variable "ipv4_address" {
  description = "IPv4 address in CIDR notation, or dhcp"
  type        = string
  default     = "dhcp"
}

variable "ipv4_gateway" {
  description = "IPv4 default gateway; leave null for DHCP"
  type        = string
  default     = null
}
