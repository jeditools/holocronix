# Related work

Point-in-time reviews of other agent sandboxes, written to decide what
holocronix should borrow, what it should not, and where its own design
stands. Each entry records the versions reviewed; the projects move fast
and the conclusions may not survive their next release.

## vmpi and Gondolin

Reviewed 2026-09-15.

| Project | Reviewed at | What it is |
|---------|-------------|------------|
| [vmpi](https://github.com/JoshMock/the-agency/tree/main/packages/vmpi) | 0.4.2 (2026-09-11), `the-agency` main at `0280b93a40c8` | npm package `@the-agency/vmpi`, a TypeScript CLI that runs the `pi` coding agent inside a QEMU microVM |
| [Gondolin](https://github.com/earendil-works/gondolin) | main at `29fa74d80211` (2026-07-06), guest image `alpine-base` 0.2.0 | npm package `@earendil-works/gondolin` from Earendil Inc., Apache-2.0, the microVM library vmpi wraps |

vmpi is small: one main source file of about 29 KB plus config and session
helpers. It has had ten releases since 0.1.1 on 2026-04-16. It supports
one agent (`pi`), one user, and interactive runs only. Gondolin describes
itself as experimental and notes that it was built with the support of
coding agents.

### How a vmpi run works

`vmpi setup` builds a base checkpoint once:

1. Download the `pi` tarball from npm on the host, verifying the npm
   SHA-512 integrity field.
2. Run `npm install` on the host with `--os=linux --libc=musl` so native
   modules match the Alpine guest, then tar `node_modules`.
3. Boot a fresh Gondolin VM (Alpine 3.23.0, `linux-virt` kernel, guest
   assets fetched from GitHub releases and pinned by SHA-256 in
   Gondolin's `builtin-image-registry.json`, about 300 MB).
4. Write the tarball into the guest at `/opt/pi-modules.tgz` over the
   virtio-serial control channel, `apk add` the default and configured
   packages, and run any `postSetupHooks` with unrestricted network.
5. Save a disk-only qcow2 checkpoint at `~/.vmpi/base-checkpoint.qcow2`.

Every `vmpi` invocation then:

1. Boots a fresh VM from a copy-on-write overlay of the checkpoint. There
   is no RAM snapshot; "resume in about a second" is a fresh boot from a
   known disk.
2. Mounts the host's current directory read-write at `/workspace` and a
   snapshot copy of `~/.pi` at `/root/.pi`, both through Gondolin's VFS.
3. Extracts the `pi` bundle into a tmpfs `/tmp` (remounted to 75% of
   guest RAM) rather than running it from the VFS mount.
4. Writes placeholder secret values into a tmpfs env file, then runs
   `pi` with a PTY attached to the host terminal.
5. Merges any session files back into the host's `~/.pi` and discards
   the VM.

Defaults are 1024 MiB of RAM and one vCPU. The guest rootfs ships with
about 90 MiB free, so setup grows it by 128 MiB using `qemu-img` and
`resize2fs`, which needs e2fsprogs on the host.

### Gondolin's runtime model

**Isolation.** QEMU with a minimal virtio-only device set. KVM on Linux,
HVF on macOS, software emulation as a slow fallback. No root: the only
host requirement is a group-readable `/dev/kvm`. An experimental libkrun
backend exists. Gondolin's stated non-goals are QEMU escapes, side
channels, denial of service, and attackers who share the host user
account. No hardening of the QEMU process itself (seccomp, jailer,
privilege drop) is documented.

**Network.** The guest sees a normal `eth0`, but every frame is
terminated by a userspace network stack on the host. Outbound TCP flows
are classified by inspecting the byte stream: HTTP/1.x is parsed and
replayed with `fetch`; TLS is intercepted by SNI with a per-VM CA that
the guest trusts; SSH is proxied only when explicitly enabled; hosts
listed under `tcp.hosts` are forwarded as raw tunnels; anything else is
dropped. HTTP CONNECT is refused. UDP is DNS only. DNS defaults to a
synthetic mode where the host answers without an upstream resolver.
Private and link-local ranges, including cloud metadata, are blocked
unless explicitly mapped. Allowlist decisions are rechecked when the
upstream connection is made, which closes DNS rebinding. Not supported:
HTTP/2, HTTP/3, QUIC, WebRTC, generic UDP.

**Secrets.** The guest environment holds placeholders of the form
`<marker>.<name>`. The host substitutes real values into request headers
only for hosts the secret is scoped to; a request carrying a placeholder
to any other host is blocked. Bodies, paths, and responses are never
rewritten. Custom request hooks may observe real values after
substitution.

**Filesystem.** Host directories are served over FUSE: a guest daemon
forwards each operation over virtio-serial to a host-side provider.
Providers include real filesystem, in-memory, read-only, and a shadow
provider that hides paths such as `.env`. Symlinks escaping the mount are
blocked, names are single components, and each RPC payload is capped at
60 KiB. vmpi uses the real filesystem provider directly with no shadowing.

**Persistence.** Checkpoints are disk-only. `/root`, `/tmp`, `/var/tmp`,
`/var/cache`, and `/var/log` are tmpfs in the guest and never persist.
VFS mounts are not part of checkpoints.

**Custom images.** The image builder takes a JSON config. Only Alpine is
supported as the distro, but an `oci` section lets an OCI image supply
the rootfs, with Alpine still providing the kernel and initramfs. The
rootfs must contain `/bin/sh` or a custom init. Output is a kernel, an
initramfs, an ext4 rootfs, and a manifest with checksums, referenced from
the SDK via `imagePath`.

### Side by side

| Dimension | jedicave (Nix/Guix image, Docker runtime) | vmpi (Gondolin microVM) |
|-----------|--------------------------------------------|-------------------------|
| Isolation boundary | Host kernel, namespaces, seccomp, `no-new-privileges` | Separate guest kernel under QEMU |
| Egress control | iptables allowlist by IP, all ports and protocols; optional mitmproxy for 80/443 | Userspace stack, default deny, unclassified TCP dropped, UDP is DNS only |
| DNS | `open` by default; `trusted` or `synthetic` opt-in | Synthetic by default, rebinding re-check at connect |
| Secrets | `env` mode by default (real value in container); `proxy` mode with placeholders opt-in | Placeholders only, header substitution scoped per host |
| Resource limits | None by default | RAM and vCPU fixed by VM sizing |
| Host privileges | Docker daemon (root-equivalent), `NET_ADMIN` in the container | User process plus `/dev/kvm` |
| Image provenance | Content-addressed derivation; every input pinned in `flake.lock` or channel hashes | Guest base pinned by SHA-256; `apk add` and post-setup hooks unpinned and run with open network; checkpoint is a mutable local file |
| Toolchains | Baked: compilers, cross toolchains, devShells, vendored crates; offline builds | Alpine/musl packages at setup; project dependencies fetched by the agent at run time |
| Workspace | Bare repo mounted read-only, cloned inside; host `.git` never exposed | Host working directory, including `.git`, mounted read-write over FUSE |
| Workload shape | Long-lived cave, `exec`, `enter`, tmux, named volumes | One ephemeral interactive session per run |
| Agents | Claude Code, OpenCode, Qwen Code, Kimi, configurable | `pi` only |
| Platforms | Linux x86_64 | Linux x86_64 (KVM), macOS Apple Silicon (HVF) |
| Workspace I/O | Bind mount or container filesystem, native speed | FUSE over virtio-serial RPC with 60 KiB payloads |
| Protocol coverage | Anything iptables passes | HTTP/1.x and TLS-wrapped HTTP/1.x, SSH opt-in, no HTTP/2 or QUIC |
| Maturity | Early stage, tested on one host setup | vmpi five months old; Gondolin experimental by its own description |

### Where vmpi and Gondolin are stronger

- **The boundary.** A guest-kernel exploit such as CVE-2026-31431 ends
  in the guest. `SECURITY.md` names the shared kernel as the fundamental
  limit of the container design, and this is the answer to it.
- **Egress that iptables cannot express.** Our allowlist rules accept
  every port and protocol on a resolved IP, and with the proxy enabled
  only 80 and 443 are forced through it. Gondolin drops any TCP flow it
  cannot classify, allows no UDP but DNS, refuses CONNECT, and blocks
  private ranges and metadata by default. The nearest we can get is
  documented under "Default-deny egress" in `SECURITY.md`.
- **Secrets by construction.** Our proxy-mode placeholders are the same
  idea, but env mode is the default and puts real values in the
  container.
- **Hard resource caps** from VM sizing, where we have none.
- **No root daemon, and macOS.** Docker Engine is a root-equivalent trust
  dependency; Gondolin is a user process.

### Where holocronix is stronger

- **Reproducible identity.** A jedicave is a derivation: rebuild it
  anywhere and get the same store paths. A vmpi checkpoint is whatever
  `apk` and the post-setup hooks produced on the day it was built, with
  no lock and open network during setup. Only the `pi` tarball is
  integrity-checked.
- **Baked toolchains and offline builds.** Cross toolchains, project
  devShells, and vendored crates are in the image; nothing is downloaded
  at run time. vmpi offers Alpine packages and expects the agent to fetch
  dependencies through the allowlist.
- **The host repository is never exposed.** vmpi mounts the working
  directory, `.git` included, read-write. The bare-repo handoff exists
  because of exactly the hook and ref tampering that allows.
- **Workload shape.** Long-lived caves you enter with tmux, several
  agents, skills and plugins baked in.
- **Build performance, most likely.** A cargo target directory or
  `node_modules` on a FUSE mount with small RPC payloads will be slow.
  vmpi itself extracts `pi` into tmpfs rather than run it from the
  mount. Not measured here.
- **Protocol coverage.** Anything HTTP/2-only or UDP-based does not work
  under Gondolin's stack.

### What we take from it

- **Not vmpi.** It is `pi`-specific glue. Everything of interest is in
  Gondolin.
- **Gondolin as a runtime backend, to be spiked.** Its OCI rootfs support
  means a Nix- or Guix-built jedicave could boot under it with the baking
  layer untouched. It would replace the compose, iptables, and mitmproxy
  layers rather than plug into them. Open questions and the plan are in
  `ROADMAP.md` under "Runtime isolation".
- **Policy hardening now, inside the Docker design.** Default-deny
  egress, port-matched allowlist rules, blocked private ranges and
  metadata, synthetic DNS by default, secrets defaulting to proxy mode,
  and cgroup limits. Each is listed in `SECURITY.md` under "Future
  hardening".

### Sources

- vmpi README, `vmpi.ts`, `config.ts`, and `CHANGELOG.md` at
  `the-agency` commit `0280b93a40c8`
- Gondolin docs at commit `29fa74d80211`: `docs/security.md`,
  `docs/network.md`, `docs/secrets.md`, `docs/vfs.md`,
  `docs/snapshots.md`, `docs/limitations.md`, `docs/custom-images.md`,
  `docs/architecture.md`, `docs/qemu.md`, `docs/backends.md`
- Gondolin `builtin-image-registry.json` and `images/alpine-base.json`
