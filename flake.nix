{
  description = "Offload agent jobs to AWS with Coder and bounded shutdown leases";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/da6dc0e7b260";
  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfreePredicate = pkg: nixpkgs.lib.getName pkg == "terraform";
      };
      coderAws = pkgs.callPackage ./pkgs/coder-aws.nix { };
    in {
      packages.${system} = {
        default = coderAws;
        coder-aws = coderAws;
        coder-trial = pkgs.callPackage ./pkgs/coder-trial.nix { coder-aws = coderAws; };
      };
      devShells.${system}.default = pkgs.mkShell {
        packages = [ (pkgs.python3.withPackages (p: [ p.boto3 p.pyyaml ])) pkgs.git pkgs.openssl pkgs.terraform ];
      };
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
