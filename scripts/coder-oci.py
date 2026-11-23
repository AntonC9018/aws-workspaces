#!/usr/bin/env python3
"""Provision and operate private Coder OCI workers with bounded leases and trial safeguards."""
from coder_workspace import controller

if __name__ == "__main__":
    controller.run_cli(default_provider="oci", prog="coder-oci")
