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

## coop

Reviewed 2026-09-27.

| Project | Reviewed at | What it is |
|---------|-------------|------------|
| [coop](https://github.com/trailofbits/coop) | v0.6.0 (2026-09-09), main at `6ac6c2c5e739` (2026-09-26) | Rust CLI from Trail of Bits, Apache-2.0, that runs Claude Code and Codex in Firecracker microVMs on Linux and Lima VMs on macOS |

coop is a serious codebase: about 46,000 lines of Rust in the main crate
with tests included, 1,225 unit tests, fuzz targets, bounded proofs under
kani, mutation testing, and integration suites that run on both
backends. It has 15 tags since its first commit on 2026-04-02. It
supports two agents, Claude Code and Codex, and nothing else. Its
`docs/trust-model.md` is the clearest statement of trust boundaries and
taint sources of any project reviewed here.

### How a coop run works

`coop setup` builds a golden image once per image name:

1. On Linux, download the latest Firecracker release tarball from
   GitHub, then a minimal CI kernel (6.1 series at review time) and an
   Ubuntu-based CI squashfs rootfs from Firecracker's public S3 bucket.
   The setup code checks no checksum on any of the three. On macOS, a
   Lima template names the Ubuntu 24.04 cloud image by URL without a
   digest.
2. Unpack the rootfs into an 8 GiB ext4 image and provision it in a
   chroot on the host (Linux) or a throwaway builder VM (macOS): apt
   installs Docker CE, the GitHub CLI, `build-essential` and friends;
   Claude Code and Codex come from their vendors' `curl | bash`
   installers; profiles add apt packages and run `pre_install` and
   `post_install` shell as root with network; devcontainer Features are
   pulled from GHCR and their `install.sh` run.
3. Write `template-config.json`: a version counter, a SHA-256 of the
   composed install *script*, the post-install script hash, and the
   profile and plugin lists. A later `coop setup` rebuilds only when
   that recipe hash changes. Agent versions are not part of it.

`coop up <dir>` then creates and boots an instance:

1. Reflink-copy the template to the instance rootfs, patch the static
   IP and hostname, grow the disk if asked.
2. On Linux, create a TAP device on a `br0` bridge at `172.16.0.1/24`,
   mark the port isolated, insert a `FORWARD` drop for bridge-to-bridge
   traffic, add `MASQUERADE`, and start Firecracker under `sudo`. The
   jailer binary is downloaded but not used; Firecracker's built-in
   seccomp filter applies by default. On macOS, `limactl start` with
   `vmType: vz` and a per-instance user-mode network.
3. Wait for SSH with a per-installation ed25519 key and host-key
   checking disabled.
4. Bootstrap the agents: `gh auth setup-git` if a GitHub token is
   configured, overlay an allowlist of `~/.claude` content, write a
   managed `settings.json` with `bypassPermissions`, install the delta
   of marketplaces, plugins, and MCP servers not already baked in.
5. Bring in the workspace: by default a tar-pipe copy of the project
   directory, `.git` included, into `/workspace` on the guest disk with
   SHA-256 checked on both ends; or `--mount` (live virtiofs on Lima,
   one-time rsync on Firecracker); or `--git-repo` cloned inside.

The instance is long-lived: `coop claude`, `coop codex`, `coop shell`,
and `coop exec` go over SSH; `coop push` and `coop pull` rsync with dirty
checks on both sides; `coop commit` and `coop restore` snapshot the disk;
`coop editor` attaches VS Code or Zed; `coop resize` changes disk, RAM,
and vCPUs. Defaults are 2 vCPUs, 4 GiB of RAM, and an 8 GiB disk. Up to
253 instances share the bridge subnet.

### coop's runtime model

**Isolation.** Firecracker on KVM, or Apple's Virtualization framework
through Lima. The VM is the stated boundary and the guest is deliberately
permissive: passwordless `sudo`, agents in bypass mode, a full Docker
daemon inside the guest, and a `root:root` serial console. Guests cannot
reach each other, enforced by two independent controls that are asserted
on every start and fail closed. Guests can reach the host by design. On
Linux the host side needs `sudo` for Firecracker, TAP and bridge setup,
iptables, and the setup chroot with its loop mounts. Out of scope by
policy: attackers who already control the host, and bugs in Firecracker,
Lima, Docker, or the agents.

**Network.** Open egress. Guest traffic is NATed through the host's
default interface with no allowlist, no port matching, and no DNS
filtering; egress control is coop's open issue #2. On Firecracker the
guest's resolvers are hard-coded to Google's. Port forwards bind
`127.0.0.1` only.

**Secrets.** By default the model API keys ride SSH `SendEnv` into the
guest's environment: never on the guest disk, but readable by anything
the agent runs. `GITHUB_TOKEN` is off by default; when enabled, `gh auth
setup-git` turns it into persistent guest state. Config values use a
`cmd:` prefix so the plaintext lives in Keychain, 1Password, Secret
Service, or a mode-0600 file, the same idea as our `value_cmd`. The
opt-in `[proxy]` mode is the strong part: a host-side `coop-proxy`
process binds loopback, is reverse-tunnelled into the guest over
`ssh -R`, and the guest holds only a per-instance capability token. The
proxy checks the token in constant time, injects the real key, and
default-denies every operation except three method-and-path pairs
(`POST /v1/messages`, `POST /v1/messages/count_tokens`, `POST
/v1/responses`) to a pinned upstream over verified TLS. It is jailed
with Landlock on Linux (no filesystem writes, no exec, TCP egress
limited to 443 and 53 on kernels 6.7 and later) or Seatbelt on macOS,
and refuses to start if the jail cannot be applied. coop is explicit
that this stops key exfiltration, not key use, and provides no egress
control.

**Workspace.** The guest disk holds `/workspace`; the host directory is
copied in and pulled back. `.git` goes both ways by default, and coop
names `pull` as "the widest guest-to-host channel". Its own docs record
that a guest can write `core.hooksPath` or `core.worktree` into a shared
`.git/config` and that `pull` can carry such entries back to the host.

**Persistence.** Instances survive stop and start. `commit` and
`restore` are disk-only checkpoints of a stopped instance.

**Images.** coop builds its own Ubuntu rootfs and nothing else. The
devcontainer `image` and `build` keys are reported as unsupported, and
there is no way to boot a supplied OCI image or rootfs.

**Tool provenance.** `coop update` requires a matching `SHA256SUMS` and
verifies a Sigstore build attestation when `gh` is present. The trust
model documents what that chain does and does not pin, including that
any workflow in the repo could mint a passing bundle.

### Side by side

| Dimension | jedicave (Nix/Guix image, Docker runtime) | coop (Firecracker or Lima VM) |
|-----------|--------------------------------------------|-------------------------------|
| Isolation boundary | Host kernel, namespaces, seccomp, `no-new-privileges` | Separate guest kernel under Firecracker or Virtualization.framework |
| Egress control | iptables allowlist by IP, all ports; optional mitmproxy for 80/443 | None; NAT to the internet, open issue #2 |
| DNS | `open` by default; `trusted` or `synthetic` opt-in | Hard-coded public resolvers, no filtering |
| Secrets | `env` mode by default; `proxy` mode substitutes headers for allowed domains | `SendEnv` by default; opt-in jailed host proxy with a three-operation allowlist and capability token |
| Resource limits | None by default | Fixed by VM sizing |
| Host privileges | Docker daemon, `NET_ADMIN` in the container | `sudo` for Firecracker, TAP, iptables, and the setup chroot; none on macOS |
| Image provenance | Content-addressed derivation, every input pinned | Recipe hash over the install script; apt, vendor installers, Firecracker binary, kernel, and rootfs all fetched unpinned and unverified |
| Toolchains | Baked, including cross toolchains and vendored crates; offline builds | apt profiles and rustup at setup; project dependencies fetched by the agent at run time |
| Docker for the agent | Not available; mounting the socket is root on the host | Full daemon inside the guest |
| Workspace | Bare repo mounted read-only, cloned inside; host `.git` never exposed | Host directory copied in and pulled back with `.git`; live virtiofs on macOS |
| Workload shape | Long-lived cave, `exec`, `enter`, tmux, named volumes | Long-lived instance, `shell`, `exec`, editor over SSH, disk checkpoints, resize |
| Agents | Claude Code, OpenCode, Qwen Code, Kimi, configurable | Claude Code and Codex |
| Platforms | Linux x86_64 | Linux x86_64 and arm64 (KVM), macOS Apple Silicon |
| Guest-to-guest | Separate compose networks per cave | Isolated bridge ports plus a `FORWARD` drop, asserted per start |
| Engineering | Python CLI, tested on one host setup | 46k lines of Rust, unit, fuzz, mutation, kani, integration on both backends |

### Where coop is stronger

- **The boundary, and Docker behind it.** Same argument as Gondolin,
  plus one we could not make there: an agent can use `docker` because
  the daemon runs inside the guest. Our `SECURITY.md` can only say the
  socket must never be mounted.
- **The credential proxy.** Ours substitutes secrets and applies hooks;
  theirs also refuses every API operation the agent does not need, holds
  the key in a process that cannot write files or exec, and fails closed
  when that jail is unavailable. A stolen key used against the account or
  files API is blocked at the proxy, not just logged.
- **The trust model as a document.** Zones, taint sources, invariants
  the type system enforces, and a stop-and-confirm checklist for changes
  that widen a boundary. Every accepted trade-off is written down with
  its rationale.
- **Workflow surface.** Multiple instances per project, push and pull
  with dirty checks in both directions, disk checkpoints, editor
  attachment, port forwards, resize, and a `devcontainer.json` subset
  that maps onto its own primitives.
- **Hard resource caps and macOS**, as with vmpi.

### Where holocronix is stronger

- **Reproducible identity.** A coop image is whatever apt, two `curl |
  bash` installers, the profile scripts, and GHCR served on the day it
  was built, on top of an unverified rootfs and kernel, run by an
  unverified Firecracker binary. The recipe hash detects config drift,
  not upstream drift, and the agents' versions are outside it. Two
  people running `coop setup` get two different images, and neither can
  be rebuilt later.
- **Egress control exists.** coop has none. Everything the agent runs
  can reach anything, and the credential proxy's own docs say it does
  not change that.
- **The host repository is never exposed.** coop copies `.git` in and
  pulls it back, and documents the hook-path and worktree corruption
  that follows. The bare-repo handoff and bundle harvest exist for
  exactly this.
- **Build isolation.** Our image is built by Nix or Guix in a sandbox
  with no network beyond hash-pinned fetches. coop's Linux image is
  provisioned in a chroot on the host, as root, with network, running
  vendor install scripts; its trust model notes that a chroot is not VM
  isolation.
- **Baked toolchains and offline builds**, and more agents.
- **Guest-side hardening.** Unprivileged user, seccomp, no setuid, no
  `sudo`. coop does not need this under its model, but it means a cave
  degrades more gracefully if the boundary is ever the wrong one.

### What we take from it

- **Not coop as a runtime.** It cannot boot an image we build. But
  Firecracker is now a second candidate microVM backend next to
  Gondolin, and coop's host networking (`network.rs`: bridge, TAP,
  isolated ports, NAT) is the reference for what a Firecracker backend
  would need. Under such a backend our iptables allowlist moves from
  inside the container to the host's `FORWARD` chain, and `NET_ADMIN`
  leaves the guest. The Gondolin-versus-Firecracker trade-off is in
  `ROADMAP.md` under "Runtime isolation".
