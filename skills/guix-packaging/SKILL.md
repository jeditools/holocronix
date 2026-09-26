---
name: guix-packaging
description: Write, inspect and verify Guix package definitions and caves. Use when editing .scm package files, cave.scm or channels.scm, when asked what a Guix package needs, costs or depends on, or when a Guix build or evaluation fails. Ask the model with `jedi guix` before reading Scheme.
---

# Guix packaging

Guix package definitions are records with a schema, compiled to a
derivation that names every input by hash. Treat them as data you can query,
not as text to read. Most mistakes come from guessing what a record contains
or what a build system adds. Both are one command away.

## Ask before you read

`jedi guix` answers as JSON. Nothing is built.

| Question | Command |
|---|---|
| What is in this package's record? | `jedi guix show SPEC` |
| What does it declare as inputs? | `jedi guix inputs SPEC` |
| What does the build system add on top? | `jedi guix inputs SPEC --implicit` |
| What would a build of it do right now? | `jedi guix plan SPEC` |
| What is its derivation and are the outputs built? | `jedi guix derivation SPEC` |
| What does the built output reference at run time? | `jedi guix references SPEC` |
| What depends on this store item? | `jedi guix referrers ITEM` |
| How big is its closure? | `jedi guix size SPEC` |
| Show me the graph around it | `jedi guix graph SPEC -t bag --depth 1` |
| Is this definition clean? | `jedi guix lint SPEC` |
| What provides X? | `jedi guix search REGEXP` |
| Is this definition data or code? | `jedi guix classify SPEC` |

`SPEC` is `name` or `name@version`, or a Scheme expression in parentheses
such as `'(@ (gnu packages base) hello)'`. Add `--cave NAME` to ask the
Guix a cave is pinned to. Add `--system` or `--target` to ask about another
architecture. A failure comes back as `{"error": "..."}` with exit 1.

Inside a cave without `jedi`, run the script directly:

```sh
guix repl -L /path/to/holocronix/guix -- /path/to/holocronix/cli/query.scm show hello
```

## Authoring workflow

1. **Start from an importer, never from a blank record.**
   `guix import crate NAME`, `guix import pypi NAME`, `guix import npm-binary NAME`,
   `guix import git URL`. The importer fills the source, the hash, and the
   obvious inputs. Edit what it produced.
2. **Never type a hash.** Get it from the tool that fetched the bytes:
   `guix download URL` for tarballs, `guix download --git URL --commit REV`
   or `guix hash -rx CHECKOUT` for git. A wrong hash fails loudly at fetch
   time, so a fabricated one is only a wasted build.
3. **Check structure before semantics.** Run
   `python3 scripts/parencheck.py FILE.scm` from this skill's directory. It
   names the top-level form that is unbalanced. Guile's own error only says
   "unexpected end of input".
4. **Normalize and lint.** `guix style -f FILE.scm` for layout, then
   `jedi guix lint SPEC`. Fix every warning before building. Add `--network`
   once to check the home page and source URL.
5. **Plan before you build.** `jedi guix plan SPEC`. If `to-build` lists
   dozens of derivations you did not expect, an input changed a hash that
   everything downstream depends on. Look again before spending an hour.
6. **Build, then prove it reproduces.**
   `guix build SPEC` then `guix build SPEC --check --rounds=2`.
   `--check` rebuilds and diffs against what is in the store.
7. **Classify what you wrote.** `jedi guix classify SPEC`. Aim for
   `pure-record` or `custom-arguments`. `has-phases` means the definition
   contains imperative build code, which a reviewer has to read as code.
   If a phase only fixes a path or a flag, look for a build-system keyword
   that does the same thing declaratively.

## Rules that prevent the common failures

