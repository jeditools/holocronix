# Roadmap

## Guix backend

Holocronix currently uses Nix to bake OCI container images. The plan is
to add Guix as a second "baking" backend so that caves can be built with
either Nix or Guix. The CLI, compose layer, firewall, and git handoff
are already backend-agnostic. The direction this serves, and the order
of the steps after the backend itself, is in `VISION.md`.

### Status

The work is split into sub-problems, tackled one at a time, each proven
on a dummy project before touching real ones. See `guix/README.md` for
usage.

| Sub-problem | State |
|-------------|-------|
| Baked Rust dependencies (crates.io) | Done. `cargo build` works offline in a `guix pack -f docker` image. |
| Baked Rust dependencies (git sources) | Done. Verified on a local fixture workspace and, to derivation level, on xous-core's two lockfiles (769 crates, 13 git repos). Each checkout gets its own vendor directory, since one crate name and version may come from several sources. `jedi lock` pins every git commit's hash in `vendor.lock.scm` via `guix download --git`, so no hash is ever copied by hand. |
| Xous cross toolchain in the image | Done. baobit's `rust-xous-toolchain` via load path under baobit's pinned Guix; std hello world cross-compiles offline. Channel form blocked by baobit's broken channel auth. |
| Image config: user, workdir, env, file ownership | Done. `jedicave-image` in `guix/holocronix/jedicave.scm` on a forked docker builder with `#:user`, `#:working-dir`, `#:owners`. Verified on `examples/hello-rust/cave.scm`. |
| CLI backend selection | Done. `jedi init --backend guix` scaffolds `cave.scm` + `channels.scm`; `build`, `update`, `inputs` dispatch on which file the cave has. |
| Agent tooling packaged for Guix | In progress. Claude Code is packaged in `(holocronix agents)`, and a Guix cave ships it by default with its settings and the plugin seed from `(holocronix claude)`. opencode, kimi-code, qwen-code and ori are not packaged yet. |
| Agent-facing model queries | Done. `jedi guix show|inputs|derivation|plan|references|referrers|size|graph|lint|search|classify` answer as JSON from `cli/query.scm`; `--cave` runs under the cave's pinned channels. Step 2 of `VISION.md`. |

### Architecture

```
holocronix/
├── cli/jedi.py              ← shared (backend-agnostic)
├── config/                   ← shared (zshrc, tmux, firewall, etc.)
├── lib/mkJediCave.nix        ← Nix backend
├── flake.nix                 ← Nix entry point
├── .guix-channel             ← makes this repo a Guix channel (directory "guix")
├── guix/                     ← Guix backend, a Guile load path (guix -L guix)
│   ├── README.md
│   └── holocronix/
│       ├── cargo-vendor.scm  ← baked Rust deps (done)
│       ├── docker.scm        ← fork of (guix docker): user, workdir, owners
│       └── jedicave.scm      ← image builder, Guix mkJediCave (done)
└── examples/                 ← hello-rust, hello-rust-git, hello-xous
```

### Mapping

| Concept | Nix | Guix |
|---------|-----|------|
| Image builder | `dockerTools.buildLayeredImage` | `guix pack -f docker --max-layers=N`, or `build-docker-image` from `(guix docker)` for full control |
| Input pinning | `flake.lock` | `channels.scm` from `guix describe -f channels`, run under `guix time-machine` |
| Project toolchain | `devShells` output | `guix shell` manifest |
| Rust dependencies | `importCargoLock` / vendored deps in devShell | `cargo-vendor` from `(holocronix cargo-vendor)`, same idea as `importCargoLock` |
| Cave builder | `mkJediCave { projectShells = [...]; }` | `jedicave-image` from `(holocronix jedicave)` |
| Cave definition file | `flake.nix` | `cave.scm` |
| Extra packages | `extraPackages` | `#:extra-packages` |
| Build command | `nix build .#container` | `guix time-machine -C channels.scm -- build -f cave.scm` |

### Implementation steps

