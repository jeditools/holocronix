;; A full jedicave image for hello-rust: the jedicave base tools, the Rust
;; toolchain, and the vendored crates, with user yoda, /workspace, and the
;; jedicave environment.  Unlike manifest.scm, which feeds `guix pack', this
;; evaluates to the Docker image itself:
;;
;;   guix build -L guix -f examples/hello-rust/cave.scm
;;   docker load < $(guix build -L guix -f examples/hello-rust/cave.scm)
;;
;; Then, for instance:
;;
;;   docker run -d --name hello hello-rust-jedicave:latest
;;   docker exec hello sh -c 'id; pwd; cargo --version'
;;   docker rm -f hello

(use-modules (holocronix jedicave)
             (holocronix cargo-vendor)
             (gnu packages))

(define here (dirname (current-filename)))

(jedicave-image
 #:name "hello-rust-jedicave"
 #:extra-packages
 (append (specifications->packages '("rust" "rust:cargo"))
         (list (cargo-vendor "hello-rust" (string-append here "/Cargo.lock"))))
 #:symlinks '(("/.cargo" . "share/cargo-config")))
