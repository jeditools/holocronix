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

- `vendor/<name>-<version>/` for every crates.io entry in the lockfile, each
  with the stub `.cargo-checksum.json` cargo expects from a directory source;
- `share/cargo-config/config.toml` that redirects `crates-io` to that
  directory and sets `net.offline = true`.

How it works, and why it needs no extra hashes: the `checksum` field in
`Cargo.lock` is the sha256 of the `.crate` tarball, so each crate becomes a
fixed-output download keyed by that checksum. The store items are named the
same way as Guix's own `crate-source`, so they are shared with anything
packaged via `(gnu packages rust-crates)` and are substitutable.

The approach mirrors nixpkgs' `importCargoLock`.

### Try it

`examples/hello-rust/` is a dummy crate with two crates.io dependencies and
a manifest describing the image.

Build the vendor package alone:

```sh
guix build -L guix -e '(begin (use-modules (holocronix cargo-vendor))
  (cargo-vendor "hello-rust" "'$PWD'/examples/hello-rust/Cargo.lock"))'
```

Offline build in a Guix container (no `-N`, so no network):

```sh
guix shell -C --pure -L guix -m examples/hello-rust/manifest.scm \
  --expose=$PWD/examples/hello-rust=/workspace/hello-rust \
  --share=/tmp/hello-target=/tmp/target \
  -- sh -c 'cd /workspace/hello-rust && HOME=/tmp CARGO_TARGET_DIR=/tmp/target \
       cargo build --locked --config $GUIX_ENVIRONMENT/share/cargo-config/config.toml'
```

Docker image:

```sh
guix pack -L guix -f docker -m examples/hello-rust/manifest.scm \
  -S /bin=bin -S /.cargo=share/cargo-config \
  --entry-point=bin/bash --image-tag=hello-rust-cave
docker load < result   # or the /gnu/store path guix pack prints

docker run --rm --network none \
  -v $PWD/examples/hello-rust:/workspace/hello-rust:ro \
  -e HOME=/tmp -e CARGO_TARGET_DIR=/tmp/target -w /workspace/hello-rust \
  hello-rust-cave:latest -c 'cargo build --locked && /tmp/target/debug/hello-rust'
```

The image's entrypoint is bash, so `docker run` arguments are bash
arguments: pass `-c '...'`, not `bash -c '...'`.

Why `/.cargo`: cargo reads `<dir>/.cargo/config.toml` for every ancestor of
the working directory up to `/`, so a symlink at the filesystem root applies
the vendored config to any project on the image without touching
`CARGO_HOME`, which cargo needs writable for its lock file.

### Not done yet

- **Git dependencies.** `cargo-vendor` errors out on any non-crates.io
  source. Supporting them means copying the crate out of its git checkout
  and resolving `workspace = true` manifest fields, as nixpkgs does with
  `replace-workspace-values.py`. All three real Rust projects looked at
  (xous-core, dc34-api, libtropic-rs) need this.
- **Alternate registries** (`sparse+` or `registry+` other than crates.io).
- **Lockfile v1** (checksums under `[metadata]`); v2 through v4 are handled.
- Image config beyond what `guix pack` offers: user, working dir, arbitrary
  env vars. Those need a custom call to `build-docker-image` from
  `(guix docker)`, which is a separate sub-problem.
