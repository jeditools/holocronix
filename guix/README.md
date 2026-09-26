# Guix backend (experimental)

Guile modules for building jedicave-style images with GNU Guix instead of
Nix. This directory is a Guix load path: use it with `guix -L guix ...` from
the repository root.

Status: experimental but usable. `jedi init --backend guix` scaffolds a Guix
cave and `jedi build` builds it; see "Sub-problem 4" below. The one thing
missing for parity is the agents, which have no Guix packages yet.

## Sub-problem 1: baked Rust dependencies

Goal: an image where `cargo build` on a project succeeds with no network,
because every crate from the project's `Cargo.lock` is already in the image.

Module: `holocronix/cargo-vendor.scm`, exporting `cargo-vendor`.

```scheme
(cargo-vendor "my-project" "/path/to/my-project/Cargo.lock")
```

returns a Guix package whose output holds:

- `vendor/<name>-<version>/` for every dependency in the lockfile, each with
  the stub `.cargo-checksum.json` cargo expects from a directory source;
- `share/cargo-config/config.toml` that redirects `crates-io` and every git
  source to that directory and sets `net.offline = true`.

The approach mirrors nixpkgs' `importCargoLock`.

### crates.io dependencies

The `checksum` field in `Cargo.lock` is the sha256 of the `.crate` tarball,
so each crate becomes a fixed-output download keyed by that checksum. No
extra hashing step, no network at evaluation time. The store items are named
the same way as Guix's own `crate-source`, so they are shared with anything
packaged via `(gnu packages rust-crates)` and are substitutable.

### Git dependencies

For a `git+URL?branch=...#COMMIT` source, the crate is located inside a
checkout of that repository by the `name` in its `[package]` table and copied
to `vendor/<name>-<version>/`. When the crate belongs to a cargo workspace,
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
substitutable. Option 3 is convenient while iterating. Option 1 exists for
fixtures and pre-fetched sources.

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
Guix names), `#:extra-packages`, `#:infra-packages`, `#:env`, `#:symlinks`,
`#:user`/`#:uid`/`#:gid`, `#:git-user`/`#:git-email`, `#:claude?` and
`#:claude-settings`, `#:project-setup`, `#:extra-directives`, `#:max-layers`.

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
`sleep infinity`. Claude Code seeding is present but off until the agent is
packaged.

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
a `cave.scm`. Only three commands care:

| Command | Nix | Guix |
|---|---|---|
| `jedi init` | writes `flake.nix` | `--backend guix` writes `cave.scm` and `channels.scm` |
| `jedi build` | `nix build .#container` | `guix time-machine -C channels.scm -- build -f cave.scm --root=result` |
| `jedi update` | `nix flake update` | re-pins `channels.scm` from `guix describe` |
| `jedi inputs` | flake input table | channel table |

Both end with `docker load` of the cave's own image, so `seed`, `up`,
`enter`, `shell`, `exec`, `firewall`, `diff`, `harvest`, `fetch`, and
`destroy` are untouched and behave identically.

```sh
jedi init --backend guix my-cave
$EDITOR ~/.config/jedicaves/my-cave/cave.scm   # add packages, cargo-vendor
jedi build my-cave
jedi seed ~/code/my-project my-cave
jedi up my-cave && jedi enter my-cave
```

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
- A Guix cave has no agents in it yet. Until they are packaged, use it as a
  reproducible build sandbox and run agents in a Nix cave.

### Known limits

- **Same crate from two git sources.** xous-core's lockfile lists `com_rs`
  twice, once via `?branch=main` and once via `?rev=...`, same commit. Both
  map to the same `vendor/<name>-<version>/`; the second copy overwrites the
  first. Harmless when the commit is the same, untested otherwise.
- **Crates needing files outside their directory** (a `build.rs` reading
  `../something`) break, as they do with `cargo vendor` and nixpkgs.
- **Alternate registries** (`sparse+` or `registry+` other than crates.io)
  are rejected.
- **Lockfile v1** (checksums under `[metadata]`) is not parsed; v2 through v4
  are.
- **No oh-my-zsh.** Guix has no package for it; `.zshrc` skips it when
  absent. The Claude plugin seed directory and settings are not baked yet
  either, pending agent packaging.
- **No agents.** The Guix image ships the base tools and the project
  toolchain, but claude-code and the others are not packaged for Guix yet,
  so a Guix cave cannot run an agent.

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
