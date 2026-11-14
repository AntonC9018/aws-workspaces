{
  description = "Offload agent jobs to AWS with Coder and bounded shutdown leases";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/da6dc0e7b260";
  outputs = { self, nixpkgs }:
    let
      # The controller runs on the x86 laptop or the aarch64 Oracle control center.
      forSystems = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (system: f (import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: nixpkgs.lib.getName pkg == "terraform";
      }));
    in {
      packages = forSystems (pkgs: rec {
        default = coder-aws;
        coder-aws = pkgs.callPackage ./pkgs/coder-aws.nix { };
        coder-trial = pkgs.callPackage ./pkgs/coder-trial.nix { inherit coder-aws; };
      });
      devShells = forSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ (pkgs.python3.withPackages (p: [ p.boto3 p.pyyaml ])) pkgs.git pkgs.openssl pkgs.terraform ];
        };
      });
      homeModules.default = { config, lib, pkgs, ... }: {
        imports = [ ./nix/home-manager.nix ];
        config = lib.mkIf config.programs.aws-workspaces.enable {
          home.file.".agents/skills/aws-workspace".source = "${self}/skills/aws-workspace";
          home.file.".codex/skills/aws-workspace".source = "${self}/skills/aws-workspace";
          home.packages = [ self.packages.${pkgs.stdenv.hostPlatform.system}.coder-aws
                            self.packages.${pkgs.stdenv.hostPlatform.system}.coder-trial ];
        };
      };
    };
}
