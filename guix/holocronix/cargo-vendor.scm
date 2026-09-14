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
;;;   * git entries are copied out of a checkout of their repository.  When
;;;     the crate lives in a cargo workspace, `workspace = true' manifest
;;;     fields are replaced by the concrete values (aux-files/
;;;     replace-workspace-values.py, from nixpkgs), because the workspace
;;;     root is not there to inherit from once the crate stands alone;
;;;   * share/cargo-config/config.toml redirects crates-io and every git
;;;     source to that directory.
;;;
;;; Path dependencies between crates of one git workspace need no rewriting:
;;; cargo resolves a `path' dependency of a non-path source by name and
;;; version within that same source.

(define-module (holocronix cargo-vendor)
  #:use-module (guix gexp)
  #:use-module (guix packages)
  #:use-module (guix download)
  #:use-module (guix git-download)
  #:use-module (guix git)
  #:use-module (guix base16)
  #:use-module (guix base32)
  #:use-module (guix build-system trivial)
  #:use-module (gnu packages base)          ;tar, coreutils
  #:use-module (gnu packages compression)   ;gzip
  #:use-module (gnu packages python)        ;python
  #:use-module (gnu packages python-build)  ;python-tomli-w
  #:use-module (ice-9 match)
  #:use-module (ice-9 rdelim)
  #:use-module (ice-9 regex)
  #:use-module (srfi srfi-1)
  #:use-module (srfi srfi-9)
  #:use-module (web uri)
  #:export (read-cargo-lock
            cargo-lock-version
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

(define (cargo-lock-version file)
  "Return the lockfile format version declared at the top of FILE, or 2 when
there is none (versions 1 and 2 omit it)."
  (call-with-input-file file
    (lambda (port)
      (let loop ((line (read-line port)))
        (cond
         ((eof-object? line) 2)
         ((string=? line "[[package]]") 2)
         ((string-match "^version = ([0-9]+)$" line)
          => (lambda (m) (string->number (match:substring m 1))))
         (else (loop (read-line port))))))))


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
;;; Git sources.
;;;

(define-record-type <git-source>
  (git-source spec url kind value commit)
  git-source?
  (spec   git-source-spec)     ;the full "git+..." string from Cargo.lock
  (url    git-source-url)
  (kind   git-source-kind)     ;"branch", "tag", "rev", or #f
  (value  git-source-value)    ;decoded, or #f
  (commit git-source-commit))

(define (git-package? pkg)
  (string-prefix? "git+" (or (lock-package-source pkg) "")))

(define (parse-git-source spec lock-version)
  "Parse SPEC, a Cargo.lock source of the form git+URL[?KIND=VALUE]#COMMIT.
Lockfile version 4 percent-encodes VALUE."
  (let* ((rest   (string-drop spec 4))
         (hash   (string-rindex rest #\#))
         (commit (substring rest (+ hash 1)))
         (head   (substring rest 0 hash))
         (q      (string-index head #\?))
         (url    (if q (substring head 0 q) head))
         (query  (and q (substring head (+ q 1))))
         (eq     (and query (string-index query #\=)))
         (kind   (and eq (substring query 0 eq)))
         (raw    (and eq (substring query (+ eq 1))))
         (value  (and raw
                      (if (>= lock-version 4)
                          (uri-decode raw #:decode-plus-to-space? #f)
                          raw))))
    (git-source spec url kind value commit)))

(define (git-source-checkout source git-checkouts git-hashes)
  "Return a file-like object holding a checkout of SOURCE.  GIT-CHECKOUTS
overrides, keyed by commit or by URL, win; then GIT-HASHES, keyed by commit,
give a fixed-output `git-fetch' origin; otherwise a `git-checkout' is
fetched at build time with no hash."
  (let ((url    (git-source-url source))
        (commit (git-source-commit source)))
    (cond
     ((or (assoc-ref git-checkouts commit)
          (assoc-ref git-checkouts url))
      => identity)
     ((assoc-ref git-hashes commit)
      => (lambda (hash)
           (origin
             (method git-fetch)
             (uri (git-reference (url url) (commit commit)))
             (file-name (string-append "cargo-git-" (string-take commit 7)
                                       "-checkout"))
             (sha256 (nix-base32-string->bytevector hash)))))
     (else
      (git-checkout (url url) (commit commit))))))


;;;
;;; Vendor directory.
;;;

(define (vendor-builder project crates git-packages git-specs offline?)
  "Return a gexp that populates the output with vendor/ and
share/cargo-config/config.toml.  CRATES is a list of
(name version checksum origin); GIT-PACKAGES a list of
(name version checkout); GIT-SPECS a list of (spec url kind value) for the
config, one per distinct git source."
  (define git-tools
    ;; Only pull Python into the build when there is something to rewrite.
    (and (not (null? git-packages))
         #~(list #+(file-append python "/bin/python3")
                 #+(local-file "aux-files/replace-workspace-values.py")
                 #+python-tomli-w)))

  (with-imported-modules '((guix build utils))
    #~(begin
        (use-modules (guix build utils)
                     (ice-9 match)
                     (ice-9 rdelim)
                     (ice-9 regex)
                     (ice-9 textual-ports)
                     (srfi srfi-1))

        (define out #$output)
        (define vendor (string-append out "/vendor"))

        (setenv "PATH" (string-append #+(file-append tar "/bin") ":"
                                      #+(file-append gzip "/bin") ":"
                                      #+(file-append coreutils "/bin")))
        (mkdir-p vendor)

        ;; crates.io crates: unpack, stub checksum with the lockfile hash.
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

        ;; Git crates: locate the crate in its checkout, copy it out,
        ;; resolve workspace inheritance, stub checksum without a hash.
        (define (manifest-field file field)
          ;; Value of FIELD in the [package] table of FILE, or #f.
          (let ((rx (string-append "^" field "[[:space:]]*=[[:space:]]*\"([^\"]+)\"")))
            (call-with-input-file file
              (lambda (port)
                (let loop ((line (read-line port)) (in-package? #f))
                  (cond
                   ((eof-object? line) #f)
                   ((string-match "^\\[package\\][[:space:]]*$" line)
                    (loop (read-line port) #t))
                   ((string-prefix? "[" line)
                    (loop (read-line port) #f))
                   ((and in-package? (string-match rx line))
                    => (lambda (m) (match:substring m 1)))
                   (else (loop (read-line port) in-package?))))))))

        (define (find-crate-manifest checkout name version)
          (let* ((manifests
                  (find-files checkout
                              (lambda (file stat)
                                (and (string=? (basename file) "Cargo.toml")
                                     (not (string-contains file "/target/"))))))
                 (matching
                  (filter (lambda (m)
                            (equal? (manifest-field m "name") name))
                          manifests)))
            (match matching
              (()
               (error "cargo-vendor: crate not found in git checkout"
                      name version checkout))
              ((one) one)
              (many
               (or (find (lambda (m)
                           (equal? (manifest-field m "version") version))
                         many)
                   (car many))))))

        (define (workspace-manifest? file)
          (call-with-input-file file
            (lambda (port)
              (let loop ((line (read-line port)))
                (cond
                 ((eof-object? line) #f)
                 ((string-match "^\\[workspace(\\]|\\.)" line) #t)
                 (else (loop (read-line port))))))))

        (define (find-workspace-root crate-dir checkout)
          ;; Walk up from CRATE-DIR, stopping at CHECKOUT.
          (let loop ((dir crate-dir))
            (let ((manifest (string-append dir "/Cargo.toml")))
              (cond
               ((and (file-exists? manifest) (workspace-manifest? manifest))
                manifest)
               ((or (string=? dir checkout) (string=? dir "/")) #f)
               (else (loop (dirname dir)))))))

        (define (mentions-workspace? manifest)
          ;; Same cheap test as nixpkgs.
          (string-contains (call-with-input-file manifest get-string-all)
                           "workspace"))

        (define git-tools #$git-tools)

        (for-each
         (match-lambda
           ((name version checkout)
            (let* ((manifest (find-crate-manifest checkout name version))
                   (src (dirname manifest))
                   (dir (string-append vendor "/" name "-" version)))
              (format #t "vendoring ~a ~a from ~a~%" name version src)
              (copy-recursively src dir #:log (%make-void-port "w"))
              (invoke "chmod" "-R" "u+w" dir)
              (when (mentions-workspace? (string-append dir "/Cargo.toml"))
                (let ((root (find-workspace-root src checkout)))
                  (if root
                      (match git-tools
                        ((python script tomli-w)
                         (setenv "GUIX_PYTHONPATH"
                                 (string-join
                                  (find-files tomli-w "^site-packages$"
                                              #:directories? #t)
                                  ":"))
                         (invoke python script
                                 (string-append dir "/Cargo.toml") root)))
                      (format #t "warning: ~a mentions a workspace but none \
was found above ~a~%" name src))))
              (call-with-output-file
                  (string-append dir "/.cargo-checksum.json")
                (lambda (port)
                  (display "{\"files\":{},\"package\":null}" port))))))
         (list #$@(map (match-lambda
                         ((name version checkout)
                          #~(list #$name #$version #$checkout)))
                       git-packages)))

        ;; Cargo config: crates-io and each git source replaced by vendor/.
        (let ((config-dir (string-append out "/share/cargo-config")))
          (mkdir-p config-dir)
          (call-with-output-file (string-append config-dir "/config.toml")
            (lambda (port)
              (format port "# Generated by holocronix cargo-vendor for ~a.~%~%"
                      #$project)
              (when #$offline?
                (display "[net]\noffline = true\n\n" port))
              (format port "[source.crates-io]
replace-with = \"vendored-sources\"

[source.vendored-sources]
directory = ~s
" vendor)
              (for-each
               (match-lambda
                 ((spec url kind value)
                  (format port "~%[source.~s]~%git = ~s~%" spec url)
                  (when kind
                    (format port "~a = ~s~%" kind value))
                  (display "replace-with = \"vendored-sources\"\n" port)))
               '#$git-specs)))))))

(define* (cargo-vendor project lockfile
                       #:key (version "0") (offline? #t)
                       (git-checkouts '()) (git-hashes '()))
  "Return a package whose output holds a `cargo vendor'-style directory with
every dependency listed in LOCKFILE, a path to a Cargo.lock, plus
share/cargo-config/config.toml pointing cargo at it.  PROJECT names the
project in the package name and config header.

With OFFLINE? true the config also sets net.offline so cargo never tries the
network.

Git dependencies are fetched as unhashed `git-checkout' objects by default.
GIT-HASHES is an alist of commit to nix-base32 sha256 of the checkout; those
commits become fixed-output `git-fetch' origins instead.  GIT-CHECKOUTS is an
alist of commit or URL to a file-like object to use as the checkout, for
local fixtures or pre-fetched sources."
  (let* ((lock-version (cargo-lock-version lockfile))
         (deps (filter lock-package-source (read-cargo-lock lockfile)))
         (registry (filter crates-io-package? deps))
         (git (filter git-package? deps))
         (unsupported (remove (lambda (pkg)
                                (or (crates-io-package? pkg)
                                    (git-package? pkg)))
                              deps))
         (sources (map (lambda (pkg)
                         (parse-git-source (lock-package-source pkg)
                                           lock-version))
                       git))
         ;; One checkout per commit, shared by every crate of that repo.
         (checkouts
          (fold (lambda (source acc)
                  (let ((commit (git-source-commit source)))
                    (if (assoc-ref acc commit)
                        acc
                        (cons (cons commit
                                    (git-source-checkout source
                                                         git-checkouts
                                                         git-hashes))
                              acc))))
                '()
                sources))
         (git-packages
          (map (lambda (pkg source)
                 (list (lock-package-name pkg)
                       (lock-package-version pkg)
                       (assoc-ref checkouts (git-source-commit source))))
               git sources))
         (git-specs
          (delete-duplicates
           (map (lambda (source)
                  (list (git-source-spec source)
                        (git-source-url source)
                        (git-source-kind source)
                        (git-source-value source)))
                sources)))
         ;; Built here on purpose: inside `package' below, `name' and
         ;; `version' refer to the record's own fields.
         (builder (vendor-builder project
                                  (map (lambda (pkg)
                                         (list (lock-package-name pkg)
                                               (lock-package-version pkg)
                                               (lock-package-checksum pkg)
                                               (crate-origin pkg)))
                                       registry)
                                  git-packages
                                  git-specs
                                  offline?)))
    (unless (null? unsupported)
      (error "cargo-vendor: unsupported sources (crates.io and git only):"
             (delete-duplicates (map lock-package-source unsupported))))
    (package
      (name (string-append project "-cargo-vendor"))
      (version version)
      (source #f)
      (build-system trivial-build-system)
      (arguments (list #:builder builder))
      (synopsis (string-append "Vendored crates for " project))
      (description
       (string-append "Every dependency from the Cargo.lock of " project
                      ", laid out as a cargo directory source, with a "
                      "config.toml that redirects crates-io and git sources "
                      "to it."))
      (home-page #f)
      (license #f))))
