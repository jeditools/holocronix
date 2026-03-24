# Volume Integrity Plan

## Problem

The entire `/env/.claude` directory is a single writable named volume (`{name}-config`).
This means settings.json, plugins, and memory/projects are all equally writable from
inside the container. A compromised process can modify Claude's settings (e.g. disable
hooks, change permissions) and the change persists across restarts.

## Current Architecture

```
Named volume: {name}-config  -->  /env/.claude/  (rw)
  ├── settings.json                 # Nix-built, copied on first boot
  ├── plugins/
  │   └── known_marketplaces.json   # Nix-built, copied on first boot
  └── projects/                     # Claude auto-memory (written at runtime)
      └── ...
```

The entrypoint (`jedicave-start`) copies settings from the Nix store into the volume
on first boot, then never touches them again. The volume is fully writable.

Additionally, named Docker volumes live inside Docker's storage (`/var/lib/docker/volumes/`)
— they are managed by Docker, not directly visible as regular host directories. If the
volume is removed (`docker volume rm`, `docker volume prune`, `jedi destroy`), or Docker
itself has a problem, the mutable state is lost with no recovery path.

The mutable state at risk:
- **Auto-memory** (`/env/.claude/projects/...`) — Claude's accumulated project knowledge
- **Shell history** (`/commandhistory/`) — zsh/bash command history

Neither is backed up to a host path the user controls.

## Goal

Split the volume so that Nix-built config is read-only and only mutable state
(auto-memory, project config) lives on a writable volume. Add checksum verification
for the writable state. Provide a path to kernel-level integrity (fs-verity) for
the read-only files. Decide on a storage strategy for mutable state that balances
persistence, portability, and security.

---

## Prerequisite: Storage Strategy for Mutable State

Before implementing integrity checks, we need to decide how mutable state is stored.
The current approach (named Docker volumes) has persistence risks. Two options:

### Option A: Host bind mounts

Mount host directories directly into the container:

```yaml
volumes:
  - ./state/claude:/env/.claude
  - ./state/history:/commandhistory
```

State lives at `~/.config/jedicaves/{name}/state/` on the host.

| Pro | Con |
|-----|-----|
| Data survives `docker volume prune/rm` | Host path structure leaks into container |
| Trivially backed up (rsync, git, etc.) | Permissions must match (UID 1000 on host) |
| Host-side checksums work directly on files | A compromised container writes to a known host path |
| `jedi destroy` can prompt separately for state deletion | Slightly more complex compose template |
| No Docker dependency for data access | |

### Option B: Named Docker volumes (current approach)

Keep using Docker-managed volumes:

```yaml
volumes:
  - {name}-claude-state:/env/.claude
  - {name}-history:/commandhistory
```

| Pro | Con |
|-----|-----|
| Clean separation — Docker manages storage | Data lost on `docker volume rm/prune` |
| No host path assumptions | Checksum verification needs a throwaway container |
| Works across storage drivers (overlay2, btrfs, zfs) | Not trivially backed up without extra tooling |
| Portable compose files | Opaque — user can't browse state without `docker run` |

### Option C: Hybrid

Use named volumes but add `jedi backup` / `jedi restore` commands that sync volume
contents to the cave directory on the host. Combines Docker's clean volume management
with host-side persistence as an explicit opt-in.

| Pro | Con |
|-----|-----|
| Best of both — Docker manages runtime, host has backups | Two copies of data to keep in sync |
| Backup is explicit, not automatic | User must remember to run `jedi backup` |
| Could auto-backup on `jedi down` | Adds complexity to the CLI |

### Decision

TBD — evaluate tradeoffs in context of the integrity phases below. The choice
affects Phase 2 implementation (checksums are simpler with bind mounts) and the
migration path from existing named volumes.

---

## Phase 1: Read-only bind mounts for Nix-built config

### 1.1 Extract config paths from the image

`mkJediCave.nix` already produces `claudeSettings` and `knownMarketplaces` as Nix
store paths. Expose them as separate image outputs or as files at well-known image
paths so the compose layer can reference them.

**Approach**: Bake the files into the image at fixed paths under `/nix-config/claude/`:

```
/nix-config/claude/settings.json
/nix-config/claude/plugins/known_marketplaces.json
```

In `fakeRootCommands`:

```nix
mkdir -p ./nix-config/claude/plugins
cp ${claudeSettings}    ./nix-config/claude/settings.json
cp ${knownMarketplaces} ./nix-config/claude/plugins/known_marketplaces.json
```

