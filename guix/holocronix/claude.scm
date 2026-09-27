;;; holocronix --- sandboxed containers for coding agents
;;;
;;; (holocronix claude): what Claude Code finds when it starts in a cave,
;;; its settings.json and its plugin seed directory, as file-like objects
;;; built from config/defaults.json and a set of pinned marketplaces.  The
;;; counterpart of the claudeSettings, knownMarketplaces and plugin-seed
;;; parts of lib/mkJediCave.nix.
;;;
;;; The seed is the documented way to ship plugins into a network-locked
;;; container: CLAUDE_CODE_PLUGIN_SEED_DIR names a read-only directory
;;;
;;;   known_marketplaces.json
;;;   marketplaces/<name>/...                       a marketplace checkout
;;;   cache/<marketplace>/<plugin>/<version>/...    a pre-installed plugin
;;;
;;; that Claude Code reads by layout; the `source' recorded in
;;; known_marketplaces.json is never fetched.  <name> must be the `name'
;;; field of that checkout's .claude-plugin/marketplace.json, not the
;;; repository name.  A pre-installed plugin stays inert until settings.json
;;; enables it, which `claude-settings-file' does for every plugin it is
;;; given.

(define-module (holocronix claude)
  #:use-module (guix gexp)
  #:use-module (guix records)
  #:use-module (guix packages)
  #:use-module (guix git-download)
  #:use-module ((guix git) #:select (url+commit->name))
  #:use-module (guix base32)
  #:use-module (gnu packages guile)
  #:export (marketplace
            marketplace?
            marketplace-name
            marketplace-repo
            marketplace-commit
            marketplace-hash
            marketplace-source
            %default-marketplaces
            %jedicave-defaults-file
            claude-plugin-seed
            claude-settings-file))


;;;
;;; Marketplaces.
;;;

(define-record-type* <marketplace>
  marketplace make-marketplace
  marketplace?
  (name    marketplace-name)     ;`name' in the checkout's marketplace.json
  (repo    marketplace-repo)     ;GitHub "owner/repo"
  (commit  marketplace-commit)   ;full commit id
  (hash    marketplace-hash))    ;nar sha256 of the checkout, as `guix hash -rx'

(define (marketplace-url m)
  (string-append "https://github.com/" (marketplace-repo m)))

(define (marketplace-source m)
  "Return an origin for M's pinned checkout.  It is named as `guix download
--git' names its result, so `guix download --git --commit=COMMIT URL' both
prints the hash to record here and leaves the checkout the build will use."
  (let ((url    (marketplace-url m))
        (commit (marketplace-commit m)))
    (origin
      (method git-fetch)
      (uri (git-reference (url url) (commit commit)))
      (file-name (url+commit->name url commit))
      (sha256 (nix-base32-string->bytevector (marketplace-hash m))))))

(define %default-marketplaces
  ;; The four repositories flake.nix takes as inputs, at the commits in
  ;; flake.lock on 2026-09-27; the hashes are of the trees Nix fetched for
  ;; those commits.  Move commit and hash together.
  (list (marketplace
         (name "anthropic-agent-skills")
         (repo "anthropics/skills")
         (commit "d230a6dd6eb1a0dbee9fec55e2f00a96e28dff81")
         (hash "09hiamg6w8qx43nv06dql07rc07bj66yvq5j9ssav7anslpahv78"))
        (marketplace
         (name "trailofbits")
         (repo "trailofbits/skills")
         (commit "870955f1af03acfec736f77c287219bc01af11e9")
         (hash "0xbv87p5n8hqicxvwf2vdxsfpn5pdg0810pm31pc9l47542jpxj9"))
        (marketplace
         (name "skills-curated")
         (repo "trailofbits/skills-curated")
         (commit "022fa0948818c9f2f738a428f4546cc65c427767")
         (hash "0kyvbvgar6g7zfdqn3apl6h14ggm1sd8jnf55w9b1l66l6ia53x4"))
        (marketplace
         (name "claude-plugins-official")
         (repo "anthropics/claude-plugins-official")
         (commit "ac45fdae4b7af187b5624599ab054090dedadd94")
         (hash "1dfxc9rpwlxb1n7ycg99ckb5924y57xvxs3pdbxlxw1lv43wlj34"))))


;;;
;;; Settings and seed.
;;;

(define %jedicave-defaults-file
  ;; Shared with the Nix builder: {"claudeSettings": {...}, "plugins": [...]}.
  (local-file "../../config/defaults.json"))