- **Docker inside the guest** is the concrete capability a VM runtime
  buys that no container hardening can.
- **An operation allowlist in the L7 proxy.** With the proxy on, restrict
  allowlisted model hosts to the method-and-path pairs the agent uses and
  return 403 for the rest. Cheap in `proxy-policy.py`; listed in
  `SECURITY.md` under "Future hardening".
- **Taint sources in `SECURITY.md`.** Name every guest-to-host channel
  (harvest, `jedi diff`, `jedi cp`, writable mounts) the way coop does,
  so a change that adds one is visible.
- **Not `cmd:` secrets.** `value_cmd` already does this.

### Sources

- coop at commit `6ac6c2c5e739`: `README.md`, `SECURITY.md`,
  `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/trust-model.md`,
  `docs/backends.md`, `docs/images-and-profiles.md`,
  `docs/workspaces.md`, `docs/credential-proxy.md`,
  `docs/platform-notes.md`, `docs/devcontainer.md`,
  `docs/getting-started.md`, `docs/multi-instance.md`,
  `docs/claude-integration.md`, `docs/design/issue-411-injecting-proxy.md`
- coop source: `src/setup.rs`, `src/vm.rs`, `src/network.rs`,
  `src/lima.rs`, `scripts/guest/*.sh`, `guest/init.sh`, `Cargo.toml`

