;; Guix manifest for a container image that can build hello-rust-git
;; offline, including its git dependency.
;;
;; From the holocronix checkout:
;;
;;   guix pack -L guix -f docker -m examples/hello-rust-git/manifest.scm \
;;       -S /bin=bin -S /.cargo=share/cargo-config \
;;       --entry-point=bin/bash --image-tag=hello-rust-git-cave
;;
;; See examples/hello-rust/manifest.scm for the crates.io-only variant and
;; guix/README.md for the full walkthrough.

(use-modules (holocronix cargo-vendor)
             (guix gexp)
             (gnu packages rust)
             (gnu packages commencement)
             (gnu packages base)
             (gnu packages bash))

(define here (dirname (current-filename)))

(define vendored
  (cargo-vendor "hello-rust-git"
                (string-append here "/Cargo.lock")
                ;; Cargo.lock points at the fixture's file:// URL.  Hand the
                ;; checkout to Guix directly instead of letting it clone.
                #:git-checkouts
                `(("file:///tmp/holocronix-fixtures/greeter"
                   . ,(local-file (string-append here "/../fixtures/greeter")
                                  "greeter-checkout"
                                  #:recursive? #t)))))

(packages->manifest
 (list rust
       (list rust "cargo")
       gcc-toolchain
       bash
       coreutils
       vendored))
