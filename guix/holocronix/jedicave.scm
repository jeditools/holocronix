;;; holocronix --- sandboxed containers for coding agents
;;;
;;; (holocronix jedicave): build a jedicave Docker image with Guix.  This is
;;; the Guix counterpart of lib/mkJediCave.nix: a profile of tools, an
;;; entrypoint, /etc/passwd and friends, a home directory for the agent
;;; user, root-only infrastructure tools, and the environment variables the
;;; agent sees.
;;;
;;; `guix pack -f docker' cannot set the image user, working directory, or
;;; arbitrary environment variables, and it archives every file as root, so
;;; the image is assembled with (holocronix docker), a small fork of Guix's
;;; docker builder that adds those.

(define-module (holocronix jedicave)
  #:use-module (guix gexp)
  #:use-module (guix modules)
  #:use-module (guix profiles)
  #:use-module (guix packages)
  #:use-module ((guix store) #:select (%store-prefix))
  #:use-module ((guix self) #:select (make-config.scm))
  #:use-module (gnu packages)
  #:use-module (gnu packages base)
  #:use-module (gnu packages bash)
  #:use-module (gnu packages compression)
  #:use-module (gnu packages gnupg)
  #:use-module (gnu packages guile)
  #:use-module (ice-9 match)
  #:use-module (srfi srfi-1)
  #:export (%jedicave-base-specs
            %jedicave-infra-specs
            jedicave-entrypoint
            jedicave-image))


;;;
;;; Package sets.  Specifications rather than variables, so the module does
;;; not have to import a dozen (gnu packages ...) modules; resolved lazily.
;;;

(define %jedicave-base-specs
  ;; Same set as jedicavePackages in lib/mkJediCave.nix, under Guix names.
  ;; Missing on Guix: oh-my-zsh, systemd headers.
  '(;; Core
    "coreutils" "diffutils" "findutils" "bash" "zsh" "git" "nss-certs"
    "git-filter-repo" "tar" "gzip"
    ;; CLI tools
    "fd" "ripgrep" "grep" "fzf" "git-delta" "tmux" "ast-grep" "jq" "nano"
    "unzip" "vim" "curl" "sed" "gawk" "less" "poppler"
    ;; Build tools
    "gcc-toolchain" "make" "pkg-config"
    ;; Monitoring / diagnostics (read-only, low-risk)
    "iputils" "procps"
    ;; Languages
    "python" "uv" "node"))

(define %jedicave-infra-specs
  ;; Installed to /usr/local/sbin (root-only), off the agent's PATH.
  '("bubblewrap" "socat" "bind:utils" "iptables" "ipset" "iproute2"))


;;;
;;; Entrypoint.
;;;

(define* (jedicave-entrypoint #:key
                              (claude? #f)
                              (claude-settings #f)
                              (project-setup ""))
  "Return an executable file-like object: the container entrypoint.  It runs
first-boot setup, injects an L7 proxy CA when one is mounted, clones the bare
repositories under /repos into /workspace, runs PROJECT-SETUP (shell text),
then sleeps forever.  When CLAUDE? is true and CLAUDE-SETTINGS is a file-like
object, it is seeded into $CLAUDE_CONFIG_DIR on first boot."
  (define claude-setup
    (if claude?
        (list "
      # Claude Code config (CLAUDE_CONFIG_DIR is typically a Docker volume).
      mkdir -p \"$CLAUDE_CONFIG_DIR\"
      [ -f \"$CLAUDE_CONFIG_DIR/settings.json\" ] || \\
        cp " (or claude-settings "/dev/null") " \"$CLAUDE_CONFIG_DIR/settings.json\"
      chmod -R u+rw \"$CLAUDE_CONFIG_DIR\" 2>/dev/null || true
")
        '()))

  (define script
    (apply mixed-text-file "jedicave-start.sh"
           `("#!" ,bash "/bin/bash
if [ ! -f \"$HOME/.jedicave-initialized\" ]; then
  echo \"[jedicave] First-boot setup...\"
" ,@claude-setup "
  touch \"$HOME/.jedicave-initialized\"
  echo \"[jedicave] Setup complete.\"
fi

# L7 proxy CA: merge proxy CA cert with the system bundle so HTTPS
# interception works transparently for all tools (curl, git, pip, etc.)
if [ -f /proxy-ca/mitmproxy-ca-cert.pem ]; then
  echo \"[jedicave] Injecting proxy CA certificate...\"
  cat \"$SSL_CERT_FILE\" /proxy-ca/mitmproxy-ca-cert.pem > /tmp/ca-bundle.crt
  export SSL_CERT_FILE=/tmp/ca-bundle.crt
  export GIT_SSL_CAINFO=/tmp/ca-bundle.crt
  export NODE_EXTRA_CA_CERTS=/proxy-ca/mitmproxy-ca-cert.pem
  export REQUESTS_CA_BUNDLE=/tmp/ca-bundle.crt
fi

# Clone bare repos from /repos/ into /workspace/
if [ -d /repos ]; then
  for bare in /repos/*.git; do
    [ -d \"$bare\" ] || continue
    repo_name=$(basename \"$bare\" .git)
    if [ ! -d \"/workspace/$repo_name\" ]; then
      echo \"[jedicave] Cloning $repo_name...\"
      git clone \"$bare\" \"/workspace/$repo_name\"
      # Materialize every seeded branch as a local tracking branch.
      git -C \"/workspace/$repo_name\" for-each-ref \\
        --format='%(refname:short)' refs/remotes/origin/ |
        while read -r remote_ref; do
          branch=\"${remote_ref#origin/}\"
          [ \"$branch\" = \"HEAD\" ] && continue
          git -C \"/workspace/$repo_name\" show-ref --verify --quiet \\
            \"refs/heads/$branch\" ||
            git -C \"/workspace/$repo_name\" branch --track \\
              \"$branch\" \"$remote_ref\"
        done
    fi
  done
fi

# Project-specific setup (env, vendored deps, etc.)
" ,project-setup "

exec sleep infinity
")))

  (computed-file "jedicave-start"
                 #~(begin
                     (copy-file #$script #$output)
                     (chmod #$output #o755))))


;;;
;;; Image.
;;;

(define (not-config? module)
  ;; Modules to import into the build side, like guix pack does: Guix's own
  ;; and ours, except (guix config) which is substituted by make-config.scm.
  (match module
    (('guix 'config) #f)
    (('guix _ ...) #t)
    (('gnu _ ...) #t)
    (('holocronix _ ...) #t)
    (_ #f)))

(define* (jedicave-image #:key
                         (name "jedicave")
                         (packages (specifications->packages
                                    %jedicave-base-specs))
                         (extra-packages '())
                         (infra-packages (specifications->packages
                                          %jedicave-infra-specs))
                         (env '())
                         (symlinks '())
                         (user "yoda")
                         (uid 1000)
                         (gid 1000)
                         (git-user "Yoda")
                         (git-email "yoda@jedicave.kyb")
                         (claude? #f)
                         (claude-settings #f)
                         (project-setup "")
                         (extra-directives '())
                         (max-layers 100))
  "Return a file-like object: a gzip-compressed Docker image archive named
NAME, loadable with `docker load'.

PACKAGES plus EXTRA-PACKAGES form the agent's profile, linked at /bin and put
on PATH.  INFRA-PACKAGES go to /usr/local/sbin, root-only.  ENV is an alist
of extra environment variables; it overrides the defaults.  SYMLINKS is an
alist of (IMAGE-PATH . PROFILE-RELATIVE-TARGET), for instance
(\"/.cargo\" . \"share/cargo-config\").  EXTRA-DIRECTIVES are appended to the
populate directives (see 'evaluate-populate-directive')."
  ;; Inside a `profile' form, `name' refers to the record's own field, so
  ;; the strings are computed outside.
  (define agent-profile
    (let ((profile-name (string-append name "-profile")))
      (profile
       (name profile-name)
       (content (packages->manifest (append packages extra-packages)))
       (allow-collisions? #t))))

  (define infra-profile
    (let ((profile-name (string-append name "-infra")))
      (profile
       (name profile-name)
       (content (packages->manifest infra-packages))
       (hooks '())
       (locales? #f))))

  (define entrypoint
    (jedicave-entrypoint #:claude? claude?
                         #:claude-settings claude-settings
                         #:project-setup project-setup))

  (define gitconfig
    (plain-file "gitconfig.local"
                (string-append "[user]
    name = " git-user "
    email = " git-email "
[core]
    excludesfile = ~/.gitignore_global
    pager = delta
[interactive]
    diffFilter = delta --color-only
[delta]
    navigate = true
    light = false
    line-numbers = true
    side-by-side = false
[merge]
    conflictstyle = diff3
[diff]
    colorMoved = default
")))

  (define build
    (with-extensions (list guile-json-3 guile-gcrypt)
      (with-imported-modules `(((guix config) => ,(make-config.scm))
                               ,@(source-module-closure
                                  '((holocronix docker)
                                    (guix build store-copy)
                                    (guix build utils)
                                    (guix profiles)
                                    (guix search-paths))
                                  #:select? not-config?))
        #~(begin
            (use-modules (holocronix docker)
                         (guix config)
                         (guix build store-copy)
                         (guix build utils)
                         (guix profiles)
                         (guix search-paths)
                         (srfi srfi-1)
                         (srfi srfi-19)
                         (ice-9 ftw)
                         (ice-9 match)
                         (ice-9 textual-ports))

            (define profile-dir #$agent-profile)
            (define infra #$infra-profile)
            (define entrypoint #$entrypoint)
            (define home (string-append "/home/" #$user))
            (define uid #$uid)
            (define gid #$gid)

            (define (closure graph)
              (map store-info-item
                   (call-with-input-file graph read-reference-graph)))

            (define paths
              (delete-duplicates
               (append (closure "profile")
                       (closure "infra")
                       (closure "entrypoint"))))

            (define (in-profile file)
              (string-append profile-dir file))

            (define shell
              (if (file-exists? (in-profile "/bin/zsh"))
                  (in-profile "/bin/zsh")
                  (in-profile "/bin/bash")))

            (define (read-file file)
              (call-with-input-file file get-string-all))

            ;; Environment: profile search paths, then jedicave defaults,
            ;; then the caller's ENV; later entries win.
            (define (merge-env . alists)
              (reverse
               (fold (lambda (entry acc)
                       (cons entry (alist-delete (car entry) acc)))
                     '()
                     (concatenate alists))))

            (define search-path-env
              (map (match-lambda
                     ((spec . value)
                      (cons (search-path-specification-variable spec) value)))
                   (profile-search-paths profile-dir)))

            (define environment
              (merge-env
               search-path-env
               `(("PATH" . ,(string-append profile-dir "/bin:"
                                           profile-dir "/sbin"))
                 ("PKG_CONFIG_PATH" . ,(in-profile "/lib/pkgconfig"))
                 ("SHELL" . ,shell)
                 ("USER" . #$user)
                 ("HOME" . ,home)
                 ("TERM" . "xterm-256color")
                 ("LANG" . "C.UTF-8")
                 ("DEVCONTAINER" . "true")
                 ("EDITOR" . "vim")
                 ("VISUAL" . "vim")
                 ("SSL_CERT_FILE"
                  . ,(in-profile "/etc/ssl/certs/ca-certificates.crt"))
                 ("SSL_CERT_DIR" . ,(in-profile "/etc/ssl/certs"))
                 ("GIT_SSL_CAINFO"
                  . ,(in-profile "/etc/ssl/certs/ca-certificates.crt"))
                 ("NODE_OPTIONS" . "--max-old-space-size=4096")
                 ("CLAUDE_CONFIG_DIR" . "/env/.claude")
                 ("CLAUDE_CODE_PLUGIN_SEED_DIR" . "/env/.claude-plugin-seed")
                 ("ZSH_CACHE_DIR" . ,(string-append home "/.cache/oh-my-zsh"))
                 ("GIT_CONFIG_GLOBAL" . ,(string-append home "/.gitconfig.local"))
                 ("UV_LINK_MODE" . "copy")
                 ("PYTHONDONTWRITEBYTECODE" . "1")
                 ("PIP_DISABLE_PIP_VERSION_CHECK" . "1")
                 ("NPM_CONFIG_IGNORE_SCRIPTS" . "true")
                 ("NPM_CONFIG_AUDIT" . "true")
                 ("NPM_CONFIG_FUND" . "false")
                 ("NPM_CONFIG_SAVE_EXACT" . "true")
                 ("NPM_CONFIG_UPDATE_NOTIFIER" . "false")
                 ("NPM_CONFIG_MINIMUM_RELEASE_AGE" . "1440"))
               '#$env))

            (define (bin-symlinks dir)
              ;; /usr/local/sbin/<tool> -> INFRA/<dir>/<tool>
              (let ((src (string-append infra "/" dir)))
                (if (directory-exists? src)
                    (map (lambda (tool)
                           `(,(string-append "/usr/local/sbin/" tool)
                             -> ,(string-append src "/" tool)))
                         (scandir src (lambda (f)
                                        (not (member f '("." ".."))))))
                    '())))

            (define directives
              `((directory "/tmp" 0 0 #o1777)
                (directory #$(%store-prefix) 0 0 #o755)
                (directory "/etc")
                (file "/etc/passwd"
                      ,(string-append
                        "root:x:0:0:root:/root:" (in-profile "/bin/bash") "\n"
                        #$user ":x:" (number->string uid) ":"
                        (number->string gid) ":" #$user ":" home ":" shell
                        "\n"))
                (file "/etc/group"
                      ,(string-append "root:x:0:\n"
                                      #$user ":x:" (number->string gid)
                                      ":\n"))
                (file "/etc/nsswitch.conf"
                      "passwd: files\ngroup:  files\nhosts:  files dns\n")
                ("/bin" -> ,(in-profile "/bin"))
                (directory "/usr/bin")
                ("/usr/bin/env" -> ,(in-profile "/bin/env"))
                ,@(map (match-lambda
                         ((path . target)
                          `(,path -> ,(string-append profile-dir "/" target))))
                       '#$symlinks)
                ;; Agent home
                (directory ,(string-append home "/.cache/oh-my-zsh"))
                (file ,(string-append home "/.zshrc")
                      ,(read-file #$(local-file "../../config/.zshrc" "zshrc")))
                (file ,(string-append home "/.tmux.conf")
                      ,(read-file #$(local-file "../../config/.tmux.conf"
                                               "tmux.conf")))
                (file ,(string-append home "/.gitignore_global")
                      ,(read-file #$(local-file "../../config/gitignore_global")))
                (file ,(string-append home "/.gitconfig.local")
                      ,(read-file #$gitconfig))
                ;; Writable mounts and volumes
                (directory "/workspace")
                (directory "/commandhistory")
                (file "/commandhistory/.bash_history" "")
                (file "/commandhistory/.zsh_history" "")
                (directory "/env/.claude")
                ;; Root-only infrastructure tools
                (directory "/usr/local/sbin" 0 0 #o750)
                ,@(bin-symlinks "bin")
                ,@(bin-symlinks "sbin")
                ,@'#$extra-directives))

            (setenv "PATH" (string-append #+(file-append tar "/bin") ":"
                                          #+(file-append gzip "/bin")))

            (build-docker-image #$output
                                paths
                                profile-dir
                                #:repository #$name
                                #:system %host-type
                                #:environment environment
                                #:entry-point (list entrypoint)
                                #:user (string-append (number->string uid) ":"
                                                      (number->string gid))
                                #:working-dir "/workspace"
                                #:extra-files directives
                                #:owners `((,home ,uid ,gid)
                                           ("/workspace" ,uid ,gid)
                                           ("/commandhistory" ,uid ,gid)
                                           ("/env" ,uid ,gid))
                                #:compressor '("gzip" "-9n")
                                #:creation-time (make-time time-utc 0 1)
                                #:max-layers #$max-layers)))))

  (computed-file (string-append name "-docker-image.tar.gz")
                 build
                 #:options
                 (list #:references-graphs
                       `(("profile" ,agent-profile)
                         ("infra" ,infra-profile)
                         ("entrypoint" ,entrypoint)))))