## stagex

Reviewed 2026-09-22.

| Project | Reviewed at | What it is |
|---------|-------------|------------|
| [stagex](https://codeberg.org/stagex/stagex) | release 2026.06.0, whitepaper draft of March 2026 | A full-source bootstrapped, mandatory-reproducible, multi-signed Linux distribution whose packages are OCI images built from Containerfiles |

stagex is not an agent sandbox. It is reviewed here because its authors
argue that Containerfiles plus OCI are a better base than Guix or Nix, on
the grounds that OCI has many builders, that no single toolchain then
needs deep review, and that far more people read Dockerfiles than Scheme.
That argument shaped `VISION.md`.

### What it does

- Bootstraps from a 181-byte hex0 seed through live-bootstrap to a
  modern LLVM and musl toolchain, the same stage0 lineage Guix uses.
- Rejects any package that does not reproduce bit for bit. Every
  Containerfile pins its source by sha256, disables the network during
  the build, fixes the source date, and writes the OCI output with
  rewritten timestamps.
- Signs nothing until two independent maintainers have rebuilt it to the
  same digest. Signatures are PGP over the manifest digest and live in a
  separate repository. Every commit and merge is signed with hardware
  keys.
- Ships about 480 package recipes in four tiers: bootstrap, core, pallet,
  user. Pallets assemble a working runtime by copying the packages a
  toolchain needs into one image by hand.

### Where it is stronger

- Reproducibility as a hard gate rather than a goal.
- A two-party quorum before publication, which neither Guix nor Nix has.
- Recipes a Docker user can read in a minute.

### Where it is weaker

- The dependency graph exists only as `COPY --from` lines. There is no
  graph or tree tool, dependencies are build-time only, and a package
  image carries no runtime closure, so nothing checks that a pallet is
  complete.
- Only Docker with the containerd image store reproduces its digests, and
  only as root. buildah and podman are listed as coming soon, so the
  "many builders" argument is aspirational for stagex itself.
- x86_64 only. arm64 fixes have been open since February 2026 and riscv64
  fixes since September 2026, while Guix ships aarch64 with substitutes.
- musl by default, which breaks manylinux wheels and prebuilt npm native
  modules that an agent will install constantly. glibc exists in the user
  tier but the language pallets are musl.

### What we take from it

- Reproducibility as a gate, two-party reproduction before signing, and
  signatures kept in a repository. These are steps 4 and 5 of
  `VISION.md`.
- Not the Containerfile substrate, and not stagex as a backend. Possibly
  stagex images as a verified base once arm64 ships and a second builder
  reproduces them.

### Sources

- stagex README, `Makefile`, `src/targets.py`, `src/impact.py`,
  `src/fetch.py`, `src/sign.sh`, `packages/core/git/Containerfile` and
  `package.toml`, `packages/pallet/rust/Containerfile`, `digests/`
- stagex issue 1708 (rootless builds change digests) and the open arm64
  and riscv64 pull requests
- Livaja, Vick, Heywood, Grove, "StageX: Eliminating Single Points of
  Failure in Linux Distributions", draft, March 2026,
  <https://codeberg.org/stagex/whitepapers/src/branch/main/out/stagex.pdf>
