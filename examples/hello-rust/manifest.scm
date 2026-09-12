;; Guix manifest for a container image that can build hello-rust offline.
;;
;; From the holocronix checkout:
;;
;;   guix pack -L guix -f docker -m examples/hello-rust/manifest.scm \
;;       -S /bin=bin -S /.cargo=share/cargo-config \
;;       --entry-point=bin/bash --image-tag=hello-rust-cave
;;   docker load < result
;;
;; The /.cargo symlink puts the generated config.toml at the filesystem root,
;; where cargo picks it up for any project under / without touching
;; CARGO_HOME (which must stay writable).

(use-modules (holocronix cargo-vendor)
             (gnu packages rust)
             (gnu packages commencement)
             (gnu packages base)
             (gnu packages bash))

(define vendored
  (cargo-vendor "hello-rust"
                (string-append (dirname (current-filename)) "/Cargo.lock")))

(packages->manifest
 (list rust
       (list rust "cargo")
       gcc-toolchain
       bash
       coreutils
       vendored))
