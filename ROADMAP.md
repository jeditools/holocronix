# Roadmap

## Guix backend

Holocronix currently uses Nix to bake OCI container images. The plan is
to add Guix as a second "baking" backend so that caves can be built with
either Nix or Guix. The CLI, compose layer, firewall, and git handoff
are already backend-agnostic.

### Architecture

```
holocronix/
├── cli/jedi.py              ← shared (backend-agnostic)
├── config/                   ← shared (zshrc, tmux, firewall, etc.)
├── lib/
│   ├── nix/mkJediCave.nix   ← Nix backend
│   └── guix/mkJediCave.scm  ← Guix backend
├── flake.nix                 ← Nix entry point
├── channels.scm              ← Guix entry point
└── ...
```

### Mapping

| Concept | Nix | Guix |
|---------|-----|------|
| Image builder | `dockerTools.buildLayeredImage` | `guix pack -f docker-image` |
| Input pinning | `flake.lock` | `channels.scm` with pinned commits |
| Project toolchain | `devShells` output | `guix shell` manifest |
| Cave builder | `mkJediCave { projectShells = [...]; }` | Guile function composing packages into a container spec |
| Extra packages | `extraPackages` | Additional packages in manifest |
| Build command | `nix build` | `guix pack` |

### Implementation steps

1. **Refactor lib directory** — move `mkJediCave.nix` into `lib/nix/`,
   create `lib/guix/` for the Guix backend.

2. **Guix container builder** — write `mkJediCave.scm`, a Guile
   function that takes a package list and produces a Docker-loadable
   image via `guix pack -f docker-image`. Handle entrypoint, user
   creation, config baking, and environment variables.

3. **Package agent tooling for Guix** — Claude Code and skills repos
   don't have Guix packages yet. Create channel definitions or
   package recipes.

4. **CLI backend selection** — add `jedi init --backend guix <name>`
   (default remains `nix`). Scaffold the appropriate cave files:
   `flake.nix` for Nix, `manifest.scm` / `channels.scm` for Guix.
   Build command dispatches to `nix build` or `guix pack` based on
   which files are present in the cave.

5. **Entrypoint and config baking** — replicate the Nix
   `fakeRootCommands` setup (creating `/etc/passwd`, seeding config
   files) using Guix profile hooks or a wrapper derivation.

6. **Testing** — verify feature parity: firewall, bare repo handoff,
   volumes, `jedi shell`/`up`/`enter` all work identically with both
   backends.

### Known challenges

- **Layered images** — `guix pack -f docker-image` produces a
  single-layer image. Incremental rebuilds transfer more data than
  Nix's `buildLayeredImage`. Guix may add layered support in the
  future, or a post-processing step could split layers.

- **Filesystem manipulation at build time** — Nix's `fakeRootCommands`
  gives fine-grained control over the image filesystem. Guix needs
  a custom profile hook or build derivation for equivalent setup.

- **Package coverage** — nixpkgs is larger. Agent tooling (Claude Code,
  llm-agents.nix equivalents) must be packaged for Guix.

- **Project integration** — projects need to expose a Guix channel or
  manifest instead of a `flake.nix` with `devShells`. This is a
  user-facing requirement, not a holocronix limitation.