1. **Baked project dependencies** — done for Rust: crates.io and git
   sources (`cargo-vendor`), proven on the dummy projects under
   `examples/`. Next: point it at a real project (libtropic-rs has the
   simplest git deps; xous-core also needs the rust-xous toolchain).
   Other ecosystems (npm, uv) later, as needed.

2. **Guix container builder** — write `jedicave.scm`, a Guile function
   that takes a package list and produces a Docker-loadable image.
   `guix pack` alone cannot set the user, working dir, or arbitrary
   env vars, so this calls `build-docker-image` from `(guix docker)`
   directly. Handle entrypoint, `/etc/passwd`, config baking.

3. **Package agent tooling for Guix** — done for Claude Code:
   `(holocronix agents)` wraps the vendor's prebuilt binary, the same
   release file llm-agents.nix uses, and `(holocronix claude)` pins the
   four skills and plugin repositories by commit and hash and builds the
   plugin seed and settings from `config/defaults.json`. Not yet:
   opencode (a Bun binary too, plus libstdc++ for a native addon),
   kimi-code and qwen-code (built from source with pnpm/npm dependency
   trees) and ori. See `guix/README.md`, sub-problem 6.

4. **CLI backend selection** — done. `jedi init --backend guix <name>`
   scaffolds `cave.scm` and `channels.scm` (default remains `nix`,
   scaffolding `flake.nix`). `cave_backend()` dispatches on which file
   the cave directory holds: `build` runs `guix time-machine -C
   channels.scm -- build -f cave.scm --root=result` then `docker load`,
   `update` re-pins channels from `guix describe`, and `inputs` prints
   the channel table. Everything downstream of `build` was already
   backend-agnostic and is untouched.

5. **Testing** — verify feature parity: firewall, bare repo handoff,
   volumes, `jedi shell`/`up`/`enter` all work identically with both
   backends.

### Known challenges

- **Package coverage** — nixpkgs is larger. The remaining agents must
  be packaged for Guix; the ones built from npm or pnpm dependency
  trees need those trees vendored first. oh-my-zsh is missing; the
  Guix image gets its prompt from starship instead. systemd
  headers do not exist on Guix; projects needing libudev or sd-bus
  get eudev, elogind, or basu.

- **Prebuilt binaries** — agents ship as Bun executables that ask for
  the FHS loader path and do not survive patchelf. The image provides
  that path as a symlink to Guix's glibc instead; a binary that also
  needs libstdc++ or other non-glibc libraries (opencode) will need an
  `/etc/ld.so.cache` or equivalent on top.

- **Project integration** — projects need to expose a Guix manifest
  or channel instead of a `flake.nix` with `devShells`. This is a
  user-facing requirement, not a holocronix limitation.

Resolved:

- Source hashes: skills and plugin repositories are pinned by commit
  plus hash in `(holocronix claude)`, the way Guix origins require;
  `guix download --git` prints the hash for a new commit and leaves
  the checkout in the store for the build.
- Layered images: `build-docker-image` supports `--max-layers`, so
  incremental rebuilds are comparable to `buildLayeredImage`.
- Root-owned image contents and missing `User`/`WorkingDir`: handled by
  the `(holocronix docker)` fork, which archives chosen subtrees under
  the agent's uid and writes both config keys.

## Runtime isolation

The cave image is only as isolated as the runtime that executes it.
Today that is Docker with runc: Linux namespaces, a seccomp profile,
and the host kernel. `SECURITY.md` lists what that does and does not
protect. The image is plain OCI, so the runtime is a pluggable choice
that leaves the Nix and Guix baking layers untouched.

`RELATED-WORK.md` compares the current design with `vmpi`, a QEMU
microVM sandbox built on Gondolin, and with `coop`, Trail of Bits'
Firecracker and Lima sandbox. The short version: both win on the
isolation boundary, Gondolin also on network policy, coop also on its
credential proxy and on Docker inside the guest; we win on reproducible
images, baked toolchains, egress control over coop, and the git handoff.
The designs compose.

### Target design

The most secure design is assembled from parts; none of the reviewed
tools ships it. Layer by layer:

- **Image identity: ours.** A Nix or Guix derivation built in a sandbox
  from pinned inputs. Nothing reviewed matches it.
- **Boundary: a hardware VM.** Firecracker is the strongest VMM: small,
  built for hostile multi-tenant workloads, with a jailer and seccomp.
  QEMU under Gondolin is a larger surface even with its trimmed device
  set. Kata is also a hardware VM, using whichever of those VMMs it is
  configured with, plus a guest agent and a shared-filesystem path for
  the image; it is the one option that remains a Docker runtime. gVisor
  is weaker than all of these: a user-space kernel that still runs on
  the host kernel, not a hardware boundary.
- **Network: no route out of the guest.** Every flow terminates in a
  host-side policy point that speaks HTTP and TLS and nothing else.
  Gondolin has this by construction. It is the biggest gap in our
  design and in coop's.
- **Secrets: never in the guest.** Placeholders substituted per host, an
  operation allowlist so a key can only do what the agent needs, and a
  jailed proxy that fails closed. Gondolin's substitution plus coop's
  jail and allowlist.
- **Workspace: the bare-repo handoff.** Seed from a bare repo, work on
  the guest disk, harvest as a bundle. Every reviewed tool exposes the
  host `.git` in some way; ours never does.
- **Guest hardening stays** under a VM: unprivileged user, seccomp,
  read-only root. It costs nothing, and a wrong boundary then degrades
  gracefully.
- **Host privileges: one-time privileged setup, then a user process.**
  Gondolin runs as a user with `/dev/kvm`. A Firecracker backend needs
  a pre-created TAP, not `sudo` on every run as coop does.

### Order of work

The order matters more than the runtime. The attack that happens in
practice is prompt injection followed by exfiltration through open
egress or an allowed domain, and a VM boundary does nothing against it.
coop is the proof: hardware isolation, open egress.

1. **Close egress inside Docker first.** Done: `network.egress: proxy`
   gives the cave a compose network marked `internal: true`, so it has
   no default route, and attaches only the proxy and DNS sidecars to the
   outside. The cave can reach nothing except mitmproxy, no iptables runs
   inside it, and `NET_ADMIN` and `NET_RAW` are off the container. With
   it came proxy-mode secrets by default, the operation allowlist,
   synthetic DNS by default, and a PID limit. All of it is policy and
   carries over unchanged to any runtime. What remains is listed under
   "Policy hardening" below.
2. **Then the boundary, Gondolin first.** It takes the image as is,
   needs no root, and its network model is already the right one. If
   the FUSE workspace or the HTTP-only stack breaks the cave workflow,
   move to Firecracker, where the step 1 policy moves to the host
   `FORWARD` chain.
3. **Firecracker as the end state** if the strongest VMM or Docker
   inside the guest is wanted. It is the most work: an ext4 rootfs and
   kernel from the closure, host networking, and our own proxy tunnel.

Not to do: adopt a VM before fixing egress; adopt `vmpi` or `coop` as
tools; leave the egress policy inside the guest under any runtime. It
belongs on the other side of the boundary.

### Status

| Item | State |
|------|-------|
| Internal cave network, proxy-only egress, no `NET_ADMIN` | Done. `network.egress: proxy` in `policy.yaml`; each project gets its own /28 for the sidecars. |
| gVisor or Kata via `runtime:` in `compose.yml` | Not started. Drop-in for the compose layer. |
| Gondolin as a microVM backend | Evaluated. Spike planned, see below. |
| Firecracker as a microVM backend | Evaluated through coop. Second candidate, see "Gondolin or Firecracker". |
| Default-deny egress in allowlist mode | Planned. See `SECURITY.md`, "Default-deny egress in allowlist mode". |
| Model API operation allowlist in the proxy | Done. `proxy.operations` in `policy.yaml`. |
| Secrets default to proxy mode | Done. An unset `inject` means proxy when the proxy is on. |
| Synthetic DNS by default | Done, for new caves. |
| cgroup limits in `compose.yml` | Partly. `resources.pids` capped by default; `cpus` and `memory` per cave. |

### Gondolin spike

