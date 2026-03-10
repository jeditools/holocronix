{
  description = "Sandboxed dev container with Claude Code";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    llm-agents.url = "github:numtide/llm-agents.nix";
    skills-anthropic = { url = "github:anthropics/skills"; flake = false; };
    skills-tob = { url = "github:trailofbits/skills"; flake = false; };
    skills-tob-curated = { url = "github:trailofbits/skills-curated"; flake = false; };

    # Project repos — add devShells here to bake their toolchains in
    xous-core.url = "path:/home/pufix/code/baochip/xous-core";
  };

  outputs = { self, nixpkgs, llm-agents
            , skills-anthropic, skills-tob, skills-tob-curated
            , xous-core }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      claude-code = llm-agents.packages.${system}.claude-code;

      # ── Project devShells to include ────────────────────────────────────
      # Add entries here to bake project toolchains into the container.
      projectShells = [
        xous-core.devShells.${system}.default
      ];

      # Extract packages from project devShells
      projectDeps = builtins.concatMap (shell:
        (shell.nativeBuildInputs or []) ++ (shell.buildInputs or [])
      ) projectShells;

      # ── Base packages ──────────────────────────────────────────────────
      containerPackages = with pkgs; [
        # Core
        coreutils bashInteractive zsh git cacert

        # AI tooling
        claude-code

        # CLI tools
        fd ripgrep fzf delta tmux ast-grep jq nano unzip vim curl oh-my-zsh gnused

        # Build tools
        gcc gnumake binutils pkg-config systemdLibs

        # Sandboxing / network
        bubblewrap socat dnsutils ipset iptables iproute2

        # Languages
        python313 uv nodejs_22
      ] ++ projectDeps;

      env = pkgs.buildEnv {
        name = "container-env";
        paths = containerPackages;
        pathsToLink = [ "/bin" "/lib" "/share" "/etc" "/include" ];
        ignoreCollisions = true;
      };

      # ── Config files (as derivations) ───────────────────────────────────

      claudeSettings = pkgs.writeText "claude-settings.json" ''
        {
          "permissions": {
            "defaultMode": "bypassPermissions"
          },
          "hooks": {
            "PreToolUse": [
              {
                "matcher": "Bash",
                "hooks": [
                  {
                    "type": "command",
                    "command": "cmd=$(cat | jq -r .tool_input.command); if printf '%s' \"$cmd\" | grep -qiE '\\bgit\\s+push\\b|\\bgit\\s+commit\\b|\\bgh\\s+'; then echo 'BLOCKED: git push, git commit, and gh commands are not allowed in this container' >&2; exit 2; fi"
                  }
                ]
              }
            ]
          }
        }
      '';

      knownMarketplaces = pkgs.writeText "known_marketplaces.json" (builtins.toJSON {
        skills = {
          source = { source = "github"; repo = "anthropics/skills"; };
          installLocation = "${skills-anthropic}";
          lastUpdated = "2025-01-01T00:00:00.000Z";
        };
        trailofbits-skills = {
          source = { source = "github"; repo = "trailofbits/skills"; };
          installLocation = "${skills-tob}";
          lastUpdated = "2025-01-01T00:00:00.000Z";
        };
        trailofbits-skills-curated = {
          source = { source = "github"; repo = "trailofbits/skills-curated"; };
          installLocation = "${skills-tob-curated}";
          lastUpdated = "2025-01-01T00:00:00.000Z";
        };
      });

      gitconfigLocal = pkgs.writeText "gitconfig.local" ''
        [core]
            excludesfile = ~/.gitignore_global
            pager = delta
        [interactive]
            diffFilter = delta --color-only
        [delta]
            navigate = true
            light = false
            line-numbers = true
            side-by-side = false
        [merge]
            conflictstyle = diff3
        [diff]
            colorMoved = default
      '';

      nsswitch = pkgs.writeText "nsswitch.conf" ''
        passwd: files
        group:  files
        hosts:  files dns
      '';

      # ── Entrypoint ──────────────────────────────────────────────────────
      entrypoint = pkgs.writeShellScriptBin "container-start" ''
        if [ ! -f "$HOME/.container-initialized" ]; then
          echo "[container] First-boot setup..."

          # Claude config (CLAUDE_CONFIG_DIR is typically a Docker volume)
          mkdir -p "$CLAUDE_CONFIG_DIR/plugins"

          [ -f "$CLAUDE_CONFIG_DIR/settings.json" ] || \
            cp ${claudeSettings} "$CLAUDE_CONFIG_DIR/settings.json"

          [ -f "$CLAUDE_CONFIG_DIR/plugins/known_marketplaces.json" ] || \
            cp ${knownMarketplaces} "$CLAUDE_CONFIG_DIR/plugins/known_marketplaces.json"

          chmod -R u+rw "$CLAUDE_CONFIG_DIR" 2>/dev/null || true

          # Configure cargo vendoring for mounted projects
          for d in "$HOME"/*/; do
            if [ -f "$d/Cargo.toml" ] && command -v xous-vendor-setup >/dev/null 2>&1; then
              echo "[container] Setting up cargo vendoring in $d"
              (cd "$d" && xous-vendor-setup) || true
            fi
          done

          touch "$HOME/.container-initialized"
          echo "[container] Setup complete."
        fi
        exec sleep infinity
      '';

    in {
      packages.${system}.container = pkgs.dockerTools.buildLayeredImage {
        name = "claude-sandbox";
        tag = "latest";
        contents = [
          env entrypoint pkgs.dockerTools.usrBinEnv
          skills-anthropic skills-tob skills-tob-curated
        ];
        config = {
          Cmd = [ "${entrypoint}/bin/container-start" ];
          User = "1000:1000";
          WorkingDir = "/workspace";
          Env = [
            "PATH=${env}/bin"
            "SHELL=${pkgs.zsh}/bin/zsh"
            "USER=user"
            "HOME=/home/user"
            "TERM=xterm-256color"
            "LANG=C.UTF-8"

            # Container
            "DEVCONTAINER=true"
            "EDITOR=vim"
            "VISUAL=vim"

            # TLS
            "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
            "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"

            # Node
            "NODE_OPTIONS=--max-old-space-size=4096"

            # Claude
            "CLAUDE_CONFIG_DIR=/env/.claude"

            # Oh My Zsh
            "ZSH=${pkgs.oh-my-zsh}/share/oh-my-zsh"
            "ZSH_CACHE_DIR=/home/user/.cache/oh-my-zsh"

            # Git
            "GIT_CONFIG_GLOBAL=/home/user/.gitconfig.local"

            # Python
            "UV_LINK_MODE=copy"
            "PYTHONDONTWRITEBYTECODE=1"
            "PIP_DISABLE_PIP_VERSION_CHECK=1"

            # NPM hardening
            "NPM_CONFIG_IGNORE_SCRIPTS=true"
            "NPM_CONFIG_AUDIT=true"
            "NPM_CONFIG_FUND=false"
            "NPM_CONFIG_SAVE_EXACT=true"
            "NPM_CONFIG_UPDATE_NOTIFIER=false"
            "NPM_CONFIG_MINIMUM_RELEASE_AGE=1440"
          ];
        };
        fakeRootCommands = ''
          # Directories
          mkdir -p ./home/user/.cache/oh-my-zsh ./workspace ./commandhistory
          mkdir -p -m 1777 ./tmp
          mkdir -p ./env/.claude/plugins ./etc

          # User database
          echo 'root:x:0:0:root:/root:${pkgs.bashInteractive}/bin/bash' > ./etc/passwd
          echo 'user:x:1000:1000:user:/home/user:${pkgs.zsh}/bin/zsh' >> ./etc/passwd
          echo 'root:x:0:' > ./etc/group
          echo 'user:x:1000:' >> ./etc/group
          cp ${nsswitch} ./etc/nsswitch.conf

          # Shell / editor config
          cp ${./config/.zshrc}          ./home/user/.zshrc
          cp ${./config/.tmux.conf}      ./home/user/.tmux.conf
          cp ${./config/gitignore_global} ./home/user/.gitignore_global
          cp ${gitconfigLocal}           ./home/user/.gitconfig.local

          # Claude settings (skills live in /nix/store, referenced by known_marketplaces.json)
          cp ${claudeSettings}      ./env/.claude/settings.json
          cp ${knownMarketplaces}   ./env/.claude/plugins/known_marketplaces.json

          # Shell history placeholders
          touch ./commandhistory/.bash_history ./commandhistory/.zsh_history

          # Ownership
          chown -R 1000:1000 ./home/user ./workspace ./commandhistory ./env
        '';
        enableFakechroot = true;
      };
    };
}