- **Gexp or quasiquote, not both.** Build-side code is a gexp: `#~`, `#$`,
  `#$@`. Host-side lists use quasiquote: `` ` ``, `,`, `,@`. Mixing them
  gives "unbound variable" errors for things that are clearly defined,
  because the wrong side is evaluating them. Modern definitions use gexps
  everywhere in `arguments`.
- **native-inputs run on the build machine, inputs run on the target.**
  Compilers, generators, test tools go in `native-inputs`. Libraries the
  result links against go in `inputs`. Getting this wrong only shows up when
  cross-compiling, which is exactly when `jedi guix inputs SPEC --implicit
  --target TRIPLET` shows the two lists side by side.
- **Implicit inputs are real inputs.** The gnu build system adds gcc, make,
  coreutils and more without you listing them. `--implicit` shows them.
  Do not add them again.
- **`inherit` copies every field, including `source` and `arguments`.**
  A package that inherits and changes the version still has the parent's
  hash until you replace `source`. Use `substitute-keyword-arguments` to
  change one argument and keep the rest.
- **One record per top-level form.** Long files fail in ways that point
  at the wrong line. Keep the definition you are editing as its own
  `define-public`, and check balance after every edit.
- **Store paths are the identity.** Two definitions that produce the same
  derivation are the same package. When in doubt whether an edit changed
  anything, compare `jedi guix derivation SPEC` before and after.

## Testing against a Guix checkout

When the change is to Guix itself or to a channel you develop:

```sh
guix shell -D guix --pure -- bash --norc --noprofile
GUILE_AUTO_COMPILE=0 ./pre-inst-env guix build -e '(@@ (gnu packages foo) name)' --dry-run
GUILE_AUTO_COMPILE=0 ./pre-inst-env guix build -e '(@@ (gnu packages foo) name)'
```

For a channel: `.guix-channel` names the directory, `channels.scm` pins
the dependencies, and `guix time-machine -C channels.scm -- build SPEC`
builds with exactly those commits. A holocronix cave's `channels.scm` is
that file, which is why `jedi guix --cave` answers for the cave's Guix.

Common evaluation errors and what they mean:

| Error | Cause |
|---|---|
| unexpected end of input | a form is missing `)`; run `parencheck.py` |
| unexpected `)` | one `)` too many |
| no code for module | module name does not match the directory layout |
| unbound variable, for something defined | gexp and quasiquote mixed, or a missing `#:use-module` |
| hash mismatch | the hash was typed, or the upstream file changed; refetch with `guix download` |

## Worked examples

**What provides a command, and what does it cost?**

```sh
jedi guix search '^fd$|fd-find'
jedi guix size fd
jedi guix plan fd
```
`size` gives the closure in bytes with the biggest contributors first.
`plan` says whether it downloads or builds.

**Add a package to a cave and see what it pulls in.**

```sh
jedi guix inputs ripgrep --implicit --cave my-cave
jedi guix graph ripgrep -t bag --depth 1 --cave my-cave
```
Then add the spec to `cave.scm` and run `jedi guix plan '(...)' --cave my-cave`
on the cave expression before `jedi build`.

**Why does this fail to cross-compile?**

```sh
jedi guix inputs foo --implicit --target aarch64-linux-gnu
```
Anything under `host-inputs` that is a compiler or generator belongs in
`native-inputs`. Anything under `build-inputs` that is a library the result
links against belongs in `inputs`.

**Package a crate.**

```sh
guix import crate NAME > name.scm         # then wrap in a module or a -f file
python3 scripts/parencheck.py name.scm
guix style -f name.scm
guix build -f name.scm --dry-run
jedi guix lint '(load "name.scm")'        # or the module form once it has one
jedi guix classify '(load "name.scm")'
```
For a cave, prefer the holocronix `cargo-vendor` module for Rust
dependencies: it turns `Cargo.lock` into declared, hashed inputs instead
of packaging each crate. See `guix/README.md`.

## When the tool is missing

If `jedi` is not available, the same answers come from Guix itself, less
conveniently: `guix show`, `guix graph --type=bag`, `guix size`,
`guix build --dry-run`, `guix lint`, `guix search`, and
`guix repl` for the record fields.