Goal: boot an unmodified jedicave OCI image under Gondolin and see
whether the cave workflow survives. Questions to answer, in order:

1. **Does it boot?** Gondolin's image builder accepts an OCI image as
   the rootfs source (`oci` in the build config); Alpine still supplies
   the kernel and initramfs, and the rootfs needs `/bin/sh`. Confirm the
   builder injects its guest daemons (`sandboxd`, `sandboxfs`) into a
   non-Alpine rootfs, and that a multi-GB Nix or Guix closure fits the
   ext4 sizing (`rootfs.sizeMb`).
2. **Where does `/workspace` live?** Gondolin mounts host directories
   over FUSE with a 60 KiB per-operation payload cap, which is the wrong
   place for a cargo target directory. Keep `/workspace` on the guest
   disk, expose `repos/` through a read-only provider, and keep the
   bare-repo clone and harvest flow as is. Measure `cargo build` on
   `examples/hello-rust` against the Docker cave.
3. **Does the policy map?** `policy.yaml` domains become Gondolin
   `allowedHosts`; proxy-mode secrets become Gondolin secrets with the
   same placeholder semantics; `hooks` become `onRequest`/`onResponse`.
   The iptables and mitmproxy sidecars disappear.
4. **What is lost?** Long-running caves with `jedi enter`, `docker exec`
   as root for firewall changes, named volumes, HTTP/2. Decide whether a
   Gondolin cave is a second cave type or a `--runtime` flag.

Not on the table: adopting `vmpi` itself. It is a thin, `pi`-only
wrapper; everything of interest is in Gondolin.

### Gondolin or Firecracker

coop shows what a Firecracker backend looks like, and it is not the
same trade as Gondolin. Neither is adopted as code: coop cannot boot an
image it did not build, and Gondolin is a library. The choice is about
which runtime a jedicave image is handed to.

| Question | Gondolin | Firecracker (coop's shape) |
|----------|----------|----------------------------|
| Image input | OCI image as rootfs, Alpine kernel supplied | ext4 rootfs plus kernel we build from the closure |
| Network path | Host userspace stack, default deny, HTTP/1.x and TLS only | TAP on a host bridge; our iptables allowlist moves to the host `FORWARD` chain, so the guest needs no `NET_ADMIN` |
| Protocols | No HTTP/2, QUIC, or general UDP | Anything the host rules pass |
| Workspace | FUSE over virtio-serial, 60 KiB payloads | Block device on the guest disk; copy in, harvest out |
| Docker in the guest | Untested; the network stack may not carry it | Works; coop runs `dockerd` in every guest |
| Host privileges | User process plus `/dev/kvm` | `sudo` for TAP, bridge, iptables |
| macOS | Yes, HVF | No |
| Secrets | Header substitution per host, built in | Ours to build; coop's jailed proxy with an operation allowlist is the model |

The Gondolin spike stays first because it needs no rootfs work and no
`sudo`. If its FUSE workspace is too slow for a cargo build, or Docker
inside the guest turns out to matter, the Firecracker path is next, with
coop's `network.rs` as the reference for the host side.

### Policy hardening borrowed from Gondolin and coop

Cheap changes to the generated firewall and policy that close gaps the
comparisons surfaced, without changing runtimes.

Done, in `policy.yaml`:

- `network.egress: proxy` puts the cave on an `internal: true` network
  with the proxy and DNS sidecars as its only neighbours, and drops
  `NET_ADMIN` and `NET_RAW`;
- `dns.mode` defaults to `synthetic`;
- secrets default to `inject: proxy` when the proxy is on;
- `proxy.operations` allows only the model API operations the agent uses
  and returns 403 for everything else on those hosts;
- `resources.pids` caps the process count; `cpus` and `memory` are per
  cave.

Remaining, for caves that stay in allowlist mode (details under
"Default-deny egress in allowlist mode" in `SECURITY.md`):

- accept only the proxy IP and DNS when the proxy is on, instead of
  every port on every allowlisted IP;
- match allowlist rules on port, not just destination IP;
- block private and link-local ranges plus the cloud metadata address.
