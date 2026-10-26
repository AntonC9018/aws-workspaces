terraform {
  required_providers {
    aws   = { source = "hashicorp/aws", version = "6.70.0" }
    coder = { source = "coder/coder", version = "2.18.0" }
  }
}

provider "aws" { region = var.region }
provider "coder" {}

variable "region" { type = string }
variable "ami_id" { type = string }
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
locals {
  workspace_url = "${var.coder_url}/@${data.coder_workspace_owner.me.name}/${data.coder_workspace.me.name}"
  origin_tags = merge({
    CreatedVia        = "Coder"
    CoderWorkspaceURL = local.workspace_url
    }, data.coder_parameter.t3_thread_id.value == "" ? {} : {
    T3ThreadId  = data.coder_parameter.t3_thread_id.value
    T3ThreadURL = data.coder_parameter.t3_thread_url.value
  })
}
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "coder_parameter" "instance_type" {
  name         = "instance_type"
  display_name = "Machine size"
  description  = "Choose for actual RAM needs. Only account-eligible x86 machines are listed."
  default      = "t3.small"
  mutable      = false
  option {
    name  = "Smoke check — 2 vCPU / 2 GiB"
    value = "t3.small"
  }
  option {
    name  = "Build — 2 vCPU / 4 GiB"
    value = "c7i-flex.large"
  }
  option {
    name  = "Build — 2 vCPU / 8 GiB"
    value = "m7i-flex.large"
  }
}

resource "aws_key_pair" "workspace" {
  key_name_prefix = "coder-${data.coder_workspace.me.id}-"
  public_key      = var.ssh_public_key
  tags            = merge(local.origin_tags, { ManagedBy = var.managed_by })
}

resource "aws_security_group" "workspace" {
  name_prefix = "coder-${data.coder_workspace.me.id}-"
  description = "Private Coder SSH bridge, restricted to the controller address"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    description = "SSH from the controller only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.origin_tags, { ManagedBy = var.managed_by })
}

resource "coder_agent" "dev" {
  arch = "amd64"
  os   = "linux"
  auth = "aws-instance-identity"
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

resource "aws_instance" "workspace" {
  # Pin the tested Canonical Ubuntu 24.04 image; NixOS gets its own later template.
  ami                                  = var.ami_id
  instance_type                        = data.coder_parameter.instance_type.value
  subnet_id                            = sort(data.aws_subnets.default.ids)[0]
  associate_public_ip_address          = true
  key_name                             = aws_key_pair.workspace.key_name
  vpc_security_group_ids               = [aws_security_group.workspace.id]
  instance_initiated_shutdown_behavior = "stop"
  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = false
    tags = merge(local.origin_tags, {
      ManagedBy     = var.managed_by
      WorkspaceId   = data.coder_workspace.me.id
      WorkspaceName = data.coder_workspace.me.name
    })
  }
  user_data_base64 = base64gzip("#cloud-config\n${jsonencode({
    package_update = true
    packages       = ["git", "rsync", "curl", "python3", "zsh"]
    write_files = [
      { path = "/usr/local/bin/workspace-lease", permissions = "0755", content = file("${path.module}/workspace_lease.py") },
      { path = "/usr/local/bin/workspace-job", permissions = "0755", content = file("${path.module}/workspace_job.py") },
      { path = "/usr/local/bin/workspace-transfer", permissions = "0755", content = file("${path.module}/workspace_transfer.py") },
      { path = "/usr/local/bin/workspace-github-credential", permissions = "0755", content = file("${path.module}/github_credential.py") },
      { path = "/etc/profile.d/agent-git.sh", content = file("${path.module}/agent-env.sh") },
      { path = "/home/ubuntu/.gitconfig", owner = "ubuntu:ubuntu", defer = true, content = "[user]\n\tname = ${var.agent_name}\n\temail = ${var.agent_email}\n[credential \"https://github.com\"]\n\thelper = /usr/local/bin/workspace-github-credential\n\tuseHttpPath = true\n[pull]\n\trebase = true\n[push]\n\tautoSetupRemote = true\n" },
      { path = "/etc/systemd/system/coder-workspace.service", content = <<-UNIT
        [Unit]
        Description=Coder workspace agent
        After=network-online.target cloud-final.service
        [Service]
        User=ubuntu
        Environment=HOME=/home/ubuntu AGENT=1 BASH_ENV=/etc/profile.d/agent-git.sh
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
      { path = "/var/lib/cloud/scripts/per-boot/coder-lease", permissions = "0755", content = "#!/bin/sh\n/usr/local/bin/workspace-lease hold 600\n" },
    ]
    runcmd = [
      "mkdir -p /home/ubuntu/projects; chown ubuntu:ubuntu /home/ubuntu/projects",
      "/usr/local/bin/workspace-lease hold 600",
      "systemctl daemon-reload",
      "systemctl enable --now coder-lease.timer",
      "systemctl enable coder-workspace.service; systemctl start --no-block coder-workspace.service",
    ]
  })}")
  tags = merge(local.origin_tags, {
    Name           = "coder-${data.coder_workspace.me.name}"
    ManagedBy      = var.managed_by
    WorkspaceId    = data.coder_workspace.me.id
    WorkspaceName  = data.coder_workspace.me.name
    OwnerId        = data.coder_workspace_owner.me.id
    LeaseExpiresAt = timeadd(timestamp(), "15m")
  })
  lifecycle {
    # Explicit start/stop reconciliations refresh the startup allowance.
    # EC2 reports this false while stopped; it must not cause replacement.
    ignore_changes  = [associate_public_ip_address, tags["SourceSnapshot"], tags["SourceProjects"]]
    prevent_destroy = true
  }
}

resource "aws_ec2_instance_state" "workspace" {
  instance_id = aws_instance.workspace.id
  state       = data.coder_workspace.me.transition == "start" ? "running" : "stopped"
}

resource "coder_metadata" "workspace" {
  resource_id = aws_instance.workspace.id
  item {
    key   = "created_via"
    value = "Coder"
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
    value = aws_instance.workspace.id
  }
  item {
    key   = "region"
    value = var.region
  }
  item {
    key   = "ssh"
    value = "Private SSH bridge; use coder-aws connect ${data.coder_workspace.me.name}"
  }
}
