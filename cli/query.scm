;;; query.scm --- JSON answers about the Guix package model, for agents.
;;;
;;; Usage:  guix repl [-L DIR]... -- cli/query.scm OP [ARG...] [--option[=value]]...
;;;
;;; This is a script, not a module, so it must stay out of guix/: that
;;; directory is the holocronix channel, and Guix loads every .scm file in a
;;; channel before compiling it.  Loaded that way, the call to `main' at the
;;; bottom runs with the builder's command line, fails, and takes the whole
;;; channel build down with it.
;;;
;;; Every invocation prints exactly one JSON object on stdout and exits 0, or
;;; prints {"error": "..."} on stdout and exits 1.  Nothing is ever built.
;;; The point is to let an agent ask the model instead of reading Scheme:
;;; a package record, its explicit and implicit inputs, its derivation, what
;;; a build would do, what is in a closure, a graph slice, lint warnings.
;;;
;;; Ops:
;;;   show SPEC                    the package record
;;;   inputs SPEC [--implicit]     explicit inputs, or the bag's build, host
;;;                                and target inputs (implicit ones included)
;;;   derivation SPEC              derivation path, output paths, built or not
;;;   plan SPEC [--no-substitutes] [--no-grafts]
;;;                                what a build would build or download
;;;   references ITEM              run-time references of a built item
;;;   referrers ITEM               what in the store refers to a built item
;;;   size ITEM...                 closure sizes, from the store or substitutes
;;;   graph SPEC [--type=T] [--depth=N]
;;;                                nodes and edges; T as `guix graph --type`
;;;   lint SPEC [--network] [--checkers=a,b]
;;;                                lint warnings, local checkers by default
;;;   search REGEXP [--limit=N]    packages whose name, synopsis or
;;;                                description match
;;;   classify SPEC                pure-record, custom-arguments or has-phases
;;;
;;; SPEC is a package spec as `guix build` takes it ("hello", "hello@2.12")
;;; or a Scheme expression in parentheses, "(@ (gnu packages base) hello)".
;;; ITEM is a SPEC or a /gnu/store path.  --system=S and --target=T apply
;;; wherever they make sense.

(use-modules (json)
             (gnu packages)
             (guix packages)
             (guix derivations)
             (guix store)
             (guix monads)
             (guix graph)
             (guix scripts graph)
             (guix scripts size)
             (guix lint)
             (guix diagnostics)
             (guix licenses)
             (guix build-system)
             (guix base32)
             (guix git-download)
             (guix utils)
             (ice-9 format)             ; `fail' uses ~{ ~}, which core format lacks
             (ice-9 match)
             (ice-9 regex)
             (ice-9 pretty-print)
             (ice-9 receive)
             (ice-9 exceptions)
             (srfi srfi-1)
             (srfi srfi-11)
             (srfi srfi-26)
             (srfi srfi-34)
             (srfi srfi-35))


;;;
;;; Plumbing.
;;;