(define* (claude-settings-file #:key
                               (plugins '())
                               (defaults %jedicave-defaults-file))
  "Return a file-like object: Claude Code's settings.json, the
\"claudeSettings\" object of DEFAULTS with every plugin listed there and in
PLUGINS (strings \"name@marketplace\") enabled."
  (computed-file
   "claude-settings.json"
   (with-extensions (list guile-json-4)
     #~(begin
         (use-modules (json)
                      (srfi srfi-1))

         (define defaults (call-with-input-file #$defaults json->scm))
         (define settings (or (assoc-ref defaults "claudeSettings") '()))
         (define plugins
           (delete-duplicates
            (append (vector->list (or (assoc-ref defaults "plugins") #()))
                    '#$plugins)))
         (define enabled
           ;; A JSON object has no duplicate keys: drop what is re-added.
           (append (remove (lambda (entry) (member (car entry) plugins))
                           (or (assoc-ref settings "enabledPlugins") '()))
                   (map (lambda (plugin) (cons plugin #t)) plugins)))

         (call-with-output-file #$output
           (lambda (port)
             (scm->json (cons (cons "enabledPlugins" enabled)
                              (alist-delete "enabledPlugins" settings))
                        port #:pretty #t)))))))

(define* (claude-plugin-seed #:key
                             (marketplaces %default-marketplaces)
                             (plugins '())
                             (defaults %jedicave-defaults-file))
  "Return a file-like object: the plugin seed directory holding
MARKETPLACES, with the plugins listed in DEFAULTS and in PLUGINS (strings
\"name@marketplace\") pre-installed under cache/.  Each plugin is looked up
in its marketplace's checkout."
  (define sources
    ;; (name repo checkout); the checkout lowers to its store path.
    (map (lambda (m)
           (list (marketplace-name m)
                 (marketplace-repo m)
                 (marketplace-source m)))
         marketplaces))

  (computed-file
   "claude-plugin-seed"
   (with-extensions (list guile-json-4)
     (with-imported-modules '((guix build utils))
       #~(begin
           (use-modules (guix build utils)
                        (json)
                        (ice-9 match)
                        (srfi srfi-1))

           (define seed #$output)
           (define marketplaces '#$sources)

           (define (json-file file)
             (call-with-input-file file json->scm))

           (define (marketplace-checkout name)
             (match (assoc name marketplaces)
               ((_ _ checkout) checkout)
               (#f (error "plugin names a marketplace not in the seed:"
                          name))))

           (define (plugin-directory name market)
             ;; Where the marketplace's manifest says the plugin is, or
             ;; failing that the conventional plugins/ and external_plugins/.
             (let* ((checkout (marketplace-checkout market))
                    (manifest (json-file (string-append
                                          checkout
                                          "/.claude-plugin/marketplace.json")))
                    (entry (find (lambda (plugin)
                                   (equal? (assoc-ref plugin "name") name))
                                 (vector->list
                                  (or (assoc-ref manifest "plugins") #()))))
                    (source (and entry (assoc-ref entry "source")))
                    (candidates
                     (append (if (string? source)
                                 (list (string-append checkout "/" source))
                                 '())
                             (list (string-append checkout "/plugins/" name)
                                   (string-append checkout
                                                  "/external_plugins/"
                                                  name)))))
               (or (find directory-exists? candidates)
                   (error "plugin not found in marketplace:" name market))))

           (define (plugin-version dir)
             (or (assoc-ref (json-file (string-append
                                        dir "/.claude-plugin/plugin.json"))
                            "version")
                 (error "plugin has no version:" dir)))

           (define plugins
             (delete-duplicates
              (append (vector->list
                       (or (assoc-ref (json-file #$defaults) "plugins") #()))
                      '#$plugins)))

           (mkdir-p (string-append seed "/marketplaces"))
           (mkdir-p (string-append seed "/cache"))

           ;; Registers each marketplace.  Claude Code reads the checkout at
           ;; marketplaces/<name>/ and never fetches `source', but the
           ;; schema wants one.
           (call-with-output-file
               (string-append seed "/known_marketplaces.json")
             (lambda (port)
               (scm->json
                (map (match-lambda
                       ((name repo _)
                        `(,name . (("source" . (("source" . "github")
                                                ("repo" . ,repo)))))))
                     marketplaces)
                port #:pretty #t)))

           (for-each (match-lambda
                       ((name _ checkout)
                        (copy-recursively
                         checkout (string-append seed "/marketplaces/" name))))
                     marketplaces)

           (for-each
            (lambda (spec)
              (match (string-split spec #\@)
                ((name market)
                 (let ((dir (plugin-directory name market)))
                   (copy-recursively
                    dir (string-append seed "/cache/" market "/" name "/"
                                       (plugin-version dir)))))
                (_ (error "plugin spec is not name@marketplace:" spec))))
            plugins))))))
