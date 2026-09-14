;; Guix manifest for a container image that cross-compiles hello-xous for
;; riscv32imac-unknown-xous-elf offline, using the rust-xous-toolchain
;; package from baobit.
;;
;; baobit is consumed as a load path, the way its own Makefile does, under
;; the Guix commit baobit pins (its channels/guix.scm).  The channel form
;; (`(channel (name 'baobit) ...)`) would be cleaner, but baobit's channel
;; authentication is broken on main at the moment, see
;; baobit/note-channel-auth-broken.md.
;;
;; From the holocronix checkout, with BAOBIT pointing at a baobit checkout:
;;
;;   guix time-machine -C $BAOBIT/channels/guix.scm -- \
;;       pack -L guix -L $BAOBIT/packages -f docker \
;;       -m examples/hello-xous/manifest.scm \
;;       -S /bin=bin -S /.cargo=share/cargo-config \
;;       --entry-point=bin/bash --image-tag=hello-xous-cave
;;
;; The toolchain wrapper provides rustc, cargo, cc, and rust-lld; its merged
;; sysroot carries the host target plus riscv32imac-unknown-xous-elf and
;; riscv32imac-unknown-none-elf.

(use-modules (holocronix cargo-vendor)
             (rust-xous-toolchain)
             (gnu packages base)
             (gnu packages bash))

(define here (dirname (current-filename)))

(define vendored
  (cargo-vendor "hello-xous" (string-append here "/Cargo.lock")))

(packages->manifest
 (list rust-xous-toolchain
       bash
       coreutils
       vendored))
