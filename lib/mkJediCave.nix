# mkJediCave — build a sandboxed container image for coding agents
#
# Called from flake.nix with { pkgs, defaultAgents, defaultSkills, ... }.
# Returns a function that takes project-specific options and produces
# a dockerTools.buildLayeredImage derivation.

{ pkgs, defaultAgents ? {}, defaultSkills ? {}, defaultClaudeSettings ? {}, defaultPlugins ? [], defaultPluginsSrc ? null }:

{
  # Project dev environments (attrset of workspace-name → devEnv from mkDevEnv).
  # Each devEnv has: packages, env, setup, shell.
  projects ? {},

  # Legacy: project devShells to extract packages from.
  # Use `projects` instead for full env + setup support.
  projectShell ? null,
  projectShells ? [],

  # AI agents to include (attrset of name → package)
  agents ? defaultAgents,

  # Additional packages to include
  extraPackages ? [],

  # Skills: attrset of name → { repo, path }
  # e.g. { my-skills = { repo = "owner/repo"; path = <derivation>; }; }
  skills ? defaultSkills,

  # Image name and tag
  name ? "jedicave",
  tag ? "latest",

  # Extra environment variables (attrset of "KEY" = "value")
  extraEnv ? {},

  # Extra commands to run in fakeRootCommands (string)
  extraFakeRootCommands ? "",

  # Git identity
  gitUser ? "Yoda",
  gitEmail ? "yoda@jedicave.kyb",

  # Claude Code settings (attrset, converted to JSON)
  claudeSettings ? defaultClaudeSettings,

  # Plugins to auto-install on first boot (overrides defaults)
  plugins ? defaultPlugins,

  # Additional plugins (extends the list above)
  extraPlugins ? [],

  # Plugin marketplace source (path to claude-plugins-official checkout)
  pluginsSrc ? defaultPluginsSrc,
}:

