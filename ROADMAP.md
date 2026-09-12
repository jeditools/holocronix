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
| Baked Rust dependencies (git sources) | Next. Needed by xous-core, dc34-api, libtropic-rs. |
| Image config: user, workdir, env, file ownership | Planned. Needs a direct `build-docker-image` call. |
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
│       └── jedicave.scm      ← image builder (planned)
└── examples/hello-rust/      ← end-to-end Guix example
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

1. **Baked project dependencies** — done for Rust crates.io deps
   (`cargo-vendor`). Git dependencies next: copy the crate out of its
   checkout and resolve `workspace = true` manifest fields, as nixpkgs
   does with `replace-workspace-values.py`. Other ecosystems (npm, uv)
   later, as needed.

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

- **Root-owned image contents** — Guix's docker builder tars every
  layer with owner 0:0, so there is no equivalent of the
  `chown -R 1000:1000` in `fakeRootCommands`. Plan: start the
  entrypoint as root, chown the writable dirs, drop to uid 1000 with
  `setpriv`.

- **Image config** — `(guix docker)` only emits `Env` and `Entrypoint`.
  `User` and `WorkingDir` can come from compose; arbitrary env needs
  the direct `build-docker-image` call from step 2.

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

Resolved: layered images. `guix pack -f docker` and
`build-docker-image` support `--max-layers`, so incremental rebuilds
are comparable to `buildLayeredImage`.