(define (fail fmt . args)
  (raise (condition (&message (message (apply format #f fmt args))))))

(define (exception->string c)
  ;; Guile's own messages are format strings for their irritants
  ;; ("Unbound variable: ~S"); ours are plain text.
  (cond ((and (exception? c) (exception-with-message? c))
         (let ((message (exception-message c))
               (irritants (if (exception-with-irritants? c)
                              (exception-irritants c)
                              '())))
           (cond ((null? irritants) message)
                 ((false-if-exception
                   (apply format #f message irritants)))
                 (else (format #f "~a: ~{~s~^ ~}" message irritants)))))
        ((message-condition? c) (condition-message c))
        (else (format #f "~a" c))))

(define (emit alist)
  (display (scm->json-string alist #:pretty #t))
  (newline))

;; JSON helpers.  guile-json maps alists with string keys to objects and
;; vectors to arrays.  Lists are ambiguous, so arrays are always vectors.
(define (obj . pairs) pairs)
(define (arr lst) (list->vector lst))
(define (->string x)
  (cond ((string? x) x)
        ((symbol? x) (symbol->string x))
        ((keyword? x) (symbol->string (keyword->symbol x)))
        (else (format #f "~a" x))))
(define (or-null x) (if x x 'null))

;; Options are --name, --name=value, or --name value for the names below.
(define %valued-options
  '("system" "target" "type" "depth" "checkers" "limit"))

(define (parse-args args)
  "Return two values: positional ARGS and an alist of options."
  (let loop ((args args) (positional '()) (options '()))
    (match args
      (()
       (values (reverse positional) options))
      (((? (cut string-prefix? "--" <>) opt) . rest)
       (let* ((body (substring opt 2))
              (idx (string-index body #\=)))
         (cond (idx
                (loop rest positional
                      (alist-cons (substring body 0 idx)
                                  (substring body (+ idx 1))
                                  options)))
               ((and (member body %valued-options) (pair? rest))
                (loop (cdr rest) positional
                      (alist-cons body (car rest) options)))
               (else
                (loop rest positional (alist-cons body #t options))))))
      ((arg . rest)
       (loop rest (cons arg positional) options)))))

(define* (option options name #:optional default)
  (match (assoc name options)
    ((_ . value) value)
    (#f default)))

(define (option-number options name default)
  (let ((value (option options name)))
    (if value
        (or (string->number value)
            (fail "--~a: not a number: ~a" name value))
        default)))


;;;
;;; Resolving arguments.
;;;

(define (resolve-package spec)
  "Return the package SPEC denotes: a spec as `guix build` takes it, or a
Scheme expression in parentheses."
  (if (string-prefix? "(" spec)
      (let ((value (primitive-eval (call-with-input-string spec read))))
        (unless (package? value)
          (fail "expression does not evaluate to a package: ~a" spec))
        value)
      (let-values (((name version) (package-name->name+version spec #\@)))
        (match (find-packages-by-name name version)
          ((package . _) package)
          (() (fail "package not found: ~a" spec))))))

(define (package->derivation* store package system target)
  (if target
      (package-cross-derivation store package target system)
      (package-derivation store package system)))

(define (resolve-items store spec system target)
  "Return the store items SPEC denotes: itself when it is a store path, else
the output paths of the package it names."
  (if (store-path? spec)
      (list spec)
      (map cdr (derivation->output-paths
                (package->derivation* store (resolve-package spec)
                                      system target)))))


;;;
;;; Rendering records.
;;;

(define (location->json loc)
  (if loc
      (obj `("file" . ,(location-file loc))
           `("line" . ,(location-line loc))
           `("column" . ,(location-column loc)))
      'null))

(define (package-brief package)
  (obj `("name" . ,(package-name package))
       `("version" . ,(package-version package))
       `("synopsis" . ,(or-null (package-synopsis package)))
       `("location" . ,(location->json (package-location package)))))

(define (thing->json thing)
  (cond ((package? thing)
         (obj `("kind" . "package")
              `("name" . ,(package-name thing))
              `("version" . ,(package-version thing))))
        ((origin? thing)
         (obj `("kind" . "origin")
              `("name" . ,(->string (origin-actual-file-name thing)))
              `("version" . null)))
        ((string? thing)
         (obj `("kind" . "file")
              `("name" . ,thing)
              `("version" . null)))
        (else
         (obj `("kind" . "file-like")
              `("name" . ,(->string thing))
              `("version" . null)))))

(define (input->json input)
  (match input
    ((label thing . outputs)
     (append (obj `("label" . ,label))
             (thing->json thing)
             (obj `("output" . ,(match outputs
                                  ((output) output)
                                  (() "out")
                                  (_ (->string outputs)))))))
    (_
     (obj `("label" . ,(->string input))
          `("kind" . "unknown")))))

(define (inputs->json inputs)
  (arr (map input->json inputs)))

(define (uri->json uri)
  (cond ((string? uri) uri)
        ((and (list? uri) (every string? uri)) (arr uri))
        ((git-reference? uri)
         (obj `("url" . ,(git-reference-url uri))
              `("commit" . ,(git-reference-commit uri))
              `("recursive" . ,(git-reference-recursive? uri))))
        (else (->string uri))))

(define (origin->json origin)
  (let ((hash (origin-hash origin))
        (method (origin-method origin)))
    (obj `("method" . ,(->string (or (and (procedure? method)
                                          (procedure-name method))
                                     "unknown")))
         `("uri" . ,(uri->json (origin-uri origin)))
         `("hash-algorithm" . ,(if hash
                                   (->string (content-hash-algorithm hash))
                                   'null))
         `("hash" . ,(if hash
                         (bytevector->nix-base32-string
                          (content-hash-value hash))
                         'null))
         `("file-name" . ,(or-null (origin-file-name origin)))
         `("patches" . ,(length (origin-patches origin)))
         `("snippet" . ,(and (origin-snippet origin) #t)))))

(define (source->json source)
  (cond ((origin? source) (origin->json source))
        ((not source) 'null)
        (else (obj `("kind" . "file-like")
                   `("value" . ,(->string source))))))

(define (license->json license)
  (cond ((license? license) (license-name license))
        ((list? license) (arr (map license->json license)))
        ((not license) 'null)
        (else (->string license))))

(define (argument-keywords package)
  (filter keyword? (package-arguments package)))

(define (arguments->string package)
  (with-output-to-string
    (lambda ()
      (pretty-print (package-arguments package)))))

(define (package->json package)
  (append
   (package-brief package)
   (obj `("description" . ,(or-null (package-description package)))
        `("home-page" . ,(or-null (package-home-page package)))
        `("license" . ,(license->json (package-license package)))
        `("build-system" . ,(->string (build-system-name
                                       (package-build-system package))))
        `("outputs" . ,(arr (package-outputs package)))
        `("supported-systems" . ,(arr (package-supported-systems package)))
        `("source" . ,(source->json (package-source package)))
        `("inputs" . ,(inputs->json (package-inputs package)))
        `("native-inputs" . ,(inputs->json (package-native-inputs package)))
        `("propagated-inputs" . ,(inputs->json
                                  (package-propagated-inputs package)))
        `("argument-keywords" . ,(arr (map ->string
                                           (argument-keywords package))))
        `("has-phases" . ,(and (memq #:phases (argument-keywords package)) #t))
        `("arguments" . ,(arguments->string package))
        `("has-replacement" . ,(and (package-replacement package) #t)))))


;;;
;;; Ops.
;;;

(define (op-show positional options)
  (match positional
    ((spec) (package->json (resolve-package spec)))
    (_ (fail "usage: show SPEC"))))

(define (op-inputs positional options)
  (match positional
    ((spec)
     (let ((package (resolve-package spec))
           (system (option options "system" (%current-system)))
           (target (option options "target" #f)))
       (if (option options "implicit")
           (let ((bag (package->bag package system target)))
             (obj `("name" . ,(bag-name bag))
                  `("system" . ,(bag-system bag))
                  `("target" . ,(or-null (bag-target bag)))
                  `("implicit" . #t)
                  `("build-inputs" . ,(inputs->json (bag-build-inputs bag)))
                  `("host-inputs" . ,(inputs->json (bag-host-inputs bag)))
                  `("target-inputs" . ,(inputs->json
                                        (bag-target-inputs bag)))))
           (obj `("name" . ,(package-name package))
                `("version" . ,(package-version package))
                `("implicit" . #f)
                `("inputs" . ,(inputs->json (package-inputs package)))
                `("native-inputs" . ,(inputs->json
                                      (package-native-inputs package)))
                `("propagated-inputs" . ,(inputs->json
                                          (package-propagated-inputs
                                           package)))))))
    (_ (fail "usage: inputs SPEC [--implicit] [--system=S] [--target=T]"))))

(define (with-grafts options thunk)
  (parameterize ((%graft? (not (option options "no-grafts"))))
    (thunk)))

(define (op-derivation positional options)
  (match positional
    ((spec)
     (let ((package (resolve-package spec))
           (system (option options "system" (%current-system)))
           (target (option options "target" #f)))
       (with-store store
         (with-grafts options
           (lambda ()
             (let ((drv (package->derivation* store package system target)))
               (obj `("name" . ,(package-name package))
                    `("version" . ,(package-version package))
                    `("system" . ,system)
                    `("target" . ,(or-null target))
                    `("grafts" . ,(%graft?))
                    `("derivation" . ,(derivation-file-name drv))
                    `("outputs" . ,(arr (map (match-lambda
                                               ((name . path)
                                                (obj `("name" . ,name)
                                                     `("path" . ,path)
                                                     `("built" . ,(valid-path?
                                                                   store path)))))
                                             (derivation->output-paths
                                              drv)))))))))))
    (_ (fail "usage: derivation SPEC [--system=S] [--target=T] [--no-grafts]"))))

(define (op-plan positional options)
  (match positional
    ((spec)
     (let ((package (resolve-package spec))
           (system (option options "system" (%current-system)))
           (target (option options "target" #f))
           (substitutes? (not (option options "no-substitutes"))))
       (with-store store
         (with-grafts options
           (lambda ()
             (let* ((drv (package->derivation* store package system target))
                    (inputs (list (derivation-input drv)))
                    (oracle (if substitutes?
                                (substitution-oracle store inputs)
                                (const #f))))
               (receive (build download)
                   (derivation-build-plan store inputs
                                          #:substitutable-info oracle)
                 (obj `("derivation" . ,(derivation-file-name drv))
                      `("substitutes" . ,substitutes?)
                      `("grafts" . ,(%graft?))
                      `("to-build" . ,(arr (map derivation-file-name build)))
                      `("to-download"
                        . ,(arr (map (lambda (s)
                                       (obj `("item" . ,(substitutable-path s))
                                            `("nar-size"
                                              . ,(substitutable-nar-size s))
                                            `("download-size"
                                              . ,(substitutable-download-size
                                                  s))))
                                     download)))
                      `("nothing-to-do" . ,(and (null? build)
                                                (null? download)))))))))))
    (_ (fail "usage: plan SPEC [--no-substitutes] [--no-grafts] [--system=S] [--target=T]"))))

(define (op-references* positional options proc key)
  (match positional
    ((spec)
     (with-store store
       (let* ((system (option options "system" (%current-system)))
              (target (option options "target" #f))
              (items (resolve-items store spec system target))
              (valid (filter (cut valid-path? store <>) items)))
         (when (null? valid)
           (fail "not in the store: ~{~a~^, ~}; run `plan` and build first"
                 items))
         (obj `("items"
                . ,(arr (map (lambda (item)
                               (obj `("item" . ,item)
                                    `(,key . ,(arr (proc store item)))))
                             valid)))))))
    (_ (fail "usage: ~a ITEM" key))))

(define (op-references positional options)
  (op-references* positional options references "references"))

(define (op-referrers positional options)
  (op-references* positional options
                  (lambda (store item)
                    (remove derivation-path? (referrers store item)))
                  "referrers"))

(define (op-size positional options)
  (when (null? positional)
    (fail "usage: size ITEM..."))
  (with-store store
    (let* ((system (option options "system" (%current-system)))
           (target (option options "target" #f))
           (items (append-map (cut resolve-items store <> system target)
                              positional))
           (profiles (run-with-store store (store-profile items)))
           (total (reduce + 0 (map profile-self-size profiles))))
      (obj `("items" . ,(arr items))
           `("total" . ,total)
           `("closure"
             . ,(arr (map (lambda (profile)
                            (obj `("item" . ,(profile-file profile))
                                 `("self" . ,(profile-self-size profile))
                                 `("closure" . ,(profile-closure-size
                                                 profile))))
                          (sort profiles
                                (lambda (a b)
                                  (> (profile-closure-size a)
                                     (profile-closure-size b)))))))))))

(define (lookup-node-type* name)
  (or (find (lambda (type) (string=? (node-type-name type) name))
            %node-types)
      (fail "unknown graph type: ~a; one of ~{~a~^, ~}" name
            (map node-type-name %node-types))))

(define (op-graph positional options)
  (match positional
    ((spec)
     (let* ((type (lookup-node-type* (option options "type" "package")))
            (depth (option-number options "depth" +inf.0))
            (system (option options "system" (%current-system)))
            (target (option options "target" #f))
            (store-items? (member (node-type-name type)
                                  '("references" "referrers")))
            (sink (if (and store-items? (store-path? spec))
                      spec
                      (resolve-package spec)))
            (nodes '())
            (edges '()))
       (define backend
         (graph-backend "json" "JSON nodes and edges"
                        (lambda (name port) *unspecified*)
                        (lambda (port) *unspecified*)
                        (lambda (id label port)
                          (set! nodes (cons (obj `("id" . ,(->string id))
                                                 `("label" . ,label))
                                            nodes)))
                        (lambda (from to port)
                          (set! edges (cons (obj `("from" . ,(->string from))
                                                 `("to" . ,(->string to)))
                                            edges)))))
       (define (export store)
         (run-with-store store
           (mlet %store-monad ((sinks ((node-type-convert type) sink)))
             (export-graph sinks (%make-void-port "w")
                           #:node-type type
                           #:backend backend
                           #:max-depth depth))
           #:system system
           #:target target))
       (if (string=? (node-type-name type) "package")
           (export #f)                      ;no store needed
           (with-store store (export store)))
       (obj `("type" . ,(node-type-name type))
            `("depth" . ,(if (= depth +inf.0) 'null depth))
            `("nodes" . ,(arr (reverse nodes)))
            `("edges" . ,(arr (reverse edges))))))
    (_ (fail "usage: graph SPEC [--type=T] [--depth=N]"))))

(define (op-lint positional options)
  (match positional
    ((spec)
     (let* ((package (resolve-package spec))
            (available (if (option options "network")
                           %all-checkers
                           %local-checkers))
            (wanted (and=> (option options "checkers")
                           (cut string-split <> #\,)))
            (checkers (if wanted
                          (filter (lambda (checker)
                                    (member (->string (lint-checker-name
                                                       checker))
                                            wanted))
                                  available)
                          available)))
       (define (run checker store)
         (let ((name (->string (lint-checker-name checker))))
           (guard (c (#t (list (obj `("checker" . ,name)
                                    `("error" . ,(exception->string c))))))
             (map (lambda (warning)
                    (obj `("checker" . ,name)
                         `("message" . ,(lint-warning-message warning))
                         `("location" . ,(location->json
                                          (lint-warning-location warning)))))
                  (if (lint-checker-requires-store? checker)
                      ((lint-checker-check checker) package #:store store)
                      ((lint-checker-check checker) package))))))
       (define (run-all store)
         (append-map (cut run <> store) checkers))
       (let ((warnings (if (any lint-checker-requires-store? checkers)
                           (with-store store (run-all store))
                           (run-all #f))))
         (obj `("name" . ,(package-name package))
              `("version" . ,(package-version package))
              `("checkers" . ,(arr (map (compose ->string lint-checker-name)
                                        checkers)))
              `("warnings" . ,(arr warnings))))))
    (_ (fail "usage: lint SPEC [--network] [--checkers=a,b]"))))

(define (op-search positional options)
  (match positional
    ((pattern)
     (let* ((rx (make-regexp pattern regexp/icase))
            (limit (option-number options "limit" 50))
            (matches? (lambda (package)
                        (or (regexp-exec rx (package-name package))
                            (and=> (package-synopsis package)
                                   (cut regexp-exec rx <>))
                            (and=> (package-description package)
                                   (cut regexp-exec rx <>)))))
            (hits (sort (fold-packages (lambda (package result)
                                         (if (matches? package)
                                             (cons package result)
                                             result))
                                       '())
                        (lambda (a b)
                          (string<? (package-name a) (package-name b)))))
            (shown (if (< limit (length hits)) (take hits limit) hits)))
       (obj `("pattern" . ,pattern)
            `("count" . ,(length hits))
            `("shown" . ,(length shown))
            `("packages" . ,(arr (map package-brief shown))))))
    (_ (fail "usage: search REGEXP [--limit=N]"))))

(define (op-classify positional options)
  (match positional
    ((spec)
     (let* ((package (resolve-package spec))
            (keys (argument-keywords package))
            (phases? (and (memq #:phases keys) #t))
            (source (package-source package))
            (snippet? (and (origin? source) (origin-snippet source) #t))
            (patches (if (origin? source) (length (origin-patches source)) 0)))
       (obj `("name" . ,(package-name package))
            `("version" . ,(package-version package))
            `("kind" . ,(cond (phases? "has-phases")
                              ((pair? keys) "custom-arguments")
                              (else "pure-record")))
            `("argument-keywords" . ,(arr (map ->string keys)))
            `("has-phases" . ,phases?)
            `("has-snippet" . ,snippet?)
            `("patches" . ,patches)
            `("has-replacement" . ,(and (package-replacement package) #t))
            `("build-system" . ,(->string (build-system-name
                                           (package-build-system package)))))))
    (_ (fail "usage: classify SPEC"))))

(define %ops
  `(("show" . ,op-show)
    ("inputs" . ,op-inputs)
    ("derivation" . ,op-derivation)
    ("plan" . ,op-plan)
    ("references" . ,op-references)
    ("referrers" . ,op-referrers)
    ("size" . ,op-size)
    ("graph" . ,op-graph)
    ("lint" . ,op-lint)
    ("search" . ,op-search)
    ("classify" . ,op-classify)))


;;;
;;; Entry point.
;;;

(define (main args)
  (guard (c (#t (emit (obj `("error" . ,(exception->string c))))
                (exit 1)))
    (match args
      ((op . rest)
       (match (assoc op %ops)
         ((_ . proc)
          (receive (positional options) (parse-args rest)
            (emit (proc positional options))))
         (#f (fail "unknown op: ~a; one of ~{~a~^, ~}" op (map car %ops)))))
      (()
       (fail "usage: query.scm OP [ARG...]; ops: ~{~a~^, ~}" (map car %ops))))))

;; Under `guix repl -- FILE ARGS...`, (command-line) is (FILE ARGS...).
(main (cdr (command-line)))
