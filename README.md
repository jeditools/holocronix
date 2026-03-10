# Claude Code in a devcontainer

A sandboxed development environment for running Claude Code with `bypassPermissions` safely enabled. Built at [Trail of Bits](https://www.trailofbits.com/) for security audit workflows.

## Why Use This?

Running Claude with `bypassPermissions` on your host machine is risky—it can execute any command without confirmation. This devcontainer provides **filesystem isolation** so you get the productivity benefits of unrestricted Claude without risking your host system.

**Designed for:**

- **Security audits**: Review client code without risking your host
- **Untrusted repositories**: Explore unknown codebases safely
- **Experimental work**: Let Claude modify code freely in isolation
- **Multi-repo engagements**: Work on multiple related repositories

## Prerequisites

- **Docker runtime** (one of):
  - [Docker Desktop](https://docker.com/products/docker-desktop) - ensure it's running
  - [OrbStack](https://orbstack.dev/)
  - [Colima](https://github.com/abiosoft/colima): `brew install colima docker && colima start`

- **[Nix](https://nixos.org/download/)** (with flakes enabled)

- **For terminal workflows** (one-time install):

  ```bash
  git clone https://github.com/trailofbits/claude-code-devcontainer ~/.claude-devcontainer
  ~/.claude-devcontainer/install.sh self-install
  ```

<details>
<summary><strong>Optimizing Colima for Apple Silicon</strong></summary>

Colima's defaults (QEMU + sshfs) are conservative. For better performance:

```bash
# Stop and delete current VM (removes containers/images)
colima stop && colima delete

# Start with optimized settings
colima start \
  --cpu 4 \
  --memory 8 \
  --disk 100 \
  --vm-type vz \
  --vz-rosetta \
  --mount-type virtiofs
```

Adjust `--cpu` and `--memory` based on your Mac (e.g., 6/16 for Pro, 8/32 for Max).

| Option | Benefit |
|--------|---------|
| `--vm-type vz` | Apple Virtualization.framework (faster than QEMU) |
| `--mount-type virtiofs` | 5-10x faster file I/O than sshfs |
| `--vz-rosetta` | Run x86 containers via Rosetta |

Verify with `colima status` - should show "macOS Virtualization.Framework" and "virtiofs".

</details>

## Quick Start

Choose the pattern that fits your workflow:

### Pattern A: Per-Project Container (Isolated)

Each project gets its own container with independent volumes. Best for one-off reviews, untrusted repos, or when you need isolation between projects.

```bash
git clone <untrusted-repo>
cd untrusted-repo
devc .          # Installs template + starts container
devc shell      # Opens shell in container
```

### Pattern B: Shared Workspace Container (Grouped)

A parent directory contains the devcontainer config, and you clone multiple repos inside. Shared volumes across all repos. Best for client engagements, related repositories, or ongoing work.

```bash
# Create workspace for a client engagement
mkdir -p ~/sandbox/client-name
cd ~/sandbox/client-name
devc .          # Install template + start container
devc shell      # Opens shell in container

# Inside container:
git clone <client-repo-1>
git clone <client-repo-2>
cd client-repo-1
claude          # Ready to work
```

## CLI Helper Commands

```
devc .              Install template + start container in current directory
devc up             Start the devcontainer
devc rebuild        Rebuild container (preserves persistent volumes)
devc down           Stop the container
devc shell          Open zsh shell in container
devc exec CMD       Execute command inside the container
devc upgrade        Upgrade Claude Code in the container
devc mount SRC DST  Add a bind mount (host → container)
devc cp CONT HOST   Copy files/directories from container to host
devc firewall CMD   Manage container network firewall (on/off/status)
devc template DIR   Copy devcontainer files to directory
devc self-install   Install devc to ~/.local/bin
devc update         Update devc to latest version
```

## File Sharing

### `devc mount`

To make a host directory available inside the container:

```bash
devc mount ~/drop /drop           # Read-write
devc mount ~/secrets /secrets --readonly
```

This adds a bind mount to `docker-compose.yml` and recreates the container.

**Tip:** A shared "drop folder" is useful for passing files in without mounting your entire home directory.

> **Security note:** Avoid mounting large host directories (e.g., `$HOME`). Every mounted path is writable from inside the container unless `--readonly` is specified, which undermines the filesystem isolation this project provides.

## Network Isolation

By default, containers have full outbound network access. Use `devc firewall` to restrict egress to an allowlist of domains.

### When to Enable Network Isolation

- Reviewing code that may contain malicious dependencies
- Auditing software with telemetry or phone-home behavior
- Maximum isolation for highly sensitive reviews

### Usage

```bash
devc firewall on                # Enable with default allowlist
devc firewall on ./rules.conf   # Enable with custom allowlist
devc firewall status            # Show current iptables rules
devc firewall off               # Disable (restore full access)
```

The default allowlist (`config/firewall-defaults.conf`) permits:

- `api.anthropic.com` — Claude API

Additional domains (GitHub, npm, PyPI) are available as commented-out entries in the config file.

### Custom Rules

Copy the defaults and edit:

```bash
cp config/firewall-defaults.conf my-rules.conf
# Edit my-rules.conf — one domain per line, # for comments
devc firewall on ./my-rules.conf
```

### How It Works

Firewall rules are applied from the host via `docker compose exec --user root`, not via sudo inside the container. This means the unprivileged container user cannot modify or disable the rules — they are locked down after setup.

### Trade-offs

- Blocks package managers unless you allowlist registries
- May break tools that require network access
- DNS resolution still works (consider blocking if paranoid)

## Security Model

This devcontainer provides **filesystem isolation** but not complete sandboxing.

**Sandboxed:** Filesystem (host files inaccessible), processes (isolated from host), package installations (stay in container)

**Not sandboxed:** Network (full outbound by default—see [Network Isolation](#network-isolation)), Docker socket (not mounted by default)

The container auto-configures `bypassPermissions` mode—Claude runs commands without confirmation. This would be risky on a host machine, but the container itself is the sandbox.

For a detailed analysis of what remains exposed, known gaps, and future hardening directions, see [SECURITY.md](SECURITY.md).

## Container Details

| Component | Details |
|-----------|---------|
| Base | Nix (`dockerTools.buildLayeredImage`), Node.js 22, Python 3.13 + uv, zsh |
| User | Unprivileged (UID 1000, no sudo), working dir `/workspace` |
| Tools | `rg`, `fd`, `fzf`, `delta`, `ast-grep`, `tmux`, `jq`, `vim`, `iptables`, `ipset` |
| Volumes (survive rebuilds) | Command history (`/commandhistory`), Claude config (`/env/.claude`) |
| Host mounts | None by default |
| Auto-configured | Claude Code (via [llm-agents.nix](https://github.com/numtide/llm-agents.nix)), [anthropics](https://github.com/anthropics/skills) + [trailofbits](https://github.com/trailofbits/skills) skills, git-delta |

All packages and config are baked into the image at build time via Nix — no runtime downloads. Skills are stored in the Nix store and referenced by path. Volumes persist shell history and Claude settings across rebuilds.

## Troubleshooting

### Container won't start

1. Check Docker is running
2. Try rebuilding: `devc rebuild`
3. Check logs: `docker logs $(docker ps -lq)`


### Python/uv not working

Python is managed via uv:

```bash
uv run script.py              # Run a script
uv add package                # Add project dependency
uv run --with requests py.py  # Ad-hoc dependency
```

## Development

Build the image manually:

```bash
nix build .#container -L
docker load < result
```

Test the container:

```bash
docker compose up -d
docker compose exec shell zsh
```
