terraform {
  required_providers {
    oci   = { source = "oracle/oci", version = "~> 6.0" }
    coder = { source = "coder/coder", version = "2.18.0" }
  }
}

provider "oci" {
  region              = var.region
  config_file_profile = var.oci_profile
}
provider "coder" {}

variable "oci_profile" { type = string }
variable "region" { type = string }
variable "compartment_id" { type = string }
variable "vcn_id" { type = string }
variable "subnet_id" { type = string }
variable "availability_domain" { type = string }
variable "x86_image_id" { type = string }
variable "arm_image_id" { type = string }
variable "managed_by" { type = string }
variable "coder_url" { type = string }

variable "ssh_public_key" { type = string }
variable "ssh_cidr" { type = string }
variable "agent_name" { type = string }
variable "agent_email" { type = string }

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

data "coder_parameter" "t3_thread_id" {
  name         = "t3_thread_id"
  display_name = "Creating T3 thread ID"
  description  = "The T3 app thread ID, not the provider's native session ID. Leave blank for manual workspaces."
  type         = "string"
  default      = ""
  mutable      = true
}

data "coder_parameter" "t3_thread_url" {
  name         = "t3_thread_url"
  display_name = "Creating T3 thread link"
  type         = "string"
  default      = ""
  mutable      = true
}

data "coder_parameter" "keep_workspace" {
  name         = "keep_workspace"
  display_name = "Exempt from automatic stopped-workspace deletion"
  description  = "Set at creation; toggle later with coder-oci keep [--off] NAME."
  type         = "string"
  default      = "false"
  mutable      = false
}

data "coder_parameter" "disk_gb" {
  name         = "disk_gb"
  display_name = "Boot volume size (GiB)"
  description  = "OCI boot volume size (50–100 GiB)."
  type         = "number"
  default      = "50"
  mutable      = false
  validation {
    min = 50
    max = 100
  }
}

data "coder_parameter" "image_id" {
  name         = "image_id"
  display_name = "Custom private OCI image OCID"
  description  = "Optional private OCI custom image OCID (blank uses the base Ubuntu 24.04 image)."
  type         = "string"
  default      = ""
  mutable      = false
}

data "coder_parameter" "instance_type" {
  name         = "instance_type"
  display_name = "Machine size"
  description  = "Always Free ARM64 default (a1-2c-12g) or Free Trial E5/A1 shapes."
  default      = "a1-2c-12g"
  mutable      = false
  option {
    name  = "Always Free ARM64 — 2 OCPU / 12 GiB (A1.Flex)"
    value = "a1-2c-12g"
  }
  option {
    name  = "Trial ARM64 — 4 OCPU / 24 GiB (A1.Flex)"
    value = "a1-4c-24g"
  }
  option {
    name  = "Trial x86_64 — 2 OCPU (4 vCPU) / 8 GiB (E5.Flex)"
    value = "e5-2c-8g"
  }
  option {
    name  = "Trial x86_64 — 4 OCPU (8 vCPU) / 16 GiB (E5.Flex)"
    value = "e5-4c-16g"
  }
  option {
    name  = "Trial x86_64 — 8 OCPU (16 vCPU) / 32 GiB (E5.Flex)"
    value = "e5-8c-32g"
  }
}

locals {
  shapes = {
    "a1-2c-12g" = { shape = "VM.Standard.A1.Flex", ocpus = 2, memory = 12, arch = "arm64" }
    "a1-4c-24g" = { shape = "VM.Standard.A1.Flex", ocpus = 4, memory = 24, arch = "arm64" }
    "e5-2c-8g"  = { shape = "VM.Standard.E5.Flex", ocpus = 2, memory = 8, arch = "amd64" }
    "e5-4c-16g" = { shape = "VM.Standard.E5.Flex", ocpus = 4, memory = 16, arch = "amd64" }
    "e5-8c-32g" = { shape = "VM.Standard.E5.Flex", ocpus = 8, memory = 32, arch = "amd64" }
  }
  selected      = local.shapes[data.coder_parameter.instance_type.value]
  is_arm        = local.selected.arch == "arm64"
  workspace_url = "${var.coder_url}/@${data.coder_workspace_owner.me.name}/${data.coder_workspace.me.name}"
  origin_tags = merge({
    CreatedVia        = "Coder"
    CoderWorkspaceURL = local.workspace_url
    }, data.coder_parameter.t3_thread_id.value == "" ? {} : {
    T3ThreadId  = data.coder_parameter.t3_thread_id.value
    T3ThreadURL = data.coder_parameter.t3_thread_url.value
  })
}

