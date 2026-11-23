{ writeShellApplication, runCommand, python3, awscli2, coder, openssh, rsync, systemd, git, openssl, libnotify
, name ? "coder-aws"
, entrypoint ? "coder-aws.py"
}:
let
  python = python3.withPackages (p: [ p.boto3 p.oci ]);
  controller = ../scripts;
  mkTemplate = tplName: srcDir: runCommand tplName {} ''
    mkdir -p $out
    cp ${srcDir}/main.tf $out/main.tf
    cp ${../assets/coder/workspace_lease.py} $out/workspace_lease.py
    cp ${../assets/coder/workspace_job.py} $out/workspace_job.py
    cp ${../scripts/coder_workspace/transfer.py} $out/workspace_transfer.py
    cp ${../assets/coder/github_credential.py} $out/github_credential.py
    cp ${../assets/coder/agent-env.sh} $out/agent-env.sh
    ${if builtins.pathExists (srcDir + "/.terraform.lock.hcl") then
      "cp ${srcDir}/.terraform.lock.hcl $out/.terraform.lock.hcl" else ""}
  '';
  awsTemplate = mkTemplate "coder-aws-template" ../templates/coder-aws;
  ociTemplate = mkTemplate "coder-oci-template" ../templates/coder-oci;
in
writeShellApplication {
  inherit name;
  runtimeInputs = [ awscli2 coder openssh rsync systemd git openssl libnotify ];
  text = ''
    export CODER_AWS_TEMPLATE=${awsTemplate}
    export CODER_OCI_TEMPLATE=${ociTemplate}
    export CODER_AWS_GUARD=${../assets/coder/stop_guard.py}
    export CODER_AWS_GITHUB_LIB=${../scripts/coder_workspace/github_app.py}
    export CODER_AWS_GITHUB_HELPER=${../assets/coder/github_credential.py}
    export CODER_AWS_TRANSFER_LIB=${../scripts/coder_workspace/transfer.py}
    exec ${python}/bin/python3 ${controller}/${entrypoint} "$@"
  '';
}
