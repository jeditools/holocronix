# Holocronix

A Nix flake library for building sandboxed containers. Bake project
toolchains into reproducible OCI images and run AI coding agents safely
inside isolated environments.

Inspired by [Trail of Bits' claude-code-devcontainer](https://github.com/trailofbits/claude-code-devcontainer).

## Why Use This?

Running AI coding agents with unrestricted permissions on your host
machine is risky — they can execute any command without confirmation. A
jedicave provides **filesystem isolation** so you get the productivity
benefits of autonomous agents without risking your host system.

Currently ships with [Claude Code](https://claude.ai) via
[llm-agents.nix](https://github.com/numtide/llm-agents.nix). Support
for additional agents is planned.

**Designed for:**

- **Experimentation**: Let agents modify code freely in isolation
- **Untrusted repositories**: Explore unknown codebases safely
- **Reproducible toolchains**: Bake project deps into the image via Nix devShells
- **Offline builds**: All tools baked in at build time — no runtime downloads

## Status

This is an early-stage project. Expect rough edges.

- **Tested on**: Fedora (x86_64) with Docker Engine
- **Not tested on**: macOS, other Linux distros, Docker Desktop, Podman
- **Requires**: Projects you want to sandbox should have a `flake.nix`
  that exposes a `devShells` output (or you can use `extraPackages` to
  add tools manually)

## Naming

| Term | What it is |
|------|-----------|
| **Holocronix** | This repo — the Nix flake library |
| **Jedicave** | A sandbox instance with its own flake.nix and compose.yml |
| **jedi** | The CLI that manages caves |

## Prerequisites

- **[Nix](https://nixos.org/download/)** (with flakes enabled)
- **[Docker Engine](https://docs.docker.com/engine/install/)**

Other Docker runtimes (Docker Desktop, Podman, etc.) may work but have
not been tested.

## Quick Start

### 1. Install the CLI

```bash
nix profile install github:jeditools/holocronix#jedi
```

Or run without installing:

```bash
nix run github:jeditools/holocronix -- <command>
```

### 2. Create a cave

```bash
jedi init dagobah
```

This scaffolds `~/.config/jedicaves/dagobah/` with:
- `flake.nix` — references holocronix + your project inputs
- `compose.yml` — container runtime config
- `firewall-defaults.conf` — domain allowlist

### 3. Configure the cave

Edit `~/.config/jedicaves/dagobah/flake.nix` — add your project as an
input and wire its devShell:

```nix
{
  inputs = {
    holocronix.url = "github:jeditools/holocronix";
    foo.url = "path:/home/yoda/code/foo";
  };

  outputs = { holocronix, ... }@inputs: let
    system = "x86_64-linux";
    mkJediCave = holocronix.lib.${system}.mkJediCave;
  in {
    packages.${system}.container = mkJediCave {
      projectShells = [
        inputs.foo.devShells.${system}.default
      ];
    };
  };
}
```

**Note:** Your project needs a `flake.nix` with a `devShells` output for
this to work. If it doesn't have one, you can still use `extraPackages`
to add tools manually:

```nix
packages.${system}.container = mkJediCave {
  extraPackages = with holocronix.inputs.nixpkgs.legacyPackages.${system}; [
    go
    gopls
  ];
};
```

### 4. Build

```bash
jedi build dagobah
```

This runs `nix build` and loads the image into Docker. The resulting image
contains all base tools **plus** everything from your project's devShell.

### 5. Seed your repo

```bash
jedi seed /home/yoda/code/foo dagobah
```

This creates a **bare repo** in the cave at `repos/foo.git`, pushes your
current branch into it, and mounts it into the container. On startup the
container clones it into `/workspace/foo`.

Why a bare repo instead of a direct bind mount? See
[Git Handoff Lifecycle](#git-handoff-lifecycle) below.

### 6. Use

```bash
jedi shell dagobah     # Ephemeral — container removed on exit
jedi up dagobah        # Long-running — stays in background
jedi enter dagobah     # Enter the running cave
jedi down dagobah      # Stop
```

## CLI Reference

| Command | Description | Requires `up`? |
|---------|-------------|----------------|
| `jedi init <name>` | Create a new cave | — |
| `jedi build [name]` | Build cave image | — |
| `jedi build --update [input]` | Update flake inputs then build | — |
| `jedi seed <repo-path> [name]` | Seed a repo as a bare repo | — |
| `jedi unseed <repo-name> [name]` | Remove a seeded repo | — |
| `jedi harvest [name]` | Show agent commits, fetch instructions | — |
| `jedi shell [name]` | Ephemeral cave, removed on exit | No |
| `jedi up [name]` | Start cave in background | — |
| `jedi enter [name]` | Enter a running cave | Yes |
| `jedi exec [name] -- cmd` | Run command in running cave | Yes |
| `jedi down [name]` | Stop cave | — |
| `jedi list` | List all caves with status | — |
| `jedi show [name]` | Cave overview (repos, volumes, status) | — |
| `jedi dir [name]` | Print cave directory path | — |
| `jedi logs [name]` | Show container logs | — |
| `jedi firewall <on\|off\|status> [name]` | Manage network firewall | Yes |
| `jedi destroy <name>` | Delete a cave | — |

When only one cave exists, the name can be omitted.

## Architecture

```
~/.config/jedicaves/dagobah/
├── flake.nix                  ← imports holocronix + project
├── flake.lock
├── compose.yml                ← container runtime config
├── firewall-defaults.conf     ← domain allowlist
└── repos/
    └── foo.git/               ← bare repo (seeded from host)
```

```
┌────────────────────────────────────────────────────────────┐
│  Cave flake.nix                                            │
│  ┌──────────────────────┐  ┌────────────────────────────┐  │
│  │ input: holocronix    │  │ input: foo (your project)  │  │
│  │ (this repo)          │  │ path:/home/yoda/code/foo   │  │
│  └──────────┬───────────┘  └──────────────┬─────────────┘  │
│             │                              │                │
│             ▼                              ▼                │
│  mkJediCave {                                          │
│    projectShells = [ foo.devShells.x86_64-linux.default ]; │
│  }                                                         │
│             │                                              │
│             ▼                                              │
│  OCI image: base tools + foo's toolchain                   │
└────────────────────────────────────────────────────────────┘
```

| Layer | What's included | Source |
|-------|-----------------|--------|
| **Base** | Claude Code, zsh, git, ripgrep, tmux, Python, Node.js, build tools, firewall | `lib/mkJediCave.nix` |
| **Project** | Compiler, cross-toolchain, build deps from the project's devShell | Project's `flake.nix` |
| **Runtime** | Source code (cloned from seeded bare repos), persistent volumes for config/history | `compose.yml` |

## Multiple Projects

Add more inputs and shells — deps are merged (assumes compatible toolchains):

```nix
{
  inputs = {
    holocronix.url = "github:jeditools/holocronix";
    foo.url = "path:/home/yoda/code/foo";
    bar.url = "github:owner/bar";
  };

  outputs = { holocronix, ... }@inputs: let
    system = "x86_64-linux";
    mkJediCave = holocronix.lib.${system}.mkJediCave;
  in {
    packages.${system}.container = mkJediCave {
      projectShells = [
        inputs.foo.devShells.${system}.default
        inputs.bar.devShells.${system}.default
      ];
    };
  };
}
```

## `mkJediCave` Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `projectShell` | derivation | `null` | Single devShell (convenience) |
| `projectShells` | list | `[]` | Multiple devShells |
| `extraPackages` | list | `[]` | Additional Nix packages |
| `skills` | attrset | Anthropic + ToB skills | Skill repos |
| `extraEnv` | attrset | `{}` | Extra environment variables |
| `extraFakeRootCommands` | string | `""` | Extra image setup commands |
| `name` | string | `"jedicave"` | Image name |
| `tag` | string | `"latest"` | Image tag |

## Network Isolation

By default, caves have full outbound network access. Use the firewall to
restrict egress to an allowlist of domains:

```bash
jedi firewall on dagobah
```

The default allowlist (`firewall-defaults.conf`) permits only
`api.anthropic.com`. Edit it to add more domains.

Rules are applied as root — the unprivileged container user cannot modify
or disable them.

```bash
jedi firewall off dagobah     # Restore full access
jedi firewall status dagobah  # Show current rules
```

## Rebuilding After Changes

Nix caches aggressively. When you modify project source:

```bash
jedi build --update foo dagobah   # Update foo input, rebuild
```

To update all inputs (including holocronix, nixpkgs):

```bash
jedi build --update dagobah
```

To rebuild without updating (uses cached inputs):

```bash
jedi build dagobah
```

## Git Handoff Lifecycle

Instead of bind-mounting your repo directly into the container, holocronix
uses **bare repos** as a secure handoff mechanism. This prevents a rogue
agent from tampering with your host's git hooks, config, or history.

### The flow

```
  HOST                             CONTAINER
  ────                             ─────────
  ~/code/foo/                      /repos/foo.git (bare, mounted)
       │                                │
       │  jedi seed                     │
       ├──────────────────────────►     │
       │  (git push to bare repo)       │
       │                                │
       │                           git clone ──► /workspace/foo
       │                                         (agent works here)
       │                                              │
       │                                         git commit
       │                                              │
       │  jedi harvest                                │
       │◄─────────────────────────────────────────────┤
       │  (git fetch from bare repo)                  │
       │                                              │
  review + merge                                      │
       │                                              │
  git push (to GitHub, etc.)
```

### Step by step

**1. Seed** — push a branch from your repo into the cave:

```bash
jedi seed ~/code/foo dagobah                  # current branch
jedi seed --branch feature ~/code/foo dagobah # specific branch
```

**2. Start the cave** — the entrypoint auto-clones into `/workspace/foo`:

```bash
jedi up dagobah
```

**3. Agent works** — commits go to the bare repo's object store.

**4. Harvest** — see what the agent did:

```bash
jedi harvest dagobah
```

**5. Fetch into your repo** — review and merge:

```bash
cd ~/code/foo
git fetch ~/.config/jedicaves/dagobah/repos/foo.git
git log FETCH_HEAD
git diff HEAD..FETCH_HEAD
git merge FETCH_HEAD   # or cherry-pick, rebase, etc.
```

**6. Push** — once you're satisfied, push to your remote as usual:

```bash
git push origin main
```

### Why not bind-mount?

A direct bind mount (`-v ~/code/foo:/workspace/foo`) exposes your `.git`
directory. A rogue agent could:

- Inject hooks (e.g. `post-checkout`) that execute on the host
- Rewrite refs or history
- Modify git config (aliases, remote URLs)

The bare repo approach eliminates these vectors. The agent writes to an
isolated object store, and you pull the results back with a review step.

### Re-seeding

To push updated code into the cave:

```bash
jedi seed ~/code/foo dagobah
```

To start fresh:

```bash
jedi unseed foo dagobah
jedi seed ~/code/foo dagobah
```

## Security Model

**Sandboxed:** Filesystem (host files inaccessible except mounts), processes (isolated namespaces), packages (stay in container)

**Not sandboxed:** Network (full outbound by default), kernel (shared with host)

The container auto-configures `bypassPermissions` — Claude runs commands
without confirmation. This is safe because the container itself is the
sandbox boundary.

For a detailed analysis, see [SECURITY.md](SECURITY.md).

## Container Details

| Component | Details |
|-----------|---------|
| Base | Nix (`dockerTools.buildLayeredImage`), Node.js 22, Python 3.15 + uv, zsh |
| User | Unprivileged (UID 1000, no sudo), working dir `/workspace` |
| Tools | `rg`, `fd`, `fzf`, `delta`, `ast-grep`, `tmux`, `jq`, `vim`, `iptables`, `ipset` |
| Volumes | Command history (`/commandhistory`), Claude config (`/env/.claude`) |
| Skills | [anthropics/skills](https://github.com/anthropics/skills), [trailofbits/skills](https://github.com/trailofbits/skills), [trailofbits/skills-curated](https://github.com/trailofbits/skills-curated) |

All packages and config are baked into the image at build time — no runtime downloads.

## Troubleshooting

### Container won't start

1. Check Docker is running: `systemctl status docker`
2. Rebuild: `jedi build dagobah`
3. Check logs: `jedi logs dagobah`

### Path input not picking up changes

`path:` flake inputs are locked by `narHash`. Update the specific input:

```bash
jedi build --update foo dagobah
```

## Acknowledgments

Built on the foundation of [claude-code-devcontainer](https://github.com/trailofbits/claude-code-devcontainer) by [Trail of Bits](https://www.trailofbits.com/). See [ORIGINS.md](ORIGINS.md) for details.
