{
  description = "Nix-built sandboxed containers for (rogue) coding agents";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    llm-agents.url = "github:numtide/llm-agents.nix";
    skills-anthropic = { url = "github:anthropics/skills"; flake = false; };
    skills-tob = { url = "github:trailofbits/skills"; flake = false; };
    skills-tob-curated = { url = "github:trailofbits/skills-curated"; flake = false; };

    # Project slot — override at build time:
    #   nix build .#container --override-input project path:/home/yoda/code/my-project
    # Defaults to nixpkgs (no devShells, so no project deps included).
    project.follows = "nixpkgs";
  };

  outputs = inputs@{ self, nixpkgs, llm-agents
                   , skills-anthropic, skills-tob, skills-tob-curated
                   , project, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      claude-code = llm-agents.packages.${system}.claude-code;

      defaultSkills = {
        skills = {
          repo = "anthropics/skills";
          path = skills-anthropic;
        };
        trailofbits-skills = {
          repo = "trailofbits/skills";
          path = skills-tob;
        };
        trailofbits-skills-curated = {
          repo = "trailofbits/skills-curated";
          path = skills-tob-curated;
        };
      };

      mkJediCave = import ./lib/mkJediCave.nix {
        inherit pkgs claude-code defaultSkills;
      };

      # Resolve project devShell if the input provides one, otherwise empty.
      projectShell =
        if project ? devShells
           && project.devShells ? ${system}
           && project.devShells.${system} ? default
        then project.devShells.${system}.default
        else null;

    in {
      # ── Library ──────────────────────────────────────────────────────
      lib.${system}.mkJediCave = mkJediCave;

      # ── Packages ─────────────────────────────────────────────────────
      packages.${system} = {
        container = mkJediCave {
          inherit projectShell;
        };
        default = self.packages.${system}.container;
        inherit claude-code;

        jedi = pkgs.stdenvNoCC.mkDerivation {
          pname = "jedi";
          version = "0.1.0";
          src = ./cli;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          installPhase = ''
            mkdir -p $out/bin
            install -m 755 $src/jedi.py $out/bin/jedi
            wrapProgram $out/bin/jedi \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.python3 ]}
          '';
        };
      };

      # ── Dev shell ─────────────────────────────────────────────────
      devShells.${system}.default = pkgs.mkShell {
        packages = [ self.packages.${system}.jedi ];
        shellHook = ''export HOLOCRONIX_URL="path:$PWD"'';
      };

      # ── Apps ───────────────────────────────────────────────────────
      apps.${system} = {
        jedi = {
          type = "app";
          program = "${self.packages.${system}.jedi}/bin/jedi";
          meta.description = "CLI for managing jedicave containers";
        };
        default = self.apps.${system}.jedi;
      };
    };
}
