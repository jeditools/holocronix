# Vision: a build substrate for coding agents

Status: draft, 2026-09-23. Written after a debate about OCI, Guix and
[stagex](https://codeberg.org/stagex/stagex) as the base for agent sandboxes.
The stagex whitepaper is at
<https://codeberg.org/stagex/whitepapers/src/branch/main/out/stagex.pdf>.

## The vision in four sentences

1. The derivation is the identity of a build.
2. OCI is the output contract.
3. Guix is the first front-end. The Nix backend stays.
4. Agents author and reproduce. Humans sign.

Everything else in this document is judged against those four sentences.

## What stays

- **The Nix backend and its cache.** Same model, more packages, and a
  second producer of the same OCI image keeps the contract honest.
- **OCI as the only thing a runtime sees.** Docker today, Gondolin or
  another runtime later, without touching the baking layer.
- **The bare-repo handoff.** Seed, harvest, fetch. The agent never touches
  the host's git directory, and everything it produced arrives as commits
  the host inspects before merging.
- **The policy layer.** Firewall, proxy, secrets, seccomp. Independent of
  how the image was built.

## Why the derivation model

The audience of a build recipe is shifting from human developers to coding
agents, and agents can be pointed at any format. Once that is true, the
arguments for Dockerfiles that rest on audience size and ecosystem breadth
stop mattering. What remains is the question of which substrate lets an
agent work most accurately and precisely, and lets anyone tell whether it
did.

| Property | Guix model | Plain Dockerfile | stagex Containerfile |
|---|---|---|---|
| Recipe to artifact is deterministic | by construction | no | by discipline and lint |
| All inputs declared, no ambient network | yes, except fixed-output fetches | no | yes, per RUN line |
| Many failures surface at evaluation, before any build | yes | no, mid-RUN | no, mid-RUN |
| Model is queryable without rebuilding | yes | no | by grep only |
| A build has an identity before it runs | derivation path | none | none |
| Blast radius of a change is computable | yes, from the graph | no, cache is line order | partly, impact tool |
| Runtime closure is derivable from the artifact | yes | no | no |
| Value with scale | superlinear, shared store nodes | linear, duplicated layers | linear |
| Imperative escape hatch | staged, sandboxed, sees only declared inputs | unrestricted | network off, otherwise unrestricted |
| Token-level generation ergonomics | worse, fixable at the edge | better | better |
| Corpus quality for learning | uniform style, labelled reviews | heterogeneous | small, uniform |

The scale row is the one the agent framing adds. More agents on a store
model means more shared, deduplicated, cross-checkable nodes. More agents on
Dockerfiles means more layers nobody can relate to each other.

Two corrections keep the argument honest.

**The audience does not fade, it changes role.** Agents amplify whatever
they were trained on. Generations drift toward the substrate that yields
verifiable reward, because that is where training signal comes from.
Reproducibility, lint and the derivation are such signals. And the audit
never shrinks: in an agent-majority world, verifying what an agent produced
matters more, since agents are also the attack vector. A single formally
specified build model with a canonical intermediate form is easier to verify
than several builders that are hoped to agree.

**The thing that wins is not Scheme.** It is the store and derivation model.
Scheme and Nix are front-ends. The ideal front-end for agents has less
imperative escape than Guix has today: package phases are ordinary
programming, and that is where precision is lost. The design direction is to
push hacks into build systems and keep per-package records as pure data.

## What stagex teaches

Borrow:

- Reproducibility as a hard gate. Non-reproducible packages are rejected, not
  flagged.
- Two independent reproductions before an artifact is signed, with signatures
  committed to a repo.
- Hermetic recipes: pinned source hashes, network off during builds, fixed
  source date, rewritten timestamps in the output.

Do not borrow, for now:

- Containerfiles as the substrate. The dependency graph exists only as
  `COPY --from` lines, there is no graph tool, dependencies are build-time
  only, and runtime closures are assembled by hand in pallets.
- The multi-arch story. stagex ships x86_64 only. Guix ships aarch64 with
  substitutes today, which matters for Apple-silicon users.
- The "diverse OCI toolchains" claim. Only Docker with the containerd store
  reproduces stagex today, and rootless builds change digests.

Possible later: stagex images as a verified-base flavor, once they ship
arm64 and a second builder reproduces them.

## Plan, in order of leverage

1. **Write the contract down.** This document, kept short. Every proposal,
   including a stagex backend, gets judged against the four sentences.

2. **Expose the model to agents.** A query tool over `guix repl` that returns
   JSON: package record, explicit and implicit inputs via the bag, derivation
   path, references and referrers, closure size, a graph slice, lint results,
   dry-run build. Ship it as `jedi` subcommands first, as an MCP server inside
   the cave second. Promote the Guix notes in CLAUDE.md into a skill with
   worked examples. Retrieval over the package tree and the guix-patches
   archive comes after the tool.

3. **Constrain authoring.** Agents start from importers for crates, PyPI, npm
   and git, then edit fields. Validate structurally with the Guile reader,
   normalize with `guix style`, gate on `guix lint`. Classify every definition
   as pure-record or has-phases and require reviewer approval for the second
   class. `cargo-vendor.scm` is the pattern: an escape hatch turned into
   declared inputs. Finish the git-dependency half on a real project.

4. **Make verification the loop and the metric.** For every agent-produced
   package or cave, record: evaluates, lints, builds, reproduces across two
   rounds, later across two machines with `guix challenge`. Extend
   `jedi harvest` so the bundle carries derivation paths and output hashes
   alongside commits, so the host can re-verify without trusting the cave.

5. **Turn the audit cave into a quorum.** A second cave rebuilds the harvested
   derivations, compares output hashes, diffs the closure against the previous
   build, runs lint, and only then does the host fetch. This is stagex's
   two-maintainer rule with agents doing the rebuilding and a human doing the
   signing. Sign OCI image digests and keep signatures in the cave repo.

6. **Shrink the escape hatch over time.** Track the has-phases ratio. When the
   same hack recurs, move it into a build system or a channel helper, and send
   it upstream through guix-patches. That grows the labelled corpus and gets
   human review, which is the flywheel the corpus argument depends on.

7. **Later.** aarch64 caves through Guix. stagex verified bases. Fine-tune an
   open model on the Guix corpus only if retrieval plus the query tool proves
   insufficient.

## Metrics

| Metric | Why it matters |
|---|---|
| Reproducibility rate of agent builds | The trust signal, and the future reward signal |
| Lint pass rate on first submission | Authoring precision |
| Has-phases ratio | Distance of the substrate from pure data |
| Time to first failure | Whether errors surface at evaluation or mid-build |
| Closure delta per change | Blast radius, what the audit cave reads first |

## Non-goals

- A new DSL. The package record type and the derivation are the schema.
- Dropping the Nix backend. Same model, bigger cache, keeps the OCI contract
  honest.
- Waiting for frontier models to learn Scheme. Steps 2 and 3 remove the need.
- Cross-builder diversity. One specified builder with a canonical
  intermediate form beats several that are hoped to agree.

## Open questions

- Where the query tool lives first: `jedi` on the host, or an MCP server
  inside the cave.
- The on-disk format of verification records in a cave.
- Whether the audit cave is Guix-only or also covers Nix caves.
- cargo-vendor git dependencies on a real project such as xous-core.
