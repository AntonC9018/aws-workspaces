#!/usr/bin/env python3
"""Provision and operate private Coder AWS workers with independent stop leases."""
from coder_workspace import controller
from coder_workspace.providers import aws as aws_provider
from coder_workspace.settings import load

SETTINGS = load()
STATE = controller.STATE


def controller_policy(account, subnets):
    return aws_provider.controller_policy(SETTINGS, account, subnets)


def thread_origin(thread_id, thread_url=None):
    return controller.thread_origin(SETTINGS, thread_id, thread_url)


should_connect = controller.should_connect


def show_links(name):
    return controller.show_links(name, SETTINGS, aws_provider.AwsProvider.filter_hint)


if __name__ == "__main__":
    controller.run_cli(default_provider="aws", prog="coder-aws")
