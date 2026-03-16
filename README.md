# Claude Code in a devcontainer

A sandboxed development environment for running Claude Code with `bypassPermissions` safely enabled. Built at [Trail of Bits](https://www.trailofbits.com/) for security audit workflows.

## Why Use This?

Running Claude with `bypassPermissions` on your host machine is risky—it can execute any command without confirmation. This devcontainer provides **filesystem isolation** so you get the productivity benefits of unrestricted Claude without risking your host system.

**Designed for:**

- **Security audits**: Review client code without risking your host
- **Untrusted repositories**: Explore unknown codebases safely
- **Experimental work**: Let Claude modify code freely in isolation
- **Offline builds**: Bake project toolchains into the image for network-free builds

## Prerequisites

- **Docker runtime** (one of):
  - [Docker Desktop](https://docker.com/products/docker-desktop) - ensure it's running
  - [OrbStack](https://orbstack.dev/)
  - [Colima](https://github.com/abiosoft/colima): `brew install colima docker && colima start`

- **[Nix](https://nixos.org/download/)** (with flakes enabled)

<details>
<summary><strong>Optimizing Colima for Apple Silicon</strong></summary>

Colima's defaults (QEMU + sshfs) are conservative. For better performance:

```bash
colima stop && colima delete

colima start \
  --cpu 4 \
  --memory 8 \
  --disk 100 \
  --vm-type vz \
  --vz-rosetta \
  --mount-type virtiofs
```

Adjust `--cpu` and `--memory` based on your Mac.

| Option | Benefit |
|--------|---------|
| `--vm-type vz` | Apple Virtualization.framework (faster than QEMU) |
| `--mount-type virtiofs` | 5-10x faster file I/O than sshfs |
| `--vz-rosetta` | Run x86 containers via Rosetta |

</details>

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  ~/.config/jedi/flake.nix  (your local config, not tracked) │
│  ┌───────────────────────┐  ┌────────────────────────────┐  │
│  │ input: devcontainer   │  │ input: foo (your project)  │  │
│  │ (this repo)           │  │ path:/home/user/code/foo   │  │
│  └───────────┬───────────┘  └──────────────┬─────────────┘  │
│              │                              │                │
│              ▼                              ▼                │
│  mkDevContainer {                                           │
│    projectShells = [ foo.devShells.x86_64-linux.default ];  │
│  }                                                          │
│              │                                               │
│              ▼                                               │
│  OCI image: base tools + foo's toolchain                    │
└─────────────────────────────────────────────────────────────┘
```

| Layer | What's included | Source |
|-------|-----------------|--------|
| **Base** | Claude Code, zsh, git, ripgrep, tmux, Python, Node.js, build tools, firewall tools | `lib/mkDevContainer.nix` |
| **Project** | Compiler, cross-toolchain, build deps from the project's devShell | Project's `flake.nix` |
| **Runtime** | Source code (bind-mounted), persistent volumes for config/history | `docker-compose.yml` |

## Quick Start

### 1. Build the base image (no project)

```bash
cd /path/to/claude-code-devcontainer
nix build .#container -L
docker load < result
```

### 2. Start the container

```bash
docker compose up -d
docker compose exec shell zsh
```

This gives you a sandbox with all the base tools. Project source can be
bind-mounted via `docker-compose.override.yml`.

## Adding a Project

To include a project's dev toolchain in the image, create a local wrapper
flake that wires the project's devShell into `mkDevContainer`. The project
must have a `flake.nix` with a `devShells` output.

### 1. Copy the template

```bash
mkdir -p ~/.config/jedi
cp local.flake.template.nix ~/.config/jedi/flake.nix
```

### 2. Edit `~/.config/jedi/flake.nix`

Add your project as a flake input and wire its devShell:

```nix
{
  inputs = {
    devcontainer.url = "path:/path/to/claude-code-devcontainer";
    foo.url = "path:/home/user/code/foo";
  };

  outputs = { devcontainer, ... }@inputs: let
    system = "x86_64-linux";
    mkDevContainer = devcontainer.lib.${system}.mkDevContainer;
  in {
    packages.${system}.container = mkDevContainer {
      projectShells = [
        inputs.foo.devShells.${system}.default
      ];
    };
  };
}
```

### 3. Build the image with the project's toolchain

```bash
cd ~/.config/jedi
nix build .#container -L
docker load < result
```

The resulting image contains the base tools **plus** everything from foo's
devShell (`nativeBuildInputs` + `buildInputs`).

### 4. Mount the project source and start

Create `docker-compose.override.yml` next to the base `docker-compose.yml`:

```yaml
services:
  shell:
    volumes:
      - /home/user/code/foo:/workspace/foo
```

```bash
cd /path/to/claude-code-devcontainer
docker compose up -d
docker compose exec shell zsh
cd /workspace/foo
```

### Multiple projects

Add more inputs and shells — deps are merged (assumes compatible toolchains):

```nix
{
  inputs = {
    devcontainer.url = "path:/path/to/claude-code-devcontainer";
    foo.url = "path:/home/user/code/foo";
    bar.url = "github:owner/bar";
  };

  outputs = { devcontainer, ... }@inputs: let
    system = "x86_64-linux";
    mkDevContainer = devcontainer.lib.${system}.mkDevContainer;
  in {
    packages.${system}.container = mkDevContainer {
      projectShells = [
        inputs.foo.devShells.${system}.default
        inputs.bar.devShells.${system}.default
      ];
    };
  };
}
```

### Quick one-off (no wrapper flake)

For a single project, you can skip the wrapper and override the input directly:

```bash
nix build .#container \
  --override-input project path:/home/user/code/foo -L
```

This uses the `project` input slot declared in the base flake.

## File Layout

| File | Role | Who edits |
|------|------|-----------|
| `flake.nix` | Base flake — exports `lib.mkDevContainer` + base image | Maintained upstream |
| `lib/mkDevContainer.nix` | Image builder function | Maintained upstream |
| `local.flake.template.nix` | Template for user's local wrapper | Copy + edit |
| `docker-compose.yml` | Runtime config (volumes, caps) | Maintained upstream |
| `docker-compose.override.yml` | Project source mounts | User creates (gitignored) |
| `config/` | Shell config (.zshrc, .tmux.conf, etc.) | Maintained upstream |

## `mkDevContainer` Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `projectShell` | derivation | `null` | Single devShell (convenience) |
| `projectShells` | list | `[]` | Multiple devShells |
| `extraPackages` | list | `[]` | Additional packages |
| `skills` | attrset | Anthropic + ToB skills | Skill repos |
| `extraEnv` | attrset | `{}` | Extra environment variables |
| `extraFakeRootCommands` | string | `""` | Extra image setup commands |
| `name` | string | `"claude-sandbox"` | Image name |
| `tag` | string | `"latest"` | Image tag |

## Network Isolation

By default, containers have full outbound network access. Use firewall
rules to restrict egress to an allowlist of domains.

```bash
# From the host, using docker compose exec:
docker compose exec --user root shell bash -c \
  'iptables -F OUTPUT && \
   iptables -A OUTPUT -d api.anthropic.com -j ACCEPT && \
   iptables -A OUTPUT -o lo -j ACCEPT && \
   iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT && \
   iptables -A OUTPUT -j DROP'
```

The default allowlist (`config/firewall-defaults.conf`) permits only
`api.anthropic.com`. Additional domains are available as commented-out
entries.

Rules are applied as root from the host — the unprivileged container user
cannot modify or disable them.

## Security Model

This devcontainer provides **filesystem isolation** but not complete sandboxing.

**Sandboxed:** Filesystem (host files inaccessible), processes (isolated from host), package installations (stay in container)

**Not sandboxed:** Network (full outbound by default—see above), Docker socket (not mounted)

The container auto-configures `bypassPermissions` mode—Claude runs commands without confirmation. This would be risky on a host machine, but the container itself is the sandbox.

For a detailed analysis, see [SECURITY.md](SECURITY.md).

## Container Details

| Component | Details |
|-----------|---------|
| Base | Nix (`dockerTools.buildLayeredImage`), Node.js 22, Python 3.13 + uv, zsh |
| User | Unprivileged (UID 1000, no sudo), working dir `/workspace` |
| Tools | `rg`, `fd`, `fzf`, `delta`, `ast-grep`, `tmux`, `jq`, `vim`, `iptables`, `ipset` |
| Volumes | Command history (`/commandhistory`), Claude config (`/env/.claude`) |
| Skills | [anthropics/skills](https://github.com/anthropics/skills), [trailofbits/skills](https://github.com/trailofbits/skills), [trailofbits/skills-curated](https://github.com/trailofbits/skills-curated) |

All packages and config are baked into the image at build time — no runtime downloads.

## Troubleshooting

### Container won't start

1. Check Docker is running
2. Try rebuilding: `nix build .#container -L && docker load < result`
3. Check logs: `docker logs $(docker ps -lq)`

### Path input not picking up changes

`path:` flake inputs are locked by `narHash`. After modifying a project:

```bash
cd ~/.config/jedi
nix flake update foo
nix build .#container -L
```

### Python/uv

Python is managed via uv:

```bash
uv run script.py              # Run a script
uv add package                # Add project dependency
uv run --with requests py.py  # Ad-hoc dependency
```