These paths are part of the image layer and are immutable (Docker image layers are
read-only by default).

### 1.2 Split the volume mount

Replace the single `{name}-config` volume with:

| Mount | Source | Target | Mode |
|-------|--------|--------|------|
| Image layer (built-in) | `/nix-config/claude/settings.json` | — | ro (immutable) |
| Image layer (built-in) | `/nix-config/claude/plugins/` | — | ro (immutable) |
| Named volume | `{name}-claude-state` | `/env/.claude` | rw |

The entrypoint symlinks the read-only files into the writable volume so Claude sees
a unified `CLAUDE_CONFIG_DIR`:

```bash
# In jedicave-start, on every boot (not just first boot):
mkdir -p "$CLAUDE_CONFIG_DIR/plugins"
ln -sf /nix-config/claude/settings.json      "$CLAUDE_CONFIG_DIR/settings.json"
ln -sf /nix-config/claude/plugins/known_marketplaces.json \
       "$CLAUDE_CONFIG_DIR/plugins/known_marketplaces.json"
```

Because the symlink targets are in an image layer, writing to them from inside the
container has no effect — the source is read-only. If Claude Code follows symlinks
and tries to write, it gets EROFS. If it unlinks and recreates, the symlink is gone
but the Nix-built original is untouched; the next restart restores the symlink.

### 1.3 Update compose template

Update the compose template to separate mutable state from config. The exact
volume strategy (bind mounts vs named volumes) depends on the storage strategy
decision above. Either way, the old `{name}-config` volume is replaced — config
files come from the image layer, only mutable state needs a writable mount.

Example with named volumes:

```yaml
volumes:
  - {name}-history:/commandhistory
  - {name}-claude-state:/env/.claude
```

Example with bind mounts:

```yaml
volumes:
  - ./state/history:/commandhistory
  - ./state/claude:/env/.claude
```

### 1.4 Remove first-boot copy logic

The entrypoint currently copies settings on first boot (`[ -f ... ] || cp ...`).
Replace this with the symlink logic from 1.2 that runs on every start. Remove the
`.jedicave-initialized` sentinel — symlinks are idempotent.

### 1.5 Update jedi CLI

- `cmd_init`: Generate the new compose template (and create host directories if
  using bind mounts).
- Add a `jedi migrate` command (or handle in `jedi up`) that detects old-style
  `{name}-config` volumes and prints migration instructions.

### Files changed

- `lib/mkJediCave.nix` — add `/nix-config/` paths, update entrypoint
- `cli/jedi.py` — update `COMPOSE_TEMPLATE`, add migration notice
- `SECURITY.md` — update volume documentation

---

## Phase 2: Checksums for mutable state (Option 1)

The writable volume (`{name}-claude-state`) holds Claude's auto-memory and project
configs. These can't be made read-only — Claude writes to them at runtime. Instead,
add checksum verification to detect tampering between sessions.

### 2.1 Digest generation on stop

Add a `jedi seal <name>` command (also called automatically by `jedi down`):

```bash
docker run --rm -v {name}-claude-state:/data alpine \
  sh -c 'find /data -type f | sort | xargs sha256sum' \
  > ~/.config/jedicaves/{name}/volume.sha256
```

The digest file lives on the host, outside the container's reach.

### 2.2 Digest verification on start

Add a `jedi verify <name>` command (also called automatically by `jedi up`):

```bash
docker run --rm -v {name}-claude-state:/data alpine \
  sh -c 'find /data -type f | sort | xargs sha256sum' \
  | diff - ~/.config/jedicaves/{name}/volume.sha256
```

On mismatch:

```
[jedi] WARNING: Volume integrity check failed for '{name}'
[jedi] The following files changed since last 'jedi down':
  /data/projects/.../MEMORY.md
[jedi] This may indicate tampering. Continue? [y/N]
```

### 2.3 First seal

On first `jedi down` (no existing digest), generate the digest silently.
On first `jedi up` with no digest, skip verification with an info message.

### 2.4 Seal for history volume too

Apply the same seal/verify to `{name}-history` (shell history). A poisoned
`.zsh_history` could execute commands on the next interactive session.

### Files changed

- `cli/jedi.py` — add `seal`, `verify` commands; hook into `up`/`down`

---

## Phase 3: fs-verity for read-only config (Option 3)

Replace the symlink approach from Phase 1 with kernel-enforced integrity using
fs-verity. This makes it cryptographically impossible to read tampered config
even if the filesystem is writable.

