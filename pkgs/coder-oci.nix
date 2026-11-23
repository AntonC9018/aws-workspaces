{ callPackage }:
callPackage ./coder-aws.nix {
  name = "coder-oci";
  entrypoint = "coder-oci.py";
}