let
  # ── Project environment ────────────────────────────────────────────────

  # Legacy: collect packages from devShells
  allShells =
    (if projectShell != null then [ projectShell ] else []) ++ projectShells;

  shellDeps = builtins.concatMap (shell:
    (shell.nativeBuildInputs or []) ++ (shell.buildInputs or [])
  ) allShells;

  # New: collect packages from projects
  projectPackages = builtins.concatMap (p:
    p.packages or []
  ) (builtins.attrValues projects);

  # Absolutize relative env values (starting with ".") per project
  projectEnv = builtins.foldl' (acc: projName:
    let
      proj = projects.${projName};
      envVars = proj.env or {};
      absolutized = builtins.mapAttrs (_: v:
        if builtins.substring 0 1 v == "."
        then "/workspace/${projName}/${v}"
        else v
      ) envVars;
    in acc // absolutized
  ) {} (builtins.attrNames projects);

  # Per-project setup commands (run after clone in entrypoint)
  projectSetupScript = builtins.concatStringsSep "\n" (
    builtins.filter (s: s != "") (
      builtins.attrValues (builtins.mapAttrs (projName: proj:
        if proj ? setup && proj.setup != null then ''
    if [ -d "/workspace/${projName}" ]; then
      echo "[jedicave] Setting up ${projName}..."
      (cd /workspace/${projName} && ${proj.setup})
    fi''
        else ""
      ) projects)
    )
  );

  agentPackages = builtins.attrValues agents;

  jedicavePackages = with pkgs; [
    # Core
    coreutils bashInteractive zsh git cacert git-filter-repo

    # CLI tools
    fd ripgrep gnugrep fzf delta tmux ast-grep jq nano unzip vim curl oh-my-zsh gnused gawk less poppler-utils

    # Build tools
    gcc gnumake binutils pkg-config systemdLibs systemd.dev

    # Monitoring / diagnostics (read-only, low-risk)
    iputils procps

    # Languages
    python315 uv nodejs_22
  ] ++ agentPackages ++ shellDeps ++ projectPackages ++ extraPackages;

  env = pkgs.buildEnv {
    name = "jedicave-env";
    paths = jedicavePackages;
    pathsToLink = [ "/bin" "/lib" "/share" "/etc" "/include" ];
    ignoreCollisions = true;
  };

  # Infrastructure tools — installed to /usr/local/sbin (root-only).
  # Not on the agent's PATH to prevent misuse by rogue agents.
  infraEnv = pkgs.buildEnv {
    name = "jedicave-infra-env";
    paths = with pkgs; [ bubblewrap socat dnsutils iptables ipset iproute2 ];
    pathsToLink = [ "/bin" "/sbin" ];
  };

  # ── Config files ─────────────────────────────────────────────────────

  # Merge auto-enabled pre-installed plugins into the user's settings.
  finalClaudeSettings = claudeSettings // {
    enabledPlugins = (claudeSettings.enabledPlugins or {}) // enabledPluginsFromList;
  };

  claudeSettingsFile = pkgs.writeText "claude-settings.json"
    (builtins.toJSON finalClaudeSettings);

  # known_marketplaces.json — registers each marketplace by name. The `source`
  # is a placeholder: when CLAUDE_CODE_PLUGIN_SEED_DIR is set, Claude Code
  # locates marketplace content by probing seed/marketplaces/<name>/ at
  # runtime and ignores the source URL.
  skillEntries = builtins.mapAttrs (_name: skill: {
    source = { source = "github"; repo = skill.repo; };
  }) skills;

  knownMarketplaces = pkgs.writeText "known_marketplaces.json"
    (builtins.toJSON skillEntries);

  # Copy each marketplace's source into the seed at marketplaces/<name>/.
  marketplaceCopyCommands = builtins.concatStringsSep "\n" (
    builtins.attrValues (builtins.mapAttrs (mname: skill: ''
      mkdir -p ./env/.claude-plugin-seed/marketplaces/${mname}
      cp -r ${skill.path}/. ./env/.claude-plugin-seed/marketplaces/${mname}/
    '') skills)
  );

  # ── Pre-installed plugins ────────────────────────────────────────────
  # Parse "name@marketplace" → { name, marketplace }
  allPluginSpecs = plugins ++ extraPlugins;

  parsePlugin = spec:
    let parts = builtins.split "@" spec;
    in { name = builtins.elemAt parts 0; marketplace = builtins.elemAt parts 2; };

  # Find a plugin directory in the marketplace source
  findPluginDir = name:
    let
      inPlugins = pluginsSrc + "/plugins/${name}";
      inExternal = pluginsSrc + "/external_plugins/${name}";
    in
      if builtins.pathExists inPlugins then inPlugins
      else if builtins.pathExists inExternal then inExternal
      else throw "Plugin '${name}' not found in marketplace source";

  # Read plugin version from .claude-plugin/plugin.json
  pluginVersion = dir:
    (builtins.fromJSON (builtins.readFile (dir + "/.claude-plugin/plugin.json"))).version;

  # Resolved plugin info for each requested plugin
  resolvedPlugins =
    if pluginsSrc == null then []
    else map (spec:
      let
        parsed = parsePlugin spec;
        dir = findPluginDir parsed.name;
      in {
        inherit (parsed) name marketplace;
        dir = dir;
        version = pluginVersion dir;
      }
    ) allPluginSpecs;

  # Auto-enable every pre-installed plugin.
  enabledPluginsFromList = builtins.listToAttrs (map (spec: {
    name = spec;
    value = true;
  }) allPluginSpecs);

  # Copy plugin contents into the seed at cache/<marketplace>/<plugin>/<version>/.
  # Claude Code probes this path at runtime and uses the cached copy without re-cloning.
  pluginCopyCommands = builtins.concatStringsSep "\n" (map (p: ''
    mkdir -p ./env/.claude-plugin-seed/cache/${p.marketplace}/${p.name}/${p.version}
    cp -r ${p.dir}/. ./env/.claude-plugin-seed/cache/${p.marketplace}/${p.name}/${p.version}/
  '') resolvedPlugins);

  gitconfigLocal = pkgs.writeText "gitconfig.local" ''
    [user]
        name = ${gitUser}
        email = ${gitEmail}
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

  hasClaude = agents ? claude-code;

  claudeSetup = ''
      # Claude Code config (CLAUDE_CONFIG_DIR is typically a Docker volume).
      # Marketplaces and plugins are baked into CLAUDE_CODE_PLUGIN_SEED_DIR
      # and registered automatically — only settings.json needs seeding here.
      mkdir -p "$CLAUDE_CONFIG_DIR"

      [ -f "$CLAUDE_CONFIG_DIR/settings.json" ] || \
        cp ${claudeSettingsFile} "$CLAUDE_CONFIG_DIR/settings.json"

      chmod -R u+rw "$CLAUDE_CONFIG_DIR" 2>/dev/null || true
  '';

  entrypoint = pkgs.writeShellScriptBin "jedicave-start" ''
    if [ ! -f "$HOME/.jedicave-initialized" ]; then
      echo "[jedicave] First-boot setup..."

      ${if hasClaude then claudeSetup else ""}

      touch "$HOME/.jedicave-initialized"
      echo "[jedicave] Setup complete."
    fi

    # L7 proxy CA: merge proxy CA cert with the system bundle so HTTPS
    # interception works transparently for all tools (curl, git, pip, etc.)
    if [ -f /proxy-ca/mitmproxy-ca-cert.pem ]; then
      echo "[jedicave] Injecting proxy CA certificate..."
      cat "$SSL_CERT_FILE" /proxy-ca/mitmproxy-ca-cert.pem > /tmp/ca-bundle.crt
      export SSL_CERT_FILE=/tmp/ca-bundle.crt
      export NIX_SSL_CERT_FILE=/tmp/ca-bundle.crt
      export NODE_EXTRA_CA_CERTS=/proxy-ca/mitmproxy-ca-cert.pem
      export REQUESTS_CA_BUNDLE=/tmp/ca-bundle.crt
    fi

    # Clone bare repos from /repos/ into /workspace/
    if [ -d /repos ]; then
      for bare in /repos/*.git; do
        [ -d "$bare" ] || continue
        repo_name=$(basename "$bare" .git)
        if [ ! -d "/workspace/$repo_name" ]; then
          echo "[jedicave] Cloning $repo_name..."
          git clone "$bare" "/workspace/$repo_name"
          # Materialize every seeded branch as a local tracking branch.
          # Why: a default `git clone` only creates one local branch (HEAD),
          # leaving other seeded branches reachable only via `origin/<name>`.
          git -C "/workspace/$repo_name" for-each-ref \
            --format='%(refname:short)' refs/remotes/origin/ |
            while read -r remote_ref; do
              branch="''${remote_ref#origin/}"
              [ "$branch" = "HEAD" ] && continue
              git -C "/workspace/$repo_name" show-ref --verify --quiet \
                "refs/heads/$branch" ||
                git -C "/workspace/$repo_name" branch --track \
                  "$branch" "$remote_ref"
            done
        fi
      done
    fi

    # Run project-specific setup (env, vendored deps, etc.)
    ${projectSetupScript}

    exec sleep infinity
  '';

  # ── Default + extra env vars ─────────────────────────────────────────

  defaultEnv = {
    PATH = "${env}/bin";
    PKG_CONFIG_PATH = "${env}/lib/pkgconfig";
    SHELL = "${pkgs.zsh}/bin/zsh";
    USER = "yoda";
    HOME = "/home/yoda";
    TERM = "xterm-256color";
    LANG = "C.UTF-8";
    DEVCONTAINER = "true";
    EDITOR = "vim";
    VISUAL = "vim";
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    NIX_SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    NODE_OPTIONS = "--max-old-space-size=4096";
    CLAUDE_CONFIG_DIR = "/env/.claude";
    CLAUDE_CODE_PLUGIN_SEED_DIR = "/env/.claude-plugin-seed";
    ZSH = "${pkgs.oh-my-zsh}/share/oh-my-zsh";
    ZSH_CACHE_DIR = "/home/yoda/.cache/oh-my-zsh";
    GIT_CONFIG_GLOBAL = "/home/yoda/.gitconfig.local";
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

  mergedEnv = defaultEnv // projectEnv // extraEnv;

  envList = builtins.attrValues (builtins.mapAttrs (k: v: "${k}=${v}") mergedEnv);

  # Config files live alongside this nix file
  configDir = ../config;

in pkgs.dockerTools.buildLayeredImage {
  inherit name tag;
  contents = [
    env entrypoint pkgs.dockerTools.usrBinEnv
  ];
  config = {
    Cmd = [ "${entrypoint}/bin/jedicave-start" ];
    User = "1000:1000";
    WorkingDir = "/workspace";
    Env = envList;
  };
  fakeRootCommands = ''
    # Directories
    mkdir -p ./home/yoda/.cache/oh-my-zsh ./workspace ./commandhistory
    mkdir -p -m 1777 ./tmp
    mkdir -p ./env/.claude ./etc

    # User database
    echo 'root:x:0:0:root:/root:${pkgs.bashInteractive}/bin/bash' > ./etc/passwd
    echo 'yoda:x:1000:1000:yoda:/home/yoda:${pkgs.zsh}/bin/zsh' >> ./etc/passwd
    echo 'root:x:0:' > ./etc/group
    echo 'yoda:x:1000:' >> ./etc/group
    cp ${nsswitch} ./etc/nsswitch.conf

    # Shell / editor config
    cp ${configDir}/.zshrc          ./home/yoda/.zshrc
    cp ${configDir}/.tmux.conf      ./home/yoda/.tmux.conf
    cp ${configDir}/gitignore_global ./home/yoda/.gitignore_global
    cp ${gitconfigLocal}           ./home/yoda/.gitconfig.local

    # Claude settings + plugin seed
    ${if hasClaude then ''
    cp ${claudeSettingsFile} ./env/.claude/settings.json

    # Plugin seed dir (CLAUDE_CODE_PLUGIN_SEED_DIR points here). Read-only at
    # runtime — Claude Code probes marketplaces/<name>/ and cache/<m>/<p>/<v>/
    # and uses on-disk content without cloning.
    mkdir -p ./env/.claude-plugin-seed
    cp ${knownMarketplaces} ./env/.claude-plugin-seed/known_marketplaces.json
    ${marketplaceCopyCommands}
    ${if pluginsSrc != null then pluginCopyCommands else ""}
    '' else ""}

    # Shell history placeholders
    touch ./commandhistory/.bash_history ./commandhistory/.zsh_history

    # Ownership
    chown -R 1000:1000 ./home/yoda ./workspace ./commandhistory ./env

    # Root-only infrastructure tools (not on agent PATH)
    mkdir -p ./usr/local/sbin
    for bin in ${infraEnv}/bin/* ${infraEnv}/sbin/*; do
      [ -e "$bin" ] && ln -sf "$bin" "./usr/local/sbin/$(basename "$bin")"
    done
    chown 0:0 ./usr/local/sbin
    chmod 0750 ./usr/local/sbin

    ${extraFakeRootCommands}
  '';
  enableFakechroot = true;
}
