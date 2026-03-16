{ pkgs, lib, config, inputs, ... }:

{
  # ── Packages ────────────────────────────────────────────────────────────
  packages = with pkgs; [
    # Sandboxing
    bubblewrap
    socat

    # CLI tools
    inputs.llm-agents.packages.${pkgs.stdenv.hostPlatform.system}.claude-code
    fd
    ripgrep
    fzf
    delta       # git-delta
    tmux
    zsh
    ast-grep
    jq
    nano
    unzip
    vim
    curl
    git
    oh-my-zsh

    # Build tools
    gcc
    gnumake
    binutils
    pkg-config
    systemdLibs  # libudev

    # Network tools (security testing)
    dnsutils
    ipset
    iptables
    iproute2
  ];

  # ── Languages ───────────────────────────────────────────────────────────
  languages.python = {
    enable = true;
    package = pkgs.python313;
    uv.enable = true;
  };

  languages.javascript = {
    enable = true;
    package = pkgs.nodejs_22;
  };

  # ── Environment variables ───────────────────────────────────────────────
  env = {
    DEVCONTAINER = "true";
    SHELL = "${pkgs.zsh}/bin/zsh";
    EDITOR = "nano";
    VISUAL = "nano";

    # Node
    NODE_OPTIONS = "--max-old-space-size=4096";

    # Claude
    CLAUDE_CONFIG_DIR = "/env/.claude";

    # Oh My Zsh (from nixpkgs, no runtime download needed)
    ZSH = "${pkgs.oh-my-zsh}/share/oh-my-zsh";

    # Git (points to container-local config that includes host config)
    GIT_CONFIG_GLOBAL = "/env/.gitconfig.local";

    # Python / uv
    UV_LINK_MODE = "copy";
    PYTHONDONTWRITEBYTECODE = "1";
    PIP_DISABLE_PIP_VERSION_CHECK = "1";

    # NPM hardening
    NPM_CONFIG_IGNORE_SCRIPTS = "true";
    NPM_CONFIG_AUDIT = "true";
    NPM_CONFIG_FUND = "false";
    NPM_CONFIG_SAVE_EXACT = "true";
    NPM_CONFIG_UPDATE_NOTIFIER = "false";
    NPM_CONFIG_MINIMUM_RELEASE_AGE = "1440";
  };

  # ── Scripts ─────────────────────────────────────────────────────────────
  # These replace post_install.py and run at container creation time.

  # setup-claude and setup-ohmyzsh removed: both are now baked into the image
  # at build time (OMZ via nixpkgs, skills via devenv inputs).

  scripts.setup-claude-settings.exec = ''
    CLAUDE_DIR="$HOME/.claude"
    mkdir -p "$CLAUDE_DIR"
    SETTINGS="$CLAUDE_DIR/settings.json"
    if [ ! -f "$SETTINGS" ] || ! jq -e '.permissions.defaultMode' "$SETTINGS" >/dev/null 2>&1; then
      cat > "$SETTINGS" << 'SETEOF'
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
SETEOF
      echo "[devenv] Claude settings configured: $SETTINGS"
    fi
  '';

  scripts.setup-gitconfig.exec = ''
    LOCAL_GITCONFIG="$HOME/.gitconfig.local"
    if [ ! -f "$LOCAL_GITCONFIG" ]; then
      cat > "$LOCAL_GITCONFIG" <<'GITEOF'
    # Container-local git config

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

    [gpg "ssh"]
        program = /usr/bin/ssh-keygen
    GITEOF
      echo "[devenv] Local git config created: $LOCAL_GITCONFIG"
    fi
  '';

  scripts.setup-tmux.exec = ''
    if [ ! -f "$HOME/.tmux.conf" ]; then
      cp ${./config/.tmux.conf} "$HOME/.tmux.conf"
      echo "[devenv] Tmux configured"
    fi
  '';

  scripts.setup-gitignore.exec = ''
    if [ ! -f "$HOME/.gitignore_global" ]; then
      cp ${./config/gitignore_global} "$HOME/.gitignore_global"
      echo "[devenv] Global gitignore created"
    fi
  '';

  scripts.setup-zshrc.exec = ''
    if [ ! -f "$HOME/.zshrc" ]; then
      cp ${./config/.zshrc} "$HOME/.zshrc"
      echo "[devenv] Zsh configured"
    fi
  '';

  scripts.fix-ownership.exec = ''
    uid=$(id -u)
    gid=$(id -g)
    for dir in "$HOME/.claude" /commandhistory; do
      if [ -d "$dir" ]; then
        owner_uid=$(stat -c '%u' "$dir" 2>/dev/null || echo "$uid")
        if [ "$owner_uid" != "$uid" ]; then
          chown -R "$uid:$gid" "$dir" 2>/dev/null || true
          echo "[devenv] Fixed ownership: $dir"
        fi
      fi
    done
  '';

  # Master setup script that runs all post-creation tasks
  scripts.post-create.exec = ''
    echo "[devenv] Running post-creation setup..."
    setup-claude-settings
    setup-tmux
    setup-gitignore
    setup-gitconfig
    setup-zshrc
    fix-ownership
    echo "[devenv] Setup complete!"
  '';

  # Container entrypoint: run post-create once on first boot, then idle
  scripts.container-start.exec = ''
    if [ ! -f "$HOME/.devenv-initialized" ]; then
      post-create
      touch "$HOME/.devenv-initialized"
    fi
    exec sleep infinity
  '';

  # ── Container ───────────────────────────────────────────────────────────
  containers.shell = let
    volumeDirs = pkgs.runCommand "volume-dirs" {} ''
      mkdir -p $out/env/.claude/plugins/marketplaces $out/commandhistory

      # Shell history placeholders
      touch $out/commandhistory/.bash_history $out/commandhistory/.zsh_history

      # Zsh config
      cp ${./config/.zshrc} $out/env/.zshrc

      # Skills marketplaces (baked at build time, no runtime download)
      cp -r ${inputs.skills-anthropic} $out/env/.claude/plugins/marketplaces/skills
      cp -r ${inputs.skills-tob} $out/env/.claude/plugins/marketplaces/trailofbits-skills
      cp -r ${inputs.skills-tob-curated} $out/env/.claude/plugins/marketplaces/trailofbits-skills-curated

      cat > $out/env/.claude/plugins/known_marketplaces.json << 'EOF'
      {
        "skills": {
          "source": {"source": "github", "repo": "anthropics/skills"},
          "installLocation": "/env/.claude/plugins/marketplaces/skills"
        },
        "trailofbits-skills": {
          "source": {"source": "github", "repo": "trailofbits/skills"},
          "installLocation": "/env/.claude/plugins/marketplaces/trailofbits-skills"
        },
        "trailofbits-skills-curated": {
          "source": {"source": "github", "repo": "trailofbits/skills-curated"},
          "installLocation": "/env/.claude/plugins/marketplaces/trailofbits-skills-curated"
        }
      }
      EOF
    '';
  in {
    name = "claude-code-devcontainer";
    startupCommand = "container-start";
    workingDir = "/workspace";
    layers = [{
      copyToRoot = [ volumeDirs ];
      perms = [{
        path = volumeDirs;
        regex = ".*";
        mode = "0755";
        uid = 1000;
        gid = 1000;
        uname = "user";
        gname = "user";
      }];
    }];
  };
}
