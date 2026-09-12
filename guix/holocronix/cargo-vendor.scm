;;; holocronix --- sandboxed containers for coding agents
;;;
;;; (holocronix cargo-vendor): turn a project's Cargo.lock into a Guix
;;; package holding a `cargo vendor'-style directory, so an image built with
;;; `guix pack' carries every crate the project needs and `cargo build'
;;; works with no network access.
;;;
;;; The approach mirrors nixpkgs' importCargoLock:
;;;
;;;   * every crates.io entry in Cargo.lock becomes a fixed-output download.
;;;     The lockfile's `checksum' *is* the sha256 of the .crate tarball, so
;;;     no extra hashing step is needed and no network is touched at
;;;     evaluation time;
;;;   * each crate is unpacked to vendor/<name>-<version>/ with a stub
;;;     .cargo-checksum.json carrying that checksum, which is all cargo
;;;     verifies for a "directory" source;
;;;   * share/cargo-config/config.toml redirects crates-io to that directory.
;;;
;;; Git dependencies are not supported yet.  They need the crate copied out of
;;; its checkout and workspace-inherited manifest fields resolved; that is the
;;; next step.

(define-module (holocronix cargo-vendor)
  #:use-module (guix gexp)
  #:use-module (guix packages)
  #:use-module (guix download)
  #:use-module (guix base16)
  #:use-module (guix build-system trivial)
  #:use-module (gnu packages base)          ;tar
  #:use-module (gnu packages compression)   ;gzip
  #:use-module (ice-9 match)
  #:use-module (ice-9 rdelim)
  #:use-module (ice-9 regex)
  #:use-module (srfi srfi-1)
  #:use-module (srfi srfi-9)
  #:export (read-cargo-lock
            lock-package
            lock-package?
            lock-package-name
            lock-package-version
            lock-package-source
            lock-package-checksum
            cargo-vendor))


