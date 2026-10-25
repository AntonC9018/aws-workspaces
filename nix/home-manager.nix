{ config, lib, ... }:
let
  cfg = config.programs.aws-workspaces;
in {
  options.programs.aws-workspaces = {
    enable = lib.mkEnableOption "AWS workspace controller settings and agent skill";
    settings = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Non-secret controller settings. Credentials remain outside the Nix store.";
    };
  };
  config = lib.mkIf cfg.enable {
    xdg.configFile."coder-workspaces/config.json".text = builtins.toJSON cfg.settings;
    home.file.".agents/skills/aws-workspace".source = ../skills/aws-workspace;
    home.file.".codex/skills/aws-workspace".source = ../skills/aws-workspace;
  };
}
