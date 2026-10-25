{ writeShellApplication, runCommand, python3, awscli2, coder, openssh, rsync, systemd, git, openssl }:
let
  python = python3.withPackages (p: [ p.boto3 ]);
  controller = ../scripts;
  template = runCommand "coder-aws-template" {} ''
    mkdir -p $out
    cp ${../templates/coder-aws/main.tf} $out/main.tf
    cp ${../assets/coder/workspace_lease.py} $out/workspace_lease.py
    cp ${../assets/coder/workspace_job.py} $out/workspace_job.py
    cp ${../scripts/coder_workspace/transfer.py} $out/workspace_transfer.py
    cp ${../assets/coder/github_credential.py} $out/github_credential.py
    cp ${../assets/coder/agent-env.sh} $out/agent-env.sh
    ${if builtins.pathExists ../templates/coder-aws/.terraform.lock.hcl then
      "cp ${../templates/coder-aws/.terraform.lock.hcl} $out/.terraform.lock.hcl" else ""}
  '';
in
writeShellApplication {
  name = "coder-aws";
  runtimeInputs = [ awscli2 coder openssh rsync systemd git openssl ];
  text = ''
    export CODER_AWS_TEMPLATE=${template}
    export CODER_AWS_GUARD=${../assets/coder/stop_guard.py}
    export CODER_AWS_GITHUB_LIB=${../scripts/coder_workspace/github_app.py}
    export CODER_AWS_GITHUB_HELPER=${../assets/coder/github_credential.py}
    export CODER_AWS_TRANSFER_LIB=${../scripts/coder_workspace/transfer.py}
    exec ${python}/bin/python3 ${controller}/coder-aws.py "$@"
  '';
}
