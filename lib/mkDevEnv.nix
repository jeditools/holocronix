# mkDevEnv — structured development environment
#
# Produces an attrset that:
#   - mkJediCave consumes via its `projects` parameter
#   - provides a standard mkShell for `nix develop`
#
# Usage in a project flake:
#
#   devEnv = holocronix.lib.${system}.mkDevEnv {
#     packages = [ rust-toolchain gcc-arm-embedded ];
#     env = { CARGO_HOME = ".cargo-home"; };
#     shellHook = ''
#       mkdir -p .cargo-home
#       ln -sf ${vendoredDeps}/config.toml .cargo-home/config.toml
#     '';
#   };
#
#   devShells.${system}.default = devEnv.shell;
#   devEnvironments.${system}.default = devEnv;

{ pkgs }:

{
  packages ? [],
  env ? {},
  shellHook ? "",
}:

{
  inherit packages env;

  # Derivation wrapping setup commands — captures Nix store paths
  # (like vendored deps) in its closure, ensuring they end up in the
  # container image when mkJediCave includes this project.
  setup =
    if shellHook != ""
    then pkgs.writeShellScript "devenv-setup" shellHook
    else null;

  # Standard mkShell for `nix develop` compatibility.
  shell = pkgs.mkShell ({
    buildInputs = packages;
    inherit shellHook;
  } // env);
}
