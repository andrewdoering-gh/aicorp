terraform {
  backend "s3" {
    bucket = "drewnet-terraform-state"
    key    = "proxmox/aicorp/terraform.tfstate"
    region = "us-east-1"

    endpoints = {
      s3 = "http://minio01.home.arpa:9000"
    }

    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true

    use_lockfile = true
  }
}
