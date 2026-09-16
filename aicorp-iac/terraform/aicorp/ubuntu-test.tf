moved {
  from = proxmox_virtual_environment_vm.ubuntu_test
  to   = proxmox_virtual_environment_vm.control01
}

resource "proxmox_download_file" "ubuntu_cloud_image" {
  content_type = "iso"
  datastore_id = "local"
  node_name    = var.proxmox_node_name

  url       = "https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img"
  file_name = "ubuntu-26.04-server-cloudimg-amd64.img"

  overwrite = false
}

resource "proxmox_virtual_environment_vm" "ubuntu_template" {
  vm_id     = var.ubuntu_template_vmid
  name      = "ubuntu-26.04-cloud-template"
  node_name = var.proxmox_node_name

  description = "Ubuntu 26.04 cloud-init template managed by Terraform"
  tags        = ["ubuntu", "server-26.04", "template", "cloud-init", "terraform"]

  template = true
  started  = false

  machine = "q35"
  bios    = "ovmf"

  cpu {
    type    = "x86-64-v2-AES"
    sockets = 1
    cores   = 1
  }

  memory {
    dedicated = 1024
    floating  = 1024
  }

  operating_system {
    type = "l26"
  }

  agent {
    enabled = true
    trim    = true
  }

  scsi_hardware = "virtio-scsi-single"

  efi_disk {
    datastore_id      = var.datastore_id
    file_format       = "raw"
    type              = "4m"
    pre_enrolled_keys = false
  }

  disk {
    datastore_id = var.datastore_id
    interface    = "scsi0"
    file_id      = proxmox_download_file.ubuntu_cloud_image.id
    file_format  = "raw"
    size         = 16

    discard   = "on"
    iothread  = true
    ssd       = true
    backup    = true
    replicate = false
  }

  initialization {
    datastore_id = var.datastore_id
    interface    = "ide2"

    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }
  }

  network_device {
    bridge       = var.network_bridge
    model        = "virtio"
    firewall     = true
    disconnected = false
  }

  serial_device {}

  vga {
    type = "serial0"
  }
}

resource "proxmox_virtual_environment_vm" "control01" {
  vm_id     = var.control01_vmid
  name      = "aicorp-control01"
  node_name = var.proxmox_node_name

  description = "AI company control-plane and orchestration host"
  tags        = ["aicorp", "control-plane", "linux", "terraform"]

  started = true
  on_boot = true

  stop_on_destroy = true

  clone {
    vm_id        = proxmox_virtual_environment_vm.ubuntu_template.vm_id
    datastore_id = var.datastore_id
    full         = true
  }

  cpu {
    type    = "x86-64-v2-AES"
    sockets = 1
    cores   = 2
  }

  memory {
    dedicated = 8192
    floating  = 8192
  }

  agent {
    enabled = true
    trim    = true
  }

  initialization {
    datastore_id = var.datastore_id
    interface    = "ide2"

    user_account {
      username = "drew"
      keys     = [trimspace(var.ssh_public_key)]
    }

    ip_config {
      ipv4 {
        address = "${var.control01_ipv4_address}/24"
        gateway = var.control01_ipv4_gateway
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
