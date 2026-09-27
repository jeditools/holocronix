;;; holocronix --- sandboxed containers for coding agents
;;;
;;; (holocronix agents): the coding agents a jedicave ships, as Guix
;;; packages.  The counterpart of the llm-agents.nix input in flake.nix.
;;;
;;; Vendors distribute these agents as single-file executables built with
;;; Bun, not as source, so a package here is the vendor's binary, unmodified,
;;; plus a wrapper.  Two facts shape that:
;;;
;;; - The binary asks for /lib64/ld-linux-x86-64.so.2 as its program
;;;   interpreter, the FHS path, which a Guix image does not have.  The
;;;   usual fix, `patchelf --set-interpreter', does not apply: Bun's
;;;   executable layout does not survive the relocation patchelf performs,
;;;   and the result segfaults on start (llm-agents.nix wrote wrap-buddy
;;;   instead of using patchelf for the same reason).  So the binary stays
;;;   as shipped and `jedicave-image' adds that one symlink to the image,
;;;   pointing at Guix's glibc.  Guix's ld.so searches glibc's own lib
;;;   directory by default, so libc, libm and friends resolve with no RPATH
;;;   and no LD_LIBRARY_PATH.  Where the symlink is absent -- `guix shell',
;;;   the build container -- the wrapper runs the loader explicitly instead.
;;;
;;; - Claude Code embeds a ripgrep with the same problem.
;;;   USE_BUILTIN_RIPGREP=0 makes it use the rg on PATH, Guix's.
;;;
;;; x86_64-linux only for now.  nixpkgs' claude-code package records the
;;; other platforms' hashes for the same release (URL platform
;;; `linux-arm64', `darwin-arm64', ...).

(define-module (holocronix agents)
  #:use-module (guix packages)
  #:use-module (guix download)
  #:use-module (guix gexp)
  #:use-module (guix utils)
  #:use-module (guix build-system copy)
  #:use-module (gnu packages base)
  #:use-module (gnu packages bash)
  #:use-module (gnu packages bootstrap)
  #:use-module (gnu packages compression)
  #:use-module (gnu packages rust-apps)
  #:export (claude-code
            %jedicave-default-agents
            fhs-dynamic-linker))


(define (nonfree uri)
  "Return a license object for proprietary terms found at URI.  (guix
licenses) has no constructor for those; this is how nonguix defines one."
  ((@@ (guix licenses) license) "Nonfree" uri
   "This is a nonfree license.  Check the URI for details."))

