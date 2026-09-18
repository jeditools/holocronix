# Roadmap

## Guix backend

Holocronix currently uses Nix to bake OCI container images. The plan is
to add Guix as a second "baking" backend so that caves can be built with
either Nix or Guix. The CLI, compose layer, firewall, and git handoff
are already backend-agnostic.

### Status

The work is split into sub-problems, tackled one at a time, each proven
on a dummy project before touching real ones. See `guix/README.md` for
usage.

| Sub-problem | State |
|-------------|-------|
| Baked Rust dependencies (crates.io) | Done. `cargo build` works offline in a `guix pack -f docker` image. |
| Baked Rust dependencies (git sources) | Done, on a local fixture workspace. Not yet tried on xous-core, dc34-api, or libtropic-rs. |
| Xous cross toolchain in the image | Done. baobit's `rust-xous-toolchain` via load path under baobit's pinned Guix; std hello world cross-compiles offline. Channel form blocked by baobit's broken channel auth. |
| Image config: user, workdir, env, file ownership | Done. `jedicave-image` in `guix/holocronix/jedicave.scm` on a forked docker builder with `#:user`, `#:working-dir`, `#:owners`. Verified on `examples/hello-rust/cave.scm`. |
| Agent tooling packaged for Guix | Planned. |
| CLI backend selection | Planned. |

### Architecture

```
holocronix/
├── cli/jedi.py              ← shared (backend-agnostic)
├── config/                   ← shared (zshrc, tmux, firewall, etc.)
├── lib/mkJediCave.nix        ← Nix backend
├── flake.nix                 ← Nix entry point
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
| Cave builder | `mkJediCave { projectShells = [...]; }` | Guile function composing packages into a container spec |
| Extra packages | `extraPackages` | Additional packages in manifest |
| Build command | `nix build` | `guix pack` |

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

3. **Package agent tooling for Guix** — Claude Code, opencode,
   kimi-code, qwen-code, and the skills repos have no Guix packages.
   Fetch release tarballs and wrap them with node, as llm-agents.nix
   does, in a channel inside this repo.

4. **CLI backend selection** — add `jedi init --backend guix <name>`
   (default remains `nix`). Scaffold the appropriate cave files:
   `flake.nix` for Nix, `cave.scm` / `channels.scm` for Guix. Build
   command dispatches to `nix build` or `guix pack` based on which
   files are present in the cave.

5. **Testing** — verify feature parity: firewall, bare repo handoff,
   volumes, `jedi shell`/`up`/`enter` all work identically with both
   backends.

### Known challenges

- **Package coverage** — nixpkgs is larger. Agent tooling must be
  packaged for Guix. oh-my-zsh is missing but trivial. systemd
  headers do not exist on Guix; projects needing libudev or sd-bus
  get eudev, elogind, or basu.

- **Project integration** — projects need to expose a Guix manifest
  or channel instead of a `flake.nix` with `devShells`. This is a
  user-facing requirement, not a holocronix limitation.

- **Source hashes** — Guix origins require a hash, so skills and
  plugin repos need a lock of commit plus hash, unlike unlocked
  flake inputs.

Resolved:

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
microVM sandbox built on Gondolin. The short version: it wins on the
isolation boundary and on network policy, we win on reproducible
images, baked toolchains, and the git handoff. The two compose.

### Status

| Item | State |
|------|-------|
| gVisor or Kata via `runtime:` in `compose.yml` | Not started. Drop-in for the compose layer. |
| Gondolin as a microVM backend | Evaluated. Spike planned, see below. |
| Default-deny egress in `policy.yaml` | Planned. See `SECURITY.md`, "Default-deny egress". |
| Secrets default to proxy mode | Planned. |
| cgroup limits in `compose.yml` | Planned. See `SECURITY.md`, "Resource limits". |

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

### Policy hardening borrowed from Gondolin

Cheap changes to the generated firewall and policy that close gaps the
comparison surfaced, without changing runtimes. Details under
"Default-deny egress" in `SECURITY.md`:

- accept only the proxy IP and DNS when the proxy is on, instead of
  every port on every allowlisted IP;
- match allowlist rules on port, not just destination IP;
- block private and link-local ranges plus the cloud metadata address;
- default `dns.mode` to `synthetic`;
- default secrets to `inject: proxy`.