### Prerequisites

- Kernel 5.4+ with `CONFIG_FS_VERITY` (standard on Fedora, Ubuntu 20.04+)
- Filesystem that supports fs-verity: ext4 or f2fs (NOT overlayfs or tmpfs)
- `fsverity` userspace utility

### 3.1 Why not just keep the Phase 1 symlinks?

Phase 1 protects against casual modification but not against:
- An attacker who gains root inside the container (can remount image layers rw)
- A Docker bug that exposes image layers as writable
- Supply chain attacks that modify the image between builds

fs-verity provides a Merkle tree over file contents. Any read of a tampered block
returns EIO. The root hash is stored in an inode attribute — modifying the file
invalidates the hash and the kernel refuses to serve the data.

### 3.2 Implementation plan

This phase requires the config files to live on an ext4/f2fs filesystem, not in
a Docker image layer (which uses overlayfs). Options:

**Option A: ext4 volume image**

Build a small ext4 disk image in Nix containing the config files with fs-verity
enabled. Mount it as a block device inside the container.

```nix
# In mkJediCave.nix
configImage = pkgs.runCommand "claude-config.img" { ... } ''
  truncate -s 4M $out
  mkfs.ext4 $out
  mount -o loop $out /mnt
  cp ${claudeSettings} /mnt/settings.json
  mkdir -p /mnt/plugins
  cp ${knownMarketplaces} /mnt/plugins/known_marketplaces.json
  fsverity enable /mnt/settings.json
  fsverity enable /mnt/plugins/known_marketplaces.json
  umount /mnt
'';
```

In compose.yml, mount as a read-only block device via `--device` or a tmpfs with
the image loopback-mounted by the entrypoint.

**Option B: fs-verity on the writable volume (hybrid)**

Use fs-verity on the *writable* volume's config files. The entrypoint copies files
from the image layer and enables fs-verity on them. Once enabled, fs-verity is
immutable — the file cannot be modified even by root without the kernel rejecting
reads.

```bash
# In jedicave-start:
cp /nix-config/claude/settings.json "$CLAUDE_CONFIG_DIR/settings.json"
fsverity enable "$CLAUDE_CONFIG_DIR/settings.json"
```

Caveat: requires the Docker volume's backing filesystem to support fs-verity.
Docker's default storage driver (overlay2) stores volumes on the host filesystem
(typically ext4), so this works if the host uses ext4.

### 3.3 Verification

Store the expected fs-verity digests (Merkle root hashes) in the Nix derivation
and verify on each boot:

```bash
expected="sha256:abc123..."
actual=$(fsverity measure "$CLAUDE_CONFIG_DIR/settings.json" | awk '{print $1}')
if [ "$expected" != "$actual" ]; then
  echo "[jedicave] INTEGRITY FAILURE: settings.json has been tampered with" >&2
  exit 1
fi
```

The expected hashes are baked into the image — an attacker would need to modify both
the file and the image to bypass this.

### 3.4 Fallback

If the kernel or filesystem doesn't support fs-verity, fall back to Phase 1
(symlinks + Phase 2 checksums) with a warning:

```
[jedicave] WARNING: fs-verity not available, falling back to symlink protection
```

### Files changed

- `lib/mkJediCave.nix` — add fsverity setup to entrypoint, embed expected hashes
- `flake.nix` — add `fsverity` to container packages (if not already present)

---

## Migration Path

| Phase | Protection level | Mutable state | Effort |
|-------|-----------------|---------------|--------|
| 1 | Config is read-only (symlinks to image layer) | Unverified | Small |
| 2 | Config is read-only + mutable state checksummed between sessions | Detected | Small |
| 3 | Config has kernel-enforced integrity (fs-verity) + checksums | Detected | Medium |

Phases 1 and 2 can ship together. Phase 3 is independent and can be added later
without changing the user-facing interface (same compose.yml, same CLI commands).

## Open Questions

- Should `jedi seal` also snapshot the symlink targets (defense in depth)?
- Should checksums be signed (GPG/age) to prevent an attacker who gains host access
  from regenerating valid digests after tampering?
- For Phase 3 Option A: can Nix's sandbox create loopback mounts during build? If
  not, the ext4 image must be built outside the sandbox or use `requiredSystemFeatures`.
- Should there be a `jedi audit` command that summarizes the integrity posture of a
  cave (which protections are active, last seal time, verification status)?