(define* (fhs-dynamic-linker #:optional (system (%current-system)))
  "Return the absolute path a prebuilt binary for SYSTEM asks for as its
program interpreter: the FHS location the vendor linked against."
  (cond ((string-prefix? "x86_64-" system) "/lib64/ld-linux-x86-64.so.2")
        ((string-prefix? "aarch64-" system) "/lib/ld-linux-aarch64.so.1")
        ((string-prefix? "i686-" system) "/lib/ld-linux.so.2")
        (else (error "fhs-dynamic-linker: no path known for" system))))


;;;
;;; Claude Code.
;;;

(define %claude-code-releases
  ;; Anthropic's download host, the one nixpkgs fetches from.  Each release
  ;; directory holds the executable as `claude' and as `claude.zst', the
  ;; latter about a quarter of the size.  llm-agents.nix fetches the plain
  ;; file from the storage.googleapis.com bucket claude-code-dist-86c565f3-
  ;; f756-42ad-8dfa-d59b1c096819/claude-code-releases/ instead.
  "https://downloads.claude.ai/claude-code-releases/")

(define-public claude-code
  (package
    (name "claude-code")
    ;; The version nixpkgs shipped on 2026-09-27, then the `latest' tag.
    ;; flake.nix pins llm-agents.nix at 2.1.231, the `stable' tag with soak;
    ;; the two move independently.  Bump deliberately.  The hash is the
    ;; release file's as nixpkgs records it, re-derived from the bytes Nix
    ;; fetched for it.
    (version "2.1.283")
    (source
     (origin
       (method url-fetch)
       (uri (string-append %claude-code-releases version
                           "/linux-x64/claude.zst"))
       (file-name (string-append "claude-code-" version "-linux-x64.zst"))
       (sha256
        (base32 "1sr5z91kc7m5qzrlxkacq427dimiync4yjckhdxddqwbx1hmhd4l"))))
    (build-system copy-build-system)
    (arguments
     (list
      #:install-plan #~'(("claude" "libexec/claude-code/"))
      ;; Bun packs the program into the executable, which `strip' would
      ;; remove; and there is no RUNPATH to validate (see the top of this
      ;; file for how libc is found).
      #:strip-binaries? #f
      #:validate-runpath? #f
      #:modules '((guix build copy-build-system)
                  (guix build utils)
                  (ice-9 popen)
                  (ice-9 rdelim))
      #:phases
      #~(modify-phases %standard-phases
          (replace 'unpack
            ;; The source is the executable itself, zstd-compressed; not an
            ;; archive.
            (lambda* (#:key source #:allow-other-keys)
              (invoke "zstd" "-d" "-q" "-o" "claude" source)
              (chmod "claude" #o755)))
          (add-after 'install 'wrap
            (lambda* (#:key inputs outputs #:allow-other-keys)
              (let* ((out (assoc-ref outputs "out"))
                     (real (string-append out "/libexec/claude-code/claude"))
                     (wrapper (string-append out "/bin/claude"))
                     (loader (search-input-file
                              inputs #$(string-drop (glibc-dynamic-linker) 1)))
                     (rg (dirname (search-input-file inputs "bin/rg"))))
                (mkdir-p (dirname wrapper))
                (call-with-output-file wrapper
                  (lambda (port)
                    ;; Same knobs as the llm-agents.nix wrapper: no
                    ;; self-update, no "installed wrong" nagging, and
                    ;; DISABLE_NON_ESSENTIAL_MODEL_CALLS on unless the user
                    ;; says otherwise.  Its bubblewrap/socat PATH additions
                    ;; are left out on purpose: in a jedicave those are
                    ;; root-only infrastructure tools.
                    (format port "#!~a
# Wrapper written by (holocronix agents); see that module for the why.
export DISABLE_AUTOUPDATER=1
export DISABLE_INSTALLATION_CHECKS=1
export USE_BUILTIN_RIPGREP=0
export DISABLE_NON_ESSENTIAL_MODEL_CALLS=\"${DISABLE_NON_ESSENTIAL_MODEL_CALLS-1}\"
export PATH=\"~a${PATH:+:$PATH}\"
if [ -e ~a ]; then
  exec -a claude ~a \"$@\"
else
  # No FHS loader here: run Guix's explicitly.
  exec ~a --argv0 claude ~a \"$@\"
fi
"
                            (search-input-file inputs "bin/bash")
                            rg #$(fhs-dynamic-linker) real loader real)))
                (chmod wrapper #o755))))
          (add-after 'wrap 'check-version
            (lambda* (#:key outputs #:allow-other-keys)
              ;; The build container has no /lib64, so this exercises the
              ;; wrapper's loader fallback; it proves the glibc matches and
              ;; the binary is intact.
              (setenv "HOME" (getcwd))  ; Claude Code writes under $HOME
              (let* ((port (open-pipe* OPEN_READ
                                       (string-append (assoc-ref outputs "out")
                                                      "/bin/claude")
                                       "--version"))
                     (line (read-line port)))
                (close-pipe port)
                (unless (and (string? line) (string-contains line #$version))
                  (error "claude --version did not report" #$version line))))))))
    (native-inputs (list zstd))
    (inputs (list bash-minimal glibc ripgrep))
    (supported-systems '("x86_64-linux"))
    (home-page "https://claude.com/claude-code")
    (synopsis "Anthropic's coding agent for the terminal")
    (description
     "Claude Code is Anthropic's agentic coding tool.  This package is the
vendor's prebuilt @code{linux-x64} executable with a wrapper that disables
self-updating and points it at Guix's ripgrep.  It expects the FHS dynamic
linker path to exist; @code{jedicave-image} provides it.")
    (license (nonfree "https://www.anthropic.com/legal/commercial-terms"))))


(define %jedicave-default-agents
  ;; What a jedicave ships unless cave.scm says otherwise.  flake.nix also
  ;; has kimi-code, opencode, ori and qwen-code; see guix/README.md for why
  ;; those are not here yet.
  (list claude-code))
