# mkDevContainer — build a sandboxed agent-powered container image
#
# Called from flake.nix with { pkgs, claude-code, defaultSkills }.
# Returns a function that takes project-specific options and produces
# a dockerTools.buildLayeredImage derivation.

{ pkgs, claude-code, defaultSkills ? {} }:

{
  # Project devShells to extract deps from (nativeBuildInputs + buildInputs).
  # Pass one via projectShell, or many via projectShells.
  projectShell ? null,
  projectShells ? [],

  # Additional packages to include
  extraPackages ? [],

  # Skills: attrset of name → { repo, path }
  # e.g. { my-skills = { repo = "owner/repo"; path = <derivation>; }; }
  skills ? defaultSkills,

  # Image name and tag
  name ? "jedibox",
  tag ? "latest",

  # Extra environment variables (attrset of "KEY" = "value")
  extraEnv ? {},

  # Extra commands to run in fakeRootCommands (string)
  extraFakeRootCommands ? "",
}:

let
  # Collect project devShells (singular for convenience, list for multi-project)
  allShells =
    (if projectShell != null then [ projectShell ] else []) ++ projectShells;

  projectDeps = builtins.concatMap (shell:
    (shell.nativeBuildInputs or []) ++ (shell.buildInputs or [])
  ) allShells;

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
    python315 uv nodejs_22
  ] ++ projectDeps ++ extraPackages;

  env = pkgs.buildEnv {
    name = "container-env";
    paths = containerPackages;
    pathsToLink = [ "/bin" "/lib" "/share" "/etc" "/include" ];
    ignoreCollisions = true;
  };

  # ── Config files ─────────────────────────────────────────────────────

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

  # Build skills marketplace entries from the attrset
  # Each skill has { repo, path } where repo is "owner/repo" for GitHub skills
  skillEntries = builtins.mapAttrs (_name: skill: {
    source = { source = "github"; repo = skill.repo; };
    installLocation = "${skill.path}";
    lastUpdated = "2025-01-01T00:00:00.000Z";
  }) skills;

  skillPaths = builtins.attrValues (builtins.mapAttrs (_: s: s.path) skills);

  knownMarketplaces = pkgs.writeText "known_marketplaces.json"
    (builtins.toJSON skillEntries);

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

  # ── Entrypoint ───────────────────────────────────────────────────────

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

      touch "$HOME/.container-initialized"
      echo "[container] Setup complete."
    fi
    exec sleep infinity
  '';

  # ── Default + extra env vars ─────────────────────────────────────────

  defaultEnv = {
    PATH = "${env}/bin";
    SHELL = "${pkgs.zsh}/bin/zsh";
    USER = "user";
    HOME = "/home/user";
    TERM = "xterm-256color";
    LANG = "C.UTF-8";
    DEVCONTAINER = "true";
    EDITOR = "vim";
    VISUAL = "vim";
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    NIX_SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    NODE_OPTIONS = "--max-old-space-size=4096";
    CLAUDE_CONFIG_DIR = "/env/.claude";
    ZSH = "${pkgs.oh-my-zsh}/share/oh-my-zsh";
    ZSH_CACHE_DIR = "/home/user/.cache/oh-my-zsh";
    GIT_CONFIG_GLOBAL = "/home/user/.gitconfig.local";
    UV_LINK_MODE = "copy";
    PYTHONDONTWRITEBYTECODE = "1";
    PIP_DISABLE_PIP_VERSION_CHECK = "1";
    NPM_CONFIG_IGNORE_SCRIPTS = "true";
    NPM_CONFIG_AUDIT = "true";
    NPM_CONFIG_FUND = "false";
    NPM_CONFIG_SAVE_EXACT = "true";
    NPM_CONFIG_UPDATE_NOTIFIER = "false";
    NPM_CONFIG_MINIMUM_RELEASE_AGE = "1440";
  };

  mergedEnv = defaultEnv // extraEnv;

  envList = builtins.attrValues (builtins.mapAttrs (k: v: "${k}=${v}") mergedEnv);

  # Config files live alongside this nix file
  configDir = ../config;

in pkgs.dockerTools.buildLayeredImage {
  inherit name tag;
  contents = [
    env entrypoint pkgs.dockerTools.usrBinEnv
  ] ++ skillPaths;
  config = {
    Cmd = [ "${entrypoint}/bin/container-start" ];
    User = "1000:1000";
    WorkingDir = "/workspace";
    Env = envList;
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
    cp ${configDir}/.zshrc          ./home/user/.zshrc
    cp ${configDir}/.tmux.conf      ./home/user/.tmux.conf
    cp ${configDir}/gitignore_global ./home/user/.gitignore_global
    cp ${gitconfigLocal}           ./home/user/.gitconfig.local

    # Claude settings
    cp ${claudeSettings}      ./env/.claude/settings.json
    cp ${knownMarketplaces}   ./env/.claude/plugins/known_marketplaces.json

    # Shell history placeholders
    touch ./commandhistory/.bash_history ./commandhistory/.zsh_history

    # Ownership
    chown -R 1000:1000 ./home/user ./workspace ./commandhistory ./env

    ${extraFakeRootCommands}
  '';
  enableFakechroot = true;
}
