# Local sandbox configuration.
#
# Copy this file to your preferred location outside this repo and edit it:
#
#   mkdir -p ~/.config/jedi
#   cp local.flake.template.nix ~/.config/jedi/flake.nix
#   $EDITOR ~/.config/jedi/flake.nix
#
# Add your project(s) as inputs and wire their devShells below.
#
# Build:
#   cd ~/.config/jedi && nix build .#container -L
#   docker load < result
#
{
  inputs = {
    devcontainer.url = "path:/path/to/claude-code-devcontainer";

    # Add project flake inputs here:
    # my-project.url = "path:/home/user/code/my-project";
    # other-project.url = "github:owner/other-project";
  };

  outputs = { devcontainer, ... }@inputs: let
    system = "x86_64-linux";
    mkDevContainer = devcontainer.lib.${system}.mkDevContainer;
  in {
    packages.${system}.container = mkDevContainer {
      # List your project devShells here:
      # projectShells = [
      #   inputs.my-project.devShells.${system}.default
      #   inputs.other-project.devShells.${system}.default
      # ];
    };
  };
}