resource "oci_core_network_security_group" "workspace" {
  compartment_id = var.compartment_id
  vcn_id         = var.vcn_id
  display_name   = "coder-${data.coder_workspace.me.id}"
  freeform_tags  = merge(local.origin_tags, { ManagedBy = var.managed_by })
}

resource "oci_core_network_security_group_security_rule" "ssh" {
  network_security_group_id = oci_core_network_security_group.workspace.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.ssh_cidr
  source_type               = "CIDR_BLOCK"
  description               = "SSH from the controller only"
  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
  lifecycle {
    ignore_changes = [source]
  }
}

resource "oci_core_network_security_group_security_rule" "egress" {
  network_security_group_id = oci_core_network_security_group.workspace.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
  description               = "Outbound internet access"
}

resource "coder_agent" "dev" {
  arch = local.selected.arch
  os   = "linux"
  auth = "token"
  metadata {
    key          = "memory"
    display_name = "RAM"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 2
  }
  metadata {
    key          = "disk"
    display_name = "Disk"
    script       = "coder stat disk --path /home/ubuntu"
    interval     = 60
    timeout      = 2
  }
}

resource "oci_core_instance" "workspace" {
  availability_domain  = var.availability_domain
  compartment_id       = var.compartment_id
  display_name         = "coder-${data.coder_workspace.me.name}"
  shape                = local.selected.shape
  state                = data.coder_workspace.me.transition == "start" ? "RUNNING" : "STOPPED"
  preserve_boot_volume = false

  shape_config {
    ocpus         = local.selected.ocpus
    memory_in_gbs = local.selected.memory
  }

  source_details {
    source_type             = "image"
    source_id               = data.coder_parameter.image_id.value != "" ? data.coder_parameter.image_id.value : (local.is_arm ? var.arm_image_id : var.x86_image_id)
    boot_volume_size_in_gbs = tonumber(data.coder_parameter.disk_gb.value)
  }

  create_vnic_details {
    subnet_id        = var.subnet_id
    assign_public_ip = "true"
    nsg_ids          = [oci_core_network_security_group.workspace.id]
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data = base64gzip("#cloud-config\n${jsonencode({
      package_update = true
      packages       = ["git", "rsync", "curl", "python3", "zsh"]
      write_files = [
        { path = "/usr/local/bin/workspace-lease", permissions = "0755", content = file("${path.module}/workspace_lease.py") },
        { path = "/usr/local/bin/workspace-job", permissions = "0755", content = file("${path.module}/workspace_job.py") },
        { path = "/usr/local/bin/workspace-transfer", permissions = "0755", content = file("${path.module}/workspace_transfer.py") },
        { path = "/usr/local/bin/workspace-github-credential", permissions = "0755", content = file("${path.module}/github_credential.py") },
        { path = "/etc/profile.d/agent-git.sh", content = file("${path.module}/agent-env.sh") },
        { path = "/home/ubuntu/.gitconfig", owner = "ubuntu:ubuntu", defer = true, content = "[user]\n\tname = ${var.agent_name}\n\temail = ${var.agent_email}\n[credential \"https://github.com\"]\n\thelper = /usr/local/bin/workspace-github-credential\n\tuseHttpPath = true\n[pull]\n\trebase = true\n[push]\n\tautoSetupRemote = true\n" },
        { path = "/etc/systemd/system/coder-workspace.service", permissions = "0600", content = <<-UNIT
          [Unit]
          Description=Coder workspace agent
          After=network-online.target cloud-final.service
          [Service]
          User=ubuntu
          Environment=HOME=/home/ubuntu AGENT=1 BASH_ENV=/etc/profile.d/agent-git.sh CODER_AGENT_TOKEN=${coder_agent.dev.token}
          ExecStart=/bin/bash /opt/coder-agent.sh
          Restart=always
          RestartSec=5
          [Install]
          WantedBy=multi-user.target
          UNIT
        },
        { path = "/opt/coder-agent.sh", permissions = "0755", content = replace(coder_agent.dev.init_script, var.coder_url, "http://127.0.0.1:7080") },
        { path = "/etc/systemd/system/coder-lease.service", content = "[Service]\nType=oneshot\nExecStart=/usr/local/bin/workspace-lease tick\n" },
        { path = "/etc/systemd/system/coder-lease.timer", content = "[Timer]\nOnBootSec=30\nOnUnitActiveSec=15\n[Install]\nWantedBy=timers.target\n" },
        { path = "/var/lib/cloud/scripts/per-boot/coder-lease", permissions = "0755", content = "#!/bin/sh\n/usr/local/bin/workspace-lease hold 600 --owner boot\n" },
      ]
      runcmd = [
        "mkdir -p /home/ubuntu/projects; chown ubuntu:ubuntu /home/ubuntu/projects",
        "/usr/local/bin/workspace-lease hold 600 --owner boot",
        "systemctl daemon-reload",
        "systemctl enable --now coder-lease.timer",
        "systemctl enable coder-workspace.service; systemctl start --no-block coder-workspace.service",
      ]
    })}")
  }

  # OCI enforces a 10-tag limit on freeform_tags. Keep at most 8 static tags here
  # so dynamic controller tags (SourceSnapshot, StoppedSince) stay within budget (<= 10).
  freeform_tags = merge(
    data.coder_parameter.t3_thread_id.value == "" ? {} : {
      T3ThreadId = data.coder_parameter.t3_thread_id.value
    },
    {
      ManagedBy      = var.managed_by
      WorkspaceId    = data.coder_workspace.me.id
      WorkspaceName  = data.coder_workspace.me.name
      OwnerId        = data.coder_workspace_owner.me.id
      InstanceSize   = data.coder_parameter.instance_type.value
      KeepWorkspace  = data.coder_parameter.keep_workspace.value
      LeaseExpiresAt = timeadd(timestamp(), "15m")
    }
  )

  lifecycle {
    ignore_changes = [
      metadata["user_data"],
      freeform_tags["SourceSnapshot"],
      freeform_tags["StoppedSince"],
      freeform_tags["KeepWorkspace"],
    ]
  }
}

resource "coder_metadata" "workspace" {
  resource_id = oci_core_instance.workspace.id
  item {
    key   = "created_via"
    value = "Coder"
  }
  item {
    key   = "provider"
    value = "oci"
  }
  item {
    key   = "creating_thread_id"
    value = data.coder_parameter.t3_thread_id.value == "" ? "Manual / not associated" : data.coder_parameter.t3_thread_id.value
  }
  item {
    key   = "creating_thread"
    value = data.coder_parameter.t3_thread_url.value == "" ? "Not associated" : "[Open creating T3 thread](${data.coder_parameter.t3_thread_url.value})"
  }
  item {
    key   = "instance_id"
    value = oci_core_instance.workspace.id
  }
  item {
    key   = "region"
    value = var.region
  }
  item {
    key   = "shape"
    value = "${local.selected.shape} (${local.selected.ocpus} OCPU / ${local.selected.memory} GiB)"
  }
  item {
    key   = "ssh"
    value = "Private SSH bridge; use coder-oci connect ${data.coder_workspace.me.name}"
  }
}