;;;
;;; Cargo.lock reader.
;;;
;;; Cargo.lock is TOML, but Guix's (guix build toml) drops array-of-tables
;;; entries, which is the whole body of a lockfile.  The format cargo writes
;;; is regular enough (one `key = "value"' per line) that a line reader is
;;; both simpler and more robust than a grammar.  Handles lockfile versions
;;; 2 through 4.
;;;

(define-record-type <lock-package>
  (lock-package name version source checksum)
  lock-package?
  (name     lock-package-name)
  (version  lock-package-version)
  (source   lock-package-source)      ;#f for workspace members
  (checksum lock-package-checksum))   ;hex sha256 of the .crate, or #f

(define %key-value-rx
  (make-regexp "^([a-z_-]+) = \"([^\"]*)\"$"))

(define (read-cargo-lock file)
  "Parse FILE, a Cargo.lock, and return the list of <lock-package> records it
declares, in file order."
  (define (finish fields acc)
    (if (null? fields)
        acc
        (cons (lock-package (assoc-ref fields "name")
                            (assoc-ref fields "version")
                            (assoc-ref fields "source")
                            (assoc-ref fields "checksum"))
              acc)))

  (call-with-input-file file
    (lambda (port)
      (let loop ((line (read-line port))
                 (in-package? #f)
                 (fields '())
                 (acc '()))
        (cond
         ((eof-object? line)
          (reverse (finish fields acc)))
         ((string=? line "[[package]]")
          (loop (read-line port) #t '() (finish fields acc)))
         ((string-prefix? "[" line)         ;[metadata] or anything else
          (loop (read-line port) #f '() (finish fields acc)))
         (in-package?
          (let ((m (regexp-exec %key-value-rx line)))
            (loop (read-line port) #t
                  (if m
                      (cons (cons (match:substring m 1) (match:substring m 2))
                            fields)
                      fields)
                  acc)))
         (else
          (loop (read-line port) #f fields acc)))))))


;;;
;;; crates.io sources.
;;;

(define %crates-io-sources
  '("registry+https://github.com/rust-lang/crates.io-index"
    "sparse+https://index.crates.io/"))

(define (crates-io-package? pkg)
  (and (member (lock-package-source pkg) %crates-io-sources) #t))

(define (crate-origin pkg)
  "Return an <origin> for the .crate tarball of PKG, hashed with the checksum
recorded in Cargo.lock."
  (let ((name (lock-package-name pkg))
        (version (lock-package-version pkg)))
    (origin
      (method url-fetch)
      ;; static.crates.io is the CDN; the API host is rate limited.
      (uri (list (string-append "https://static.crates.io/crates/"
                                name "/" version "/download")
                 (string-append "https://crates.io/api/v1/crates/"
                                name "/" version "/download")))
      ;; Same file name as Guix's own `crate-source', so the store item is
      ;; shared with crates fetched for (gnu packages rust-crates).
      (file-name (string-append "rust-"
                                (string-map (lambda (c)
                                              (if (char=? c #\_) #\- c))
                                            name)
                                "-" version ".tar.gz"))
      (sha256 (base16-string->bytevector (lock-package-checksum pkg))))))


;;;
;;; Vendor directory.
;;;

(define (vendor-builder name crates offline?)
  "Return a gexp that populates the output with vendor/ and
share/cargo-config/config.toml.  CRATES is a list of
(name version checksum origin)."
  (with-imported-modules '((guix build utils))
    #~(begin
        (use-modules (guix build utils)
                     (ice-9 match))

        (define out #$output)
        (define vendor (string-append out "/vendor"))

        (setenv "PATH" (string-append #+(file-append tar "/bin") ":"
                                      #+(file-append gzip "/bin")))
        (mkdir-p vendor)

        (for-each
         (match-lambda
           ((name version checksum crate)
            (let ((dir (string-append vendor "/" name "-" version)))
              (format #t "vendoring ~a ~a~%" name version)
              (mkdir-p dir)
              (invoke "tar" "xf" crate "-C" dir "--strip-components=1")
              ;; Cargo only verifies the files listed here.  An empty map
              ;; plus the lockfile checksum is what nixpkgs writes too.
              (call-with-output-file
                  (string-append dir "/.cargo-checksum.json")
                (lambda (port)
                  (format port "{\"files\":{},\"package\":~s}" checksum))))))
         (list #$@(map (match-lambda
                         ((name version checksum origin)
                          #~(list #$name #$version #$checksum #$origin)))
                       crates)))

        (let ((config-dir (string-append out "/share/cargo-config")))
          (mkdir-p config-dir)
          (call-with-output-file (string-append config-dir "/config.toml")
            (lambda (port)
              (format port "# Generated by holocronix cargo-vendor for ~a.~%~%"
                      #$name)
              (when #$offline?
                (display "[net]\noffline = true\n\n" port))
              (format port "[source.crates-io]
replace-with = \"vendored-sources\"

[source.vendored-sources]
directory = ~s
" vendor)))))))

(define* (cargo-vendor project lockfile #:key (version "0") (offline? #t))
  "Return a package whose output holds a `cargo vendor'-style directory with
every crates.io dependency listed in LOCKFILE, a path to a Cargo.lock, plus
share/cargo-config/config.toml pointing cargo at it.  PROJECT names the
project in the package name and config header.  With OFFLINE? true the config
also sets net.offline so cargo never tries the network."
  (let* ((deps (filter lock-package-source (read-cargo-lock lockfile)))
         (unsupported (remove crates-io-package? deps))
         ;; Built here on purpose: inside `package' below, `name' and
         ;; `version' refer to the record's own fields.
         (builder (vendor-builder project
                                  (map (lambda (pkg)
                                         (list (lock-package-name pkg)
                                               (lock-package-version pkg)
                                               (lock-package-checksum pkg)
                                               (crate-origin pkg)))
                                       deps)
                                  offline?)))
    (unless (null? unsupported)
      (error "cargo-vendor: only crates.io sources are supported for now:"
             (delete-duplicates (map lock-package-source unsupported))))
    (package
      (name (string-append project "-cargo-vendor"))
      (version version)
      (source #f)
      (build-system trivial-build-system)
      (arguments (list #:builder builder))
      (synopsis (string-append "Vendored crates for " project))
      (description
       (string-append "Every crates.io dependency from the Cargo.lock of "
                      project ", laid out as a cargo directory source, with "
                      "a config.toml that redirects crates-io to it."))
      (home-page #f)
      (license #f))))
