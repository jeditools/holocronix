# Guix backend (experimental)

Guile modules for building jedicave-style images with GNU Guix instead of
Nix. This directory is a Guix load path: use it with `guix -L guix ...` from
the repository root.

Status: exploration, one sub-problem at a time. Nothing here is wired into
the `jedi` CLI yet.

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
- **Not wired into `jedi`.** `jedi build` still runs `nix build`; a cave with
  a `cave.scm` needs the `guix build -f` and `docker load` steps by hand.
