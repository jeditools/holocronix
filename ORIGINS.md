# Origins

Holocronix grew out of Trail of Bits'
[claude-code-devcontainer](https://github.com/trailofbits/claude-code-devcontainer),
a project that packages Claude Code inside a Docker container so it can
run with `bypassPermissions` without risking the host system.

The starting point for this work was commit
[`d05cd49`](https://github.com/trailofbits/claude-code-devcontainer/tree/d05cd49ea293a416b4be41d4a01f8fe6756a7b76).

## The upstream project

At that commit, `claude-code-devcontainer` was a conventional
devcontainer built on:

- A **Dockerfile** based on Microsoft's `devcontainers/base:ubuntu-24.04`
  image, with pinned digest. It installed system packages via `apt-get`,
  downloaded tools (delta, fzf, fnm/Node, Oh My Zsh) at build time, and
  ran `curl https://claude.ai/install.sh | bash` to install Claude Code.
- A **`devcontainer.json`** that configured VS Code / Cursor integration,
  volumes, and a `post_install.py` lifecycle hook.
- A **`post_install.py`** script that auto-configured
  `bypassPermissions`, set up git-delta, and installed marketplace
  skills on first boot.
- An **`install.sh`** script (aliased as `devc`) providing a CLI for
  terminal workflows: `devc .`, `devc up`, `devc shell`, `devc rebuild`,
  `devc exec`, `devc mount`, `devc cp`, etc.

The workflow was:

1. Clone the repo to `~/.claude-devcontainer` and run `devc self-install`.
2. Run `devc .` from your project directory — this copies the template
   into `.devcontainer/`, builds the image, and starts a container.
3. Use `devc shell` to drop in and run Claude.

There was no Nix involvement, no way to inject project-specific
toolchains, and no firewall CLI — the README documented manual
`iptables` commands users could run inside the container with sudo.

The container user (`vscode`) had passwordless sudo. Claude Code was
installed via its install script at image build time, pulling from the
network. Skills were installed via the Claude plugin marketplace system.

## What changed

Holocronix is a ground-up rewrite that keeps the same security
goal — sandboxed AI agent execution — but replaces virtually all of the
implementation.

### Architecture

| Aspect | claude-code-devcontainer | Holocronix |
|--------|--------------------------|-------------------|
| Image build | Dockerfile (Ubuntu 24.04 base) | `dockerTools.buildLayeredImage` (pure Nix, no base distro) |
| Runtime config | `devcontainer.json` + `docker-compose.yml` in project dir | Named caves under `~/.config/jedicaves/` with `compose.yml` per cave |
| CLI | Bash script (`install.sh` / `devc`) | Python CLI (`jedi`), installable via `nix profile install` |
| Claude Code | `curl \| bash` at image build time | Baked in via `llm-agents.nix` flake input — no runtime downloads |
| Skills | `claude plugin marketplace add` at build time | Flake inputs baked into the image; no network needed |
| Project deps | None — fixed toolset only | `mkJediCave` merges project devShell deps into the image |
| Container user | `vscode` with passwordless sudo | `user` (UID 1000), no sudo, no setuid |
| IDE integration | VS Code / Cursor via devcontainer.json | Terminal-only; no devcontainer.json |
| Node.js | fnm (downloaded at build time) | Nix package (`nodejs_22`) |
| Python | uv-installed Python 3.13 | Nix package (`python315`) + uv for project use |
| First-boot setup | `post_install.py` (Python lifecycle hook) | Shell entrypoint script |

### What was kept

- **Security model.** The core idea is identical: the container is the
  trust boundary. Claude runs with `bypassPermissions` inside; the host
  stays safe.
- **CLI-driven workflow.** `devc` became `jedi`, but the pattern of
  CLI commands for lifecycle management (`up`, `down`, `shell`, `exec`,
  `rebuild`) carries over.
- **Base toolset.** ripgrep, fd, fzf, delta, tmux, jq, vim, bubblewrap,
  socat, iptables/ipset, and Oh My Zsh all carry over from the upstream
  Dockerfile.
- **Persistent volumes.** Command history and Claude config survive
  container rebuilds, same as upstream.
- **NPM hardening.** `IGNORE_SCRIPTS`, `AUDIT`, and
  `MINIMUM_RELEASE_AGE` environment variables are preserved.
- **Skills.** Anthropic and Trail of Bits skill repos are still
  included, just sourced as Nix flake inputs instead of marketplace
  downloads.

### What was dropped

- **Dockerfile and devcontainer.json.** Replaced entirely by Nix's
  `dockerTools.buildLayeredImage`. There is no Dockerfile or
  devcontainer.json.
- **`post_install.py`.** The Python lifecycle hook is replaced by a
  simple shell entrypoint.
- **`install.sh` / `devc` bash script.** Replaced by `jedi`, a Python
  CLI distributed as a Nix package.
- **VS Code / Cursor integration.** The project is terminal-first.
  There is no devcontainer.json for IDE reopening.
- **Ubuntu base image.** The container has no base distro — all packages
  come from Nix.
- **Passwordless sudo.** The container user has no elevated privileges.
  Firewall rules are applied via `docker compose exec --user root`.
- **Network-dependent build steps.** Upstream downloaded Claude Code,
  delta, fzf, fnm, and Oh My Zsh over the network during `docker build`.
  Everything is now resolved by Nix at build time from pinned inputs.
- **GitHub CLI auth volume.** Upstream mounted `~/.config/gh` for `gh`
  persistence. Jedicave blocks `gh` commands entirely via a Claude hook.
- **Host `~/.gitconfig` mount.** Upstream mounted it read-only for git
  identity. Jedicave does not mount it, reducing information disclosure.
- **`devc self-install` / `devc update`.** The CLI is installed and
  updated through Nix (`nix profile install` / `nix profile upgrade`).

### What was added

- **Nix flake library.** `mkJediCave` is exported as
  `lib.<system>.mkJediCave` so other flakes can build custom
  containers without the CLI.
- **Project devShell injection.** Pass your project's `devShells` to
  `mkJediCave` and their dependencies are merged into the image.
  Upstream had no mechanism for project-specific toolchains.
- **Named caves.** `jedi init my-audit` creates a self-contained
  directory with its own `flake.nix`, `compose.yml`, and firewall
  config. Multiple caves can coexist independently.
- **Firewall CLI.** `jedi firewall on/off/status` applies
  domain-allowlist iptables rules. Upstream only documented manual
  iptables commands in the README.
- **Hook blocking git push/commit.** A Claude `PreToolUse` hook blocks
  `git push`, `git commit`, and `gh` commands inside the container.
  Upstream had no such restrictions.
- **`jedi build --update`** integrates `nix flake update` into the build
  step, updating one or all inputs before rebuilding.
- **`jedi list`** shows all caves and their running status.
- **`jedi destroy`** tears down a cave cleanly.
- **`jedi shell` vs `jedi enter`.** Ephemeral containers (removed on
  exit) are distinct from attaching to a long-running container.
- **Security documentation.** `SECURITY.md` provides a detailed threat
  model, trust boundary analysis, and future hardening roadmap. Upstream
  had a brief security section in its README.

## Acknowledgments

Trail of Bits created the original project and the security-first
approach it embodies. This rewrite would not exist without their work.
