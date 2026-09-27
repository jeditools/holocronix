# Guix backend (experimental)

Guile modules for building jedicave-style images with GNU Guix instead of
Nix. This directory is a Guix load path: use it with `guix -L guix ...` from
the repository root.

Status: experimental but usable. `jedi init --backend guix` scaffolds a Guix
cave and `jedi build` builds it; see "Sub-problem 4" below. A Guix cave
ships Claude Code with its settings and plugin seed by default (sub-problem
6); the other agents `flake.nix` ships are not packaged for Guix yet.

## Sub-problem 1: baked Rust dependencies

Goal: an image where `cargo build` on a project succeeds with no network,
because every crate from the project's `Cargo.lock` is already in the image.

Module: `holocronix/cargo-vendor.scm`, exporting `cargo-vendor`.

```scheme
(cargo-vendor "my-project" "/path/to/my-project/Cargo.lock")
```

returns a Guix package whose output holds:

- `vendor/<name>-<version>/` for every crates.io dependency in the lockfile,
  each with the stub `.cargo-checksum.json` cargo expects from a directory
  source;
- `git-sources/<repo>-<commit>/<name>-<version>/` for every git dependency,
  one directory per checkout;
- `share/cargo-config/config.toml` that redirects `crates-io` to `vendor/`
  and every git source to its checkout's directory, and sets
  `net.offline = true`.

The approach mirrors nixpkgs' `importCargoLock`. The lockfile argument may
also be a list, for a project with more than one workspace (xous-core has
`Cargo.lock` and `locales/Cargo.lock`): one config.toml then covers them
all, which matters because only one can be in effect.

Why a directory per checkout rather than one shared `vendor/`: a cargo
directory source holds each crate name and version once, yet a lockfile may
take the same name and version from several sources at once. xous-core has
eleven such pairs, `curve25519-dalek-derive 0.1.1` from both crates.io and
a fork among them. Cargo's own vendoring splits them the same way.

### crates.io dependencies

The `checksum` field in `Cargo.lock` is the sha256 of the `.crate` tarball,
so each crate becomes a fixed-output download keyed by that checksum. No
extra hashing step, no network at evaluation time. The store items are named
the same way as Guix's own `crate-source`, so they are shared with anything
packaged via `(gnu packages rust-crates)` and are substitutable.

### Git dependencies

For a `git+URL?branch=...#COMMIT` source, the crate is located inside a
checkout of that repository by the `name` in its `[package]` table and copied
to `git-sources/<repo>-<commit>/<name>-<version>/`. When the crate belongs to
a cargo workspace,
its manifest is rewritten so `workspace = true` fields carry the concrete
values from the workspace root (`aux-files/replace-workspace-values.py`, the
nixpkgs script, run with Python and tomli-w at build time only). Without
that rewrite the crate cannot be parsed standalone. Path dependencies between
crates of one workspace need no rewriting: cargo resolves a `path` dependency
of a non-path source by name and version within that same source.

Where the checkout comes from, in order of precedence:

```scheme
(cargo-vendor "my-project" "/path/to/Cargo.lock"
  ;; 1. explicit file-like objects, keyed by commit or URL
  #:git-checkouts `(("https://github.com/foo/bar" . ,(local-file "..." #:recursive? #t)))
  ;; 2. fixed-output git-fetch origins, keyed by commit
  #:git-hashes '(("d589aff0246a6e132e679a16d49c0d09803d6cd7" . "0abc...base32...")))
  ;; 3. otherwise an unhashed git-checkout, cloned at build time
```

Option 2 is what a committed cave definition should use: reproducible and
substitutable. `jedi lock` produces it. For each git source in the cave's
lockfiles it runs `guix download --git --commit=COMMIT URL`, which fetches
the checkout into the store and prints its hash, and writes the alist to
`<cave>/vendor.lock.scm`. `read-vendor-lock` loads that file:

```scheme
(cargo-vendor "my-project" "/path/to/Cargo.lock"
  #:git-hashes (read-vendor-lock (dirname (current-filename))))
```

`(current-filename)` is the cave.scm being evaluated, so the lock file is
found next to it whatever the working directory. A missing file reads as
the empty list, so a project without git dependencies never needs one: the
crates.io checksums in `Cargo.lock` are all it takes. Option 3 is
convenient while iterating. Option 1 exists for fixtures and pre-fetched
sources.

### Try it

Two dummy projects live under `examples/`:

- `hello-rust/`: two crates.io dependencies, nothing else.
- `hello-rust-git/`: the same plus `greeter`, a git dependency on the
  workspace in `examples/fixtures/greeter/`. That fixture uses
  `[workspace.package]`, `[workspace.dependencies]`, and a path dependency
  between members, the cases that make git vendoring hard.

The fixture is plain files in the repo. `hello-rust-git/setup-fixture.sh`
turns it into a git repository at `/tmp/holocronix-fixtures/greeter` with
pinned identity and dates, so the commit hash is the same everywhere and
matches `Cargo.lock`. Only `cargo generate-lockfile` needs that repository;
the Guix build maps the URL to the fixture directory via `#:git-checkouts`
and never fetches.

Build a vendor package alone:

```sh
guix build -L guix -e '(begin (use-modules (holocronix cargo-vendor))
  (cargo-vendor "hello-rust" "'$PWD'/examples/hello-rust/Cargo.lock"))'
```

Offline build in a Guix container (no `-N`, so no network). Same for
`hello-rust-git` with the paths swapped. `--share` and `--expose` bind-mount
existing directories, so create the target directory first:

```sh
mkdir -p /tmp/hello-target
guix shell -C --pure -L guix -m examples/hello-rust/manifest.scm \
  --expose=$PWD/examples/hello-rust=/workspace/hello-rust \
  --share=/tmp/hello-target=/tmp/target \
  -- sh -c 'cd /workspace/hello-rust && HOME=/tmp CARGO_TARGET_DIR=/tmp/target \
       cargo build --locked --config $GUIX_ENVIRONMENT/share/cargo-config/config.toml'
```

Docker image:

```sh
guix pack -L guix -f docker -m examples/hello-rust-git/manifest.scm \
  -S /bin=bin -S /.cargo=share/cargo-config \
  --entry-point=bin/bash --image-tag=hello-rust-git-cave
docker load < result   # or the /gnu/store path guix pack prints

docker run --rm --network none \
  -v $PWD/examples/hello-rust-git:/workspace/hello-rust-git:ro \
  -e HOME=/tmp -e CARGO_TARGET_DIR=/tmp/target -w /workspace/hello-rust-git \
  hello-rust-git-cave:latest -c 'cargo build --locked && /tmp/target/debug/hello-rust-git'
```

The image's entrypoint is bash, so `docker run` arguments are bash
arguments: pass `-c '...'`, not `bash -c '...'`.

Why `/.cargo`: cargo reads `<dir>/.cargo/config.toml` for every ancestor of
the working directory up to `/`, so a symlink at the filesystem root applies
the vendored config to any project on the image without touching
`CARGO_HOME`, which cargo needs writable for its lock file.

Regenerating `hello-rust-git/Cargo.lock` after changing the fixture:

```sh
examples/hello-rust-git/setup-fixture.sh          # prints the commit
cd examples/hello-rust-git && guix shell rust rust:cargo nss-certs -- \
  sh -c 'SSL_CERT_FILE=$GUIX_ENVIRONMENT/etc/ssl/certs/ca-certificates.crt cargo generate-lockfile'
```

If the fixture already exists at that path from an older version, move it
away first; the script does not overwrite.

## Sub-problem 2: Xous cross toolchain in the image

Goal: an image that cross-compiles for `riscv32imac-unknown-xous-elf`
offline, so a xous-core cave can build without the host's toolchain.

Nothing new is packaged here. baobit already provides
`rust-xous-toolchain`: a pinned Rust with a merged sysroot carrying the host
target, `riscv32imac-unknown-xous-elf`, and `riscv32imac-unknown-none-elf`,
plus wrappers for rustc, cargo, cc, and rust-lld. The image just includes
that package, built under the Guix commit baobit pins so the derivation
matches what baobit's CI and substitute server (guix.baobit.one) produce.

baobit is consumed as a load path, the way its own Makefile does, not as a
channel: baobit's channel authentication is currently broken on main (see
`note-channel-auth-broken.md` in baobit), so a `(channel (name 'baobit) ...)`
pin would fail to authenticate. Switch to the channel form once that is
fixed.

`examples/hello-xous/` is a std hello world for the Xous target with one
crates.io dependency, so vendoring is exercised on a cross build too.

With `BAOBIT` pointing at a baobit checkout:

```sh
mkdir -p /tmp/hello-xous-target
guix time-machine -C $BAOBIT/channels/guix.scm -- \
  shell -C --pure -L guix -L $BAOBIT/packages -m examples/hello-xous/manifest.scm \
  --expose=$PWD/examples/hello-xous=/workspace/hello-xous \
  --share=/tmp/hello-xous-target=/tmp/target \
  -- sh -c 'cd /workspace/hello-xous && HOME=/tmp CARGO_TARGET_DIR=/tmp/target \
       cargo build --locked --target riscv32imac-unknown-xous-elf \
         --config $GUIX_ENVIRONMENT/share/cargo-config/config.toml'
```

The result cannot run on the host. Check it is a RISC-V ELF instead:

```sh
od -A x -t x1z -N 20 /tmp/hello-xous-target/riscv32imac-unknown-xous-elf/debug/hello-xous
# 7f 45 4c 46 01 ... at 0, and f3 00 (EM_RISCV) at offset 0x12
```

Docker image, same flags as the other examples but under `time-machine`:

```sh
guix time-machine -C $BAOBIT/channels/guix.scm -- \
  pack -L guix -L $BAOBIT/packages -f docker -m examples/hello-xous/manifest.scm \
  -S /bin=bin -S /.cargo=share/cargo-config \
  --entry-point=bin/bash --image-tag=hello-xous-cave
docker load < result
docker run --rm --network none \
  -v $PWD/examples/hello-xous:/workspace/hello-xous:ro \
  -e HOME=/tmp -e CARGO_TARGET_DIR=/tmp/target -w /workspace/hello-xous \
  hello-xous-cave:latest -c 'cargo build --locked --target riscv32imac-unknown-xous-elf'
```

If the toolchain is not in your store or on guix.baobit.one, the first build
compiles the Xous sysroot from the betrusted-io Rust fork, which takes a
long time. `guix time-machine -C $BAOBIT/channels/guix.scm -- build -L
$BAOBIT/packages --dry-run -e '(@ (rust-xous-toolchain) rust-xous-toolchain)'`
tells you beforehand.

## Sub-problem 3: the image builder

Goal: a real jedicave image, not a bare `guix pack` profile: user `yoda`
(uid 1000), `/workspace` as working directory, a writable home with the
shell and git config, the volume mount points owned by the agent, root-only
infrastructure tools, the entrypoint, and the environment variables the Nix
image sets.

Module: `holocronix/jedicave.scm`, exporting `jedicave-image`, the Guix
counterpart of `lib/mkJediCave.nix`. It evaluates to the Docker image
tarball, so a cave definition is a file returning it:

```scheme
(use-modules (holocronix jedicave) (holocronix cargo-vendor) (gnu packages))
(jedicave-image
 #:name "hello-rust-jedicave"
 #:extra-packages (append (specifications->packages '("rust" "rust:cargo"))
                          (list (cargo-vendor "hello-rust" "/path/to/Cargo.lock")))
 #:symlinks '(("/.cargo" . "share/cargo-config")))
```

`examples/hello-rust/cave.scm` is exactly that. Options mirror `mkJediCave`:
`#:packages` (defaults to `%jedicave-base-specs`, the Nix tool list under
Guix names), `#:extra-packages`, `#:agents`, `#:marketplaces`, `#:plugins`,
`#:infra-packages`, `#:env`, `#:symlinks`, `#:user`/`#:uid`/`#:gid`,
`#:git-user`/`#:git-email`, `#:claude?` and `#:claude-settings`,
`#:project-setup`, `#:extra-directives`, `#:max-layers`.

Why `guix pack` is not enough: its docker format only writes `Env` and
`Entrypoint` into the image config, and it archives every file as root, so
there is no way to give the agent a writable home. `holocronix/docker.scm`
is a fork of Guix's `guix/docker.scm` (GPLv3, same as this repo) with three
additions: `#:user` and `#:working-dir` in the config, `#:owners` to archive
chosen subtrees of the non-store layer under another uid/gid, and modes kept
as the populate directives set them. The store layers are untouched and
still split with `--max-layers`, so images share layers like
`buildLayeredImage` output does.

The entrypoint is a port of the Nix `jedicave-start` script: first-boot
setup, proxy CA injection, cloning bare repos from `/repos` into
`/workspace` with every seeded branch materialized, project setup, then
`sleep infinity`. When the image has Claude Code, first boot also copies
`settings.json` into `CLAUDE_CONFIG_DIR` (sub-problem 6).

### Try it

```sh
guix build -L guix -f examples/hello-rust/cave.scm        # prints the tarball path
docker load < /gnu/store/...-hello-rust-jedicave-docker-image.tar.gz

docker run -d --name hello --network none \
  -v $PWD/examples/hello-rust:/src/hello-rust:ro hello-rust-jedicave:latest
docker logs hello                       # [jedicave] First-boot setup... Setup complete.
docker exec hello sh -c 'id; pwd; ls -ld /home/yoda /workspace /usr/local/sbin'
docker exec hello sh -c 'cp -r /src/hello-rust /workspace/ && cd /workspace/hello-rust \
  && cargo build --locked && ./target/debug/hello-rust'
docker rm -f hello
```

Expected: `uid=1000(yoda)`, `/workspace`, home and workspace owned by yoda,
`/usr/local/sbin` unreadable to yoda, and the build succeeds with no network.
Compressed tarball is about 900 MB; the loaded image about 6 GB, since the
base set includes gcc-toolchain, python, node, and rust.

## Sub-problem 4: the `jedi` CLI

Goal: a Guix cave managed by the same commands as a Nix cave.

A cave is Nix-backed when it holds a `flake.nix`, Guix-backed when it holds
a `cave.scm`. Only these commands care:

| Command | Nix | Guix |
|---|---|---|
| `jedi init` | writes `flake.nix` | `--backend guix` writes `cave.scm` and `channels.scm` |
| `jedi lock` | n/a | pins the git sources of the seeded `Cargo.lock`s in `vendor.lock.scm` |
| `jedi build` | `nix build .#container` | `guix time-machine -C channels.scm -- build -f cave.scm --root=result` |
| `jedi update` | `nix flake update` | re-pins `channels.scm` from `guix describe` |
| `jedi inputs` | flake input table | channel table |

Both end with `docker load` of the cave's own image, so `seed`, `up`,
`enter`, `shell`, `exec`, `firewall`, `diff`, `harvest`, `fetch`, and
`destroy` are untouched and behave identically.

```sh
jedi init --backend guix my-cave
$EDITOR ~/.config/jedicaves/my-cave/cave.scm   # add packages, cargo-vendor
jedi seed ~/code/my-project my-cave
jedi lock my-cave       # only if Cargo.lock has git sources
jedi build my-cave
jedi up my-cave && jedi enter my-cave
```

### Locking git dependencies

`jedi lock` is the one step of the workflow that touches the network, and
it is only needed when a `Cargo.lock` has git sources. It scans every
tracked `Cargo.lock` in the cave's seeded repos, read from their host
working trees, which is what `cave.scm` points at too, or the files given
with `--lockfile`. For each repository and commit not already recorded it
lists what it is about to fetch, asks (`--yes` skips that), runs
`guix download --git --commit=COMMIT URL`, and appends the hash to
`<cave>/vendor.lock.scm` as it goes, so an interrupted run resumes where it
stopped. Re-run it after a `Cargo.lock` change; already-pinned commits are
not fetched again, and entries for commits no longer in the scanned
lockfiles are kept, since another lockfile may still want them.

The lock file is the git-dependency half of `flake.lock`: `cave.scm` says
which lockfiles to vendor, `vendor.lock.scm` says what their git sources
resolved to, and both are committed. The checkout is what `git-fetch` will
verify against that hash at build time, so a wrong or altered checkout
fails the build rather than being used.

The fetch happens once. A fixed-output store path is a function of the
content hash and the name, and the `git-fetch` origin `cargo-vendor` builds
is named exactly as `guix download --git` names its result,
`<repo>-<commit7>`, so the checkout `jedi lock` put in the store is already
that origin's output and `jedi build` downloads nothing. On another
machine, or after `guix gc`, the origin fetches and verifies it itself.

### Pinning

`channels.scm` is the Guix counterpart of `flake.lock`: `jedi init` writes
the holocronix channel plus everything `guix describe -f channels` reports,
each at a commit, and `jedi build` runs under `guix time-machine` with it.
`jedi update` re-pins. Delete the file to build with whatever `guix` is on
`PATH`.

When `HOLOCRONIX_URL` points at a local checkout, as the devShell sets it,
`init` records that path and pins it to the checkout's `HEAD`, and `build`
also passes `-L <checkout>/guix` so the working tree wins over the pinned
commit. That is what makes editing the modules and rebuilding immediate.
For a cave you intend to keep, point the channel at a real remote so the
pin means something to someone else.

Adding another channel, a project's own or baobit's, means editing the
first list in `channels.scm` by hand; `jedi` only rewrites the file on
`update`.

### A cave that needs another channel

A cave whose toolchain lives outside holocronix and Guix proper, for
instance baobit's `rust-xous-toolchain`, adds that channel to
`channels.scm` and uses its modules from `cave.scm`:

```scheme
;; channels.scm
(list (channel (name 'holocronix) (url "...") (branch "main") (commit "..."))
      (channel (name 'baobit) (url "/home/you/code/baochip/baobit")
               (branch "crossbar-boot-305693ed")
               (commit "e492b6444cf7ea17e4f0c4c78be40792b81e9333"))
      (channel (name 'guix) (url "https://git.guix.gnu.org/guix.git")
               (branch "master")
               ;; baobit's pin, NOT `guix describe`'s: see below.
               (commit "36d403cfd77ff5452978cf94425902675e6ad81b")
               (introduction ...)))
```

```scheme
;; cave.scm
(use-modules (holocronix jedicave) (rust-xous-toolchain) (gnu packages))
(jedicave-image
 #:name "jedicave-xous-toolchain"
 ;; rust-xous-toolchain, not plain "rust": it ships its own rustc and cargo
 ;; wrappers, so adding both would collide in the profile.
 #:extra-packages (list rust-xous-toolchain))
```

Two things decide whether this is a four-minute build or an overnight one.

**Match the other channel's Guix pin.** A package is only in your store
under the exact Guix commit it was built with. baobit pins `36d403cf`, so a
cave pinning anything else re-derives `rust-xous-toolchain` and rebuilds the
Xous sysroot from the betrusted-io Rust fork. Take the pin from the other
project's own channels file rather than from `guix describe`. Check before
committing to a build:

```sh
guix time-machine -C channels.scm -- build -L <holocronix>/guix -f cave.scm --dry-run
```

Nothing named `rust-sysroot` or `rust-xous` in the output means it resolves
from the store.

**`jedi update` leaves both alone.** It re-pins the `holocronix` form in
place and nothing else, so extra channels, a hand-set Guix commit, your
comments and your layout all survive; it prints which channels it left
untouched. Add `--guix` when you do want the Guix pin moved to whatever
`guix describe` reports, which on a cave like this means rebuilding the
other channel's packages.

A channel with no `(introduction ...)` draws a warning that it cannot be
authenticated. That is expected for a local checkout, and for baobit, whose
own signing chain is broken on main.

### Notes

- Each cave has its own image, `jedicave-<cave>:latest`. The scaffolded
  `cave.scm` passes that as `#:name`, matching what `compose.yml` expects.
  Keep the two in step, or let `jedi build` do it: it retags whatever the
  definition produced under the cave's own name.
- Image compression defaults to `gzip -1n`, not `guix pack`'s `-9n`. The
  archive is loaded into Docker immediately, so an hour of compression to
  save a few percent is wasted; `jedi build` also passes
  `--max-silent-time=0`, since compressing a multi-GB archive is silent for
  long enough to trip the daemon's default one-hour limit.
- A Guix cave ships Claude Code by default; `#:agents '()` in `cave.scm`
  makes a plain build sandbox. Sub-problem 6 has the details.

### Known limits

- **Git submodules.** A checkout is fetched non-recursively, by `jedi lock`
  and by the `git-fetch` origin alike, so a git dependency whose crate needs
  a submodule is incomplete. cargo itself does fetch them.
- **Crates needing files outside their directory** (a `build.rs` reading
  `../something`) break, as they do with `cargo vendor` and nixpkgs.
- **Alternate registries** (`sparse+` or `registry+` other than crates.io)
  are rejected.
- **Lockfile v1** (checksums under `[metadata]`) is not parsed; v2 through v4
  are.
- **No oh-my-zsh.** Guix has no package for it; `.zshrc` skips it when
  absent, and the prompt comes from starship instead, configured by
  `config/starship.toml` with plain Unicode symbols so it renders without a
  Nerd Font.
- **Only Claude Code.** Of the agents `flake.nix` ships, only claude-code
  is packaged for Guix; opencode, kimi-code, qwen-code and ori are not.
  Sub-problem 6 says what each one needs.

## Sub-problem 5: asking the model

Goal: let an agent, or a script, ask the Guix package model a question and
get a JSON answer, instead of reading Scheme and guessing. This is step 2
of `VISION.md`: expose the model through tools before teaching anyone to
read the source.

`cli/query.scm` is a script run under `guix repl`. `jedi guix` wraps it.
It lives beside `jedi.py` and not under `guix/` on purpose: `guix/` is the
holocronix channel, and Guix loads every `.scm` file in a channel before
compiling it, so a script there runs its entry point inside the channel
build and fails it. Only modules belong under the channel directory.

```sh
jedi guix show hello                 # the record: source, inputs, arguments
jedi guix inputs hello --implicit    # what the build system adds (the bag)
jedi guix derivation hello           # .drv path, output paths, built or not
jedi guix plan hello                 # what a build would build or download
jedi guix references ITEM            # run-time references of a built item
jedi guix referrers ITEM             # what refers to a built item
jedi guix size hello coreutils       # closure sizes, store or substitutes
jedi guix graph hello -t bag --depth 1   # nodes and edges of a slice
jedi guix lint hello                 # local checkers; --network for the rest
jedi guix search '^ripgrep'          # name, synopsis, description regexp
jedi guix classify git               # pure-record, custom-arguments, has-phases
```

`SPEC` is what `guix build` takes, `hello` or `hello@2.12`, or a Scheme
expression in parentheses such as `'(@ (gnu packages base) hello)'`.
`ITEM` is a spec or a `/gnu/store` path.

The contract:

- One JSON object on stdout, exit 0. On failure, `{"error": "..."}` on
  stdout and exit 1, so a caller never parses stderr. The daemon's
  substituter still chats on stderr during `plan`.
- Nothing is ever built. `plan` asks the daemon and the substitute servers
  what a build would do and stops there.
- `--cave NAME` runs the query under that cave's `channels.scm` through
  `guix time-machine`, so the answer describes the Guix the cave builds
  with. Without it, the `guix` on `PATH` answers.
- `--system` and `--target` apply to `inputs`, `derivation`, `plan` and
  `graph`, which is how to see what a cross-compiled package needs.

Which ops need the daemon: `derivation`, `plan`, `references`,
`referrers`, `size`, `graph` with any type but `package`, and `lint` when
the `derivation` or `profile-collisions` checkers run. `show`, `inputs`,
`search`, `classify` and `graph -t package` need only the package modules.

`classify` is the has-phases metric from `VISION.md`: a package is
`pure-record` when its `arguments` are empty, `custom-arguments` when they
set flags but no phases, and `has-phases` when it modifies phases, which is
where a definition stops being data.

Directly, without `jedi`:

```sh
guix repl -L guix -- cli/query.scm show hello
```

## Sub-problem 6: agents

Goal: a Guix cave that runs Claude Code with the settings and pre-installed
plugins a Nix cave has. Done for Claude Code; the other agents in
`flake.nix` are covered at the end.

`holocronix/agents.scm` defines the `claude-code` package and
`%jedicave-default-agents`, which `jedicave-image` installs unless
`#:agents` says otherwise. `holocronix/claude.scm` pins the four skills and
plugin repositories `flake.nix` takes as inputs, by commit and hash, and
builds the plugin seed directory and `settings.json` from
`config/defaults.json`, the file the Nix builder reads. A cave that wants
more plugins passes `#:plugins '("name@marketplace")`; one that wants
another marketplace adds a `(marketplace ...)` record to `#:marketplaces`.

### The binary

Anthropic ships Claude Code as a single executable built with Bun, and
that is what nixpkgs and llm-agents.nix package too, so the Guix package
fetches the release file, zstd-compressed from Anthropic's download host
as nixpkgs does, and leaves the executable unmodified. The version is
2.1.283, what nixpkgs shipped on 2026-09-27; `flake.nix` pins llm-agents.nix
at 2.1.231, and the two move independently. The usual prebuilt-binary
treatment does not apply: `patchelf
--set-interpreter` relocates the program headers, and the Bun executable
segfaults on start afterwards. llm-agents.nix uses its own `wrap-buddy`
tool instead of patchelf for the same reason.

The binary asks for `/lib64/ld-linux-x86-64.so.2`, the FHS loader path.
Guix's glibc loader searches glibc's own `lib/` by default, so one symlink
from that path to Guix's `ld-linux-x86-64.so.2` is all the binary needs:
libc, libm, libpthread, libdl and librt resolve with no RPATH and no
`LD_LIBRARY_PATH`. `jedicave-image` adds the symlink whenever the image has
agents, and glibc joins the image closure through it. The process then runs
as upstream built it, with a real `/proc/self/exe`.

The wrapper at `bin/claude` sets the knobs the Nix wrapper sets:
`DISABLE_AUTOUPDATER`, `DISABLE_INSTALLATION_CHECKS`, and
`DISABLE_NON_ESSENTIAL_MODEL_CALLS` as a default rather than forced; plus
`USE_BUILTIN_RIPGREP=0` with Guix's ripgrep first on `PATH`, since the
embedded `rg` has the same loader problem. It leaves out the Nix wrapper's
bubblewrap and socat: in a jedicave those are root-only infrastructure
tools. Where `/lib64` is absent, in `guix shell` or the build container,
the wrapper runs the Guix loader explicitly. The build's `check-version`
phase goes through that path to run `claude --version`, which is what
catches a glibc mismatch or a damaged download at build time.

### Settings and seed

`CLAUDE_CODE_PLUGIN_SEED_DIR` is the documented way to give a
network-locked container its plugins: a read-only directory holding
`known_marketplaces.json`, `marketplaces/<name>/` checkouts, and
`cache/<marketplace>/<plugin>/<version>/` copies, which Claude Code reads
by layout. `<name>` must be the `name` inside the checkout's
`.claude-plugin/marketplace.json`, not the repository name. The seed is
built once as a store item and linked at `/env/.claude-plugin-seed`.
`settings.json` is baked at `/env/.claude/settings.json` and copied there
again by the entrypoint on first boot, because `/env/.claude` is usually a
volume that hides the baked copy. Both carry the same content as the Nix
cave's.

`claude plugin list` and `claude plugin marketplace list` report nothing
in either kind of cave: the seed is read by the plugin loader when a
session starts, not by those subcommands. Checked against a Nix cave image
on the same host.

### Pinning

The marketplace pins are commit plus nar hash, the Guix half of
`flake.lock`. To move one, edit `commit` and `hash` together;
`guix download --git --commit=COMMIT https://github.com/OWNER/REPO` prints
the hash and, because the origin is named the way `guix download` names its
result, leaves the checkout in the store for the build to use. The Claude
Code hash is the release file's; `guix download URL` prints it.

The hashes committed here were computed from the trees and the file Nix
had already fetched and verified for the same commits and version, so the
first Guix build downloaded nothing from GitHub or from Anthropic's bucket.

### Try it

```sh
guix build -L guix -e '(@ (holocronix agents) claude-code)'
$(guix build -L guix -e '(@ (holocronix agents) claude-code)')/bin/claude --version
guix build -L guix -e '(begin (use-modules (holocronix claude)) (claude-plugin-seed))'
```

A cave gets all of it by default:

```sh
jedi update my-cave
jedi build my-cave
jedi up my-cave
jedi enter my-cave
claude --version                     # 2.1.283 (Claude Code)
ls -la /lib64 /env/.claude-plugin-seed
```

A test image with the base tools trimmed to nine packages plus Claude Code
2.1.231 came to a 470 MB tarball and 1.3 GB loaded, 311 MB of it Claude
Code. 2.1.283 is 241 MB unpacked and an 85 MB download.

### The other agents

`flake.nix` also ships kimi-code, opencode, ori and qwen-code. None is
packaged yet:

- **opencode** is a Bun executable too, from GitHub releases, so the loader
  treatment above applies, but it also `dlopen`s a native addon that needs
  `libstdc++.so.6`, which the glibc-only search path does not find. The
  image needs an `/etc/ld.so.cache` covering `gcc:lib`, or an equivalent,
  first.
- **kimi-code** and **qwen-code** are built from source with pnpm and npm
  dependency trees and native addons (node-pty, keytar). Packaging them
  means vendoring those trees the way `cargo-vendor` does for crates.
- **ori** is a native binary built from source in llm-agents.nix; not
  looked at yet.
