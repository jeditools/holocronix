# Security Model and Limitations

This document describes what the jedicave isolates, what it does not, and where gaps remain. The goal is to help users make informed decisions about their threat model rather than assume the container is a complete sandbox.

## At a glance

| Layer | Status | Notes |
|-------|--------|-------|
| Filesystem | Isolated | Host inaccessible except explicit mounts |
| Processes | Isolated | PID/mount/UTS/IPC namespaces |
| Privileges | Hardened | No sudo, no setuid; firewall rules immutable from container user |
| NPM scripts | Hardened | Disabled by default (`IGNORE_SCRIPTS=true`, 24h release age gate) |
| Network | Firewalled by default | iptables allowlist in the cave, or `egress: proxy` for an internal network with no route out |
| DNS | Filtered by default | `synthetic` (CoreDNS allowlist, default), `trusted` (redirect), or `open` |
| Kernel | Shared | Host kernel exposed; seccomp blocks AF_ALG (CVE-2026-31431) |
| Resources | PIDs limited | `pids: 4096` by default; `cpus` and `memory` set per cave in `policy.yaml` |
| Git identity | Isolated | Host `~/.gitconfig` not mounted by default |
| Docker socket | Safe by default | Not mounted, but fatal if added |
| Cloud metadata | Exposed | `169.254.169.254` reachable from container |
| Volumes | Persistent | Survive rebuilds; no integrity verification |

| Implemented hardening | Impact |
|------------------------|--------|
| Seccomp profile | Blocks AF_ALG and other unnecessary syscalls (CVE-2026-31431) |
| `no-new-privileges` | Blocks privilege escalation via setuid/execve |
| DNS filtering (CoreDNS) | `dns.mode: synthetic` in `policy.yaml`, the default for new caves |
| Internal-network egress | `network.egress: proxy`: no route out of the cave, no `NET_ADMIN` inside it |
| Model API operation allowlist | `proxy.operations`: a listed host admits only named `METHOD /path` pairs |
| Sidecar hardening | CoreDNS and mitmproxy run with capabilities dropped and `no-new-privileges` |
| PID limit | `resources.pids` in `policy.yaml`, 4096 by default |

| Future hardening | Impact |
|------------------|--------|
| CPU and memory limits by default | Today opt-in per cave via `resources` |
| Read-only root filesystem | Prevents persistent container modifications |
| Cloud metadata blocking | Prevents IAM credential leaks on cloud hosts in allowlist mode |
| User namespace remapping | Maps container root to unprivileged host UID |
| Port-matched allowlist rules | In allowlist mode, drop non-HTTPS ports on allowlisted IPs and private ranges |
| gVisor, Kata Containers, or a microVM runtime | Separate kernel from the host; Docker for the agent; see [RELATED-WORK.md](RELATED-WORK.md) |
| Volume integrity checks | Detects tampering between sessions |
| Audit logging | Supports post-incident analysis |

## What is isolated

**Filesystem.** The host filesystem is not accessible inside the container except for explicitly mounted paths. The workspace directory (`.:/workspace`) is bind-mounted read-write.

**Privileges.** The container user is unprivileged. Nix strips setuid bits, so there is no working `sudo`. Firewall rules are applied from the host via `docker compose exec --user root`, making them immutable from the container user's perspective.

**NPM install scripts.** `NPM_CONFIG_IGNORE_SCRIPTS=true` prevents automatic execution of npm lifecycle scripts, which are a common vector for supply-chain attacks. `NPM_CONFIG_MINIMUM_RELEASE_AGE=1440` avoids very recently published packages.

**Process isolation.** Container processes are isolated from host processes via Linux namespaces (PID, mount, UTS, IPC).

## What is NOT isolated

### Shared kernel

The container shares the host Linux kernel. A kernel vulnerability exploitable from within the container could compromise the host. This is the fundamental limitation of OS-level containerization versus hardware virtualization (VMs).

A custom seccomp profile (`seccomp.json`) reduces the kernel attack surface by blocking syscalls unnecessary for coding agent workloads. Notably, `AF_ALG` socket creation is denied (returns `EAFNOSUPPORT`), mitigating CVE-2026-31431 — a container escape via the kernel crypto API that requires only an unprivileged user. The profile is based on Docker's default with targeted additions. See https://copy.fail/ for details on the vulnerability.

The `no-new-privileges` security option is also applied, preventing processes from gaining additional privileges via setuid binaries or `execve`.

### Network

The firewall is enabled by default, restricting outbound access to allowlisted domains. It can be disabled with `jedi firewall off` or `--no-firewall`. Even with the firewall enabled:

- **DNS tunneling (in `open` mode).** With `dns.mode: open` in `policy.yaml`, DNS queries resolve for all domains and a malicious process could tunnel data through them. New caves default to `dns.mode: synthetic`, a CoreDNS sidecar that only resolves allowlisted domains (everything else returns NXDOMAIN). `dns.mode: trusted` redirects DNS to specific resolvers via iptables DNAT, which is lighter but does not prevent tunneling.
- **Exfiltration via allowed domains.** Data can be exfiltrated through any allowlisted endpoint. For example, if `github.com` is allowed, a process could push data to an attacker-controlled repository.
- **IP-based bypass.** The iptables rules use domain names, which are resolved to IPs at rule-creation time. If a domain resolves to multiple IPs or changes its DNS records after rules are applied, traffic may be allowed or blocked unexpectedly.
- **Every port on an allowlisted IP.** An allowlist entry becomes `iptables -A OUTPUT -d <ip> -j ACCEPT` with no protocol or port match, so it admits SSH, UDP, or any custom service on that IP, not just HTTPS. With the L7 proxy enabled, only ports 80 and 443 are forced through the proxy; other ports on allowlisted IPs still bypass HTTP-level policy, hooks, and secret handling. Compare Gondolin's userspace network stack, which drops any TCP flow it cannot classify as HTTP, TLS, SSH, or an explicit mapping (see [RELATED-WORK.md](RELATED-WORK.md)). With `network.egress: proxy` the cave has no route to any IP at all, so this gap closes; see "Internal-network egress" below.
- **Cloud metadata services.** On cloud instances (AWS, GCP, Azure), the instance metadata endpoint (`169.254.169.254`) is reachable from inside the container in allowlist mode. This can leak IAM credentials, instance identity tokens, and other sensitive data. The default firewall rules do not block this endpoint. With `network.egress: proxy` it is unreachable, like everything else outside the sidecar network.

### Secrets

`policy.yaml` supports two injection modes. In `env` mode the real value is resolved on the host and passed into the shell container's environment, where any process, including code the agent runs, can read it and send it to any allowlisted endpoint. In `proxy` mode the container only sees a placeholder; the real value lives in the proxy sidecar and is substituted into any header carrying the placeholder (or the listed `headers`) for the configured domains, so the secret never enters the agent's environment. A secret that names no `inject` mode uses `proxy` when `proxy.enabled` is true and `env` otherwise, so enabling the proxy is the one opt-in.

Proxy mode controls where a secret goes. What it is used for is bounded by `proxy.operations`, which limits a listed host to the `METHOD /path` pairs the agent needs (see "Model API operation allowlist" under "Implemented hardening"). Hosts without an entry admit any operation.

### Docker socket

The Docker socket is not mounted by default, but if a user adds it, any process inside the container gains full control over the Docker daemon — effectively root on the host.

There is no safe way to give the agent a working `docker` from inside a container. A separate kernel is the only answer: coop runs a full Docker daemon inside each Firecracker guest, where the agent can build and run containers without any path to the host daemon (see [RELATED-WORK.md](RELATED-WORK.md)). This is one of the concrete reasons for the microVM runtime in [ROADMAP.md](ROADMAP.md).

### Git identity

`~/.gitconfig` is not mounted by default. If re-enabled (by uncommenting the volume in `compose.yml`), the following information is exposed to the container:

- **Identity** (name, email) — can be used to impersonate the host user in commits pushed to attacker-controlled repositories
- **Credential helpers** — `[credential]` entries reveal the authentication system in use (e.g., `osxkeychain`, `store`, `libsecret`). If the credential store file is also mounted, credentials are directly accessible.
- **URL rewrite rules** — `[url "...".insteadOf]` entries can leak internal infrastructure hostnames (e.g., private GitLab instances)
- **Signing key configuration** — `[gpg]` and `[gpg "ssh"]` sections reveal key IDs, SSH key paths, and 1Password integration details. Combined with SSH agent forwarding, the container could sign commits as the host user.
- **Include directives** — `[include]` and `[includeIf]` reference other config files, expanding the attack surface if those paths are also mounted
- **Proxy/network configuration** — `[http.proxy]` entries reveal internal network topology

The mount is read-only so the container cannot modify the config, but the information disclosure is valuable for reconnaissance from inside a compromised container.

### Mounted host directories

Any path added via `jedi mount` is writable by default. A compromised process inside the container can modify or delete files in mounted directories. Use `--readonly` when the container only needs read access.

### Resource limits

The shell container gets a PID limit by default (`resources.pids` in `policy.yaml`, 4096), which stops fork bombs. CPU and memory limits are per cave (`resources.cpus`, `resources.memory`) and unset by default, so a runaway build can still exhaust host memory, and nothing limits disk fill through volumes.

### NET_ADMIN capability

The container is granted `NET_ADMIN` and `NET_RAW` capabilities to support iptables-based firewall rules. These capabilities also allow the container's root user (accessible via `docker compose exec --user root`) to manipulate network interfaces, routing tables, and raw sockets. The unprivileged container user cannot exercise these capabilities directly, but they expand the attack surface if a privilege escalation vulnerability exists.

Both capabilities exist only because the egress policy is enforced inside the container. With `network.egress: proxy` no iptables runs inside the cave and neither capability is granted (see "Internal-network egress" below).

### Named volumes

Persistent volumes (`cave config volume`, `cave history volume`) survive container rebuilds. If a volume is compromised (e.g., malicious Claude settings injected into the config volume), the compromise persists across container restarts. There is currently no integrity verification for volume contents.

### Setup-time integrity

All software (Claude Code, Oh My Zsh, skills) is baked into the image at build time via Nix from pinned inputs. No runtime downloads occur. The first-boot entrypoint only copies small config files (Claude settings, known marketplaces JSON) from the Nix store into Docker volumes. Build-time integrity depends on the Nix binary cache and flake lock file.

## Threat model

### Trust boundaries

| Component | Trust level | Rationale |
|-----------|-------------|-----------|
| Host machine | Trusted | User's workstation; runs Docker, controls container lifecycle |
| Docker daemon | Trusted | Manages containers; has root-equivalent access to the host |
| Container image (Nix) | Trusted | Built from pinned Nix flake inputs on the host before launch |
| Claude Code | Trusted but manipulable | Installed by the user, but runs `bypassPermissions` — will execute whatever code it is asked to, including malicious payloads delivered via prompt injection or dependency confusion |
| Code under review | Untrusted | The entire reason the container exists; may contain malicious build scripts, backdoored dependencies, or adversarial prompts |
| Network | Untrusted | Outbound by default; inbound blocked by Docker networking |

The **container is the trust boundary**. Everything inside it — workspace files, installed packages, Claude's actions — should be assumed potentially hostile. Everything outside it — host filesystem, Docker daemon, other containers — should remain unaffected.

### Container-to-host channels

Every path by which container-authored bytes reach the host is a taint source. A change that adds one, or widens one, needs the same scrutiny as a change to the firewall. The channels today:

- **`jedi harvest`.** A git bundle is copied out with `docker cp` and fetched into the bare repo under `repos/`. A bundle carries objects and refs only; hooks and config never leave the container. The host user still inspects the staging repo before fetching into a real one.
- **`jedi diff`.** Output of `git diff` inside the container, printed to the terminal. Text only; it is never fed to a shell on the host.
- **`jedi cp`.** Arbitrary files copied out with `docker cp`. This is the widest channel. The CLI prompts before writing outside the current directory, and the filenames and contents are attacker-controlled.
- **Writable mounts.** Anything added with `jedi mount` without `--readonly`, and the workspace bind mount where used, is written by the container directly.
- **Named volumes.** Not a host channel, but container-authored state that the next container start reads (see "Named volumes" above).

The list follows the taint-source discipline in coop's trust model (see [RELATED-WORK.md](RELATED-WORK.md)).

### What the container defends against

- **Accidental host damage.** Claude operating with `bypassPermissions` can `rm -rf /` inside the container without touching the host.
- **Malicious build scripts.** `npm install`, `make`, `pip install` may execute attacker-controlled code. The container limits what that code can reach.
- **Dependency supply-chain attacks.** A trojanized package can run arbitrary commands at install time. NPM scripts are disabled by default, and the container constrains the blast radius of anything that still executes.
- **Prompt injection via code.** Adversarial content in reviewed code (comments, docstrings, filenames) may instruct Claude to take harmful actions. The container ensures those actions are confined.

### What the container does NOT defend against

- **Container escapes.** A kernel exploit or Docker runtime vulnerability can break out of the namespace boundary. The seccomp profile reduces the kernel attack surface (e.g., blocking AF_ALG for CVE-2026-31431), but full mitigation requires a separate kernel (VM) via gVisor, Kata Containers, or a microVM runtime such as Gondolin (see [RELATED-WORK.md](RELATED-WORK.md)).
- **Compromising the trusted tool chain.** If the Nix cache, Claude's install script, or Oh My Zsh is compromised at build/setup time, the container starts in a compromised state.
- **Persistent volume poisoning.** Malicious code can write to named volumes (Claude config, shell history) that survive rebuilds, establishing persistence across sessions.
- **Data exfiltration via allowed channels.** Even with the firewall enabled, data can leave through DNS, allowed domains, or timing side-channels. The container reduces the surface but cannot eliminate covert channels.
- **Host resource exhaustion.** Without cgroup limits, a process inside the container can starve the host of CPU, memory, or disk.

## Implemented hardening

### Seccomp profile

A custom seccomp profile is applied via `security_opt: [seccomp:seccomp.json]` in compose. The profile is based on Docker's default (which blocks ~44 syscalls) and adds:

- **AF_ALG block (CVE-2026-31431).** `socket(AF_ALG, ...)` returns `EAFNOSUPPORT` (errno 97). AF_ALG exposes the kernel crypto API to userspace via `algif_aead`, which contains a logic flaw allowing a four-byte page-cache write from an unprivileged user. The 732-byte PoC works identically across all affected distributions (kernels shipped 2017-2026). Blocking AF_ALG has zero functional impact: TLS, SSH, dm-crypt, IPsec, and standard crypto libraries access the kernel crypto API directly, not through AF_ALG sockets.

Future consideration: restrict socket families to an allowlist (`AF_UNIX`, `AF_INET`, `AF_INET6`, `AF_NETLINK`) rather than a denylist. More aggressive but more resilient to future socket-family kernel bugs.

The profile source is `config/seccomp.json`, installed alongside the CLI via Nix and copied into each cave directory at `jedi init` / `jedi up` time.

### No-new-privileges

Applied via `security_opt: [no-new-privileges:true]` in compose. Prevents processes from gaining additional privileges via setuid binaries, `execve`, or other mechanisms. Combined with Nix stripping setuid bits, this ensures no privilege escalation path exists within the container.

### Internal-network egress

`network.egress: proxy` in `policy.yaml` puts the cave on a compose network marked `internal: true`, which Docker creates with no route to the outside, and attaches only the proxy and DNS sidecars to a second, external network. The cave can reach nothing but those two containers, every byte that leaves is HTTP or TLS the proxy has seen, and the shell container gets no `NET_ADMIN` or `NET_RAW` because nothing inside it programs a firewall. `jedi firewall` has nothing to switch in this mode and `--no-firewall` cannot open the network; the policy file is the only way out. It requires `proxy.enabled: true` and `dns.mode: synthetic`, and `jedi up` refuses any other combination. This is the in-Docker equivalent of Gondolin's host-terminated network (see [RELATED-WORK.md](RELATED-WORK.md)).

Each compose project gets its own /28 for the sidecar network, derived from the project name and moved past any Docker network that already covers it, so several caves or sessions with sidecars can run at once.

### Model API operation allowlist

`proxy.operations` lists, per host, the `METHOD /path` pairs the agent uses, and the proxy answers 403 to anything else on that host before it leaves. For `api.anthropic.com` that is `POST /v1/messages` and `POST /v1/messages/count_tokens`. A key that is stolen from the container, or driven by an injected prompt, then cannot reach account, admin, or file endpoints through the cave. Hosts without an entry are unrestricted. Borrowed from coop's credential proxy, which ships the same closed list (see [RELATED-WORK.md](RELATED-WORK.md)).

### Sidecar hardening

The CoreDNS and mitmproxy sidecars run with every capability dropped (CoreDNS keeps `NET_BIND_SERVICE` for port 53) and `no-new-privileges`. The proxy holds the real secrets, so it is the container a compromised cave would try next.

### PID limit

`resources.pids` in `policy.yaml` caps the shell container's process count, 4096 by default. `resources.cpus` and `resources.memory` add cgroup CPU and memory limits when set.

## Future hardening

The following improvements would strengthen isolation. They are listed roughly in order of impact-to-effort ratio.

### CPU and memory limits by default

`resources.cpus` and `resources.memory` are unset by default because a fixed cap breaks large builds: a Rust link step can exceed 8 GiB. The remaining step is a default derived from the host's size rather than a constant.

### Read-only root filesystem

Run the container with `read_only: true` and use tmpfs mounts for writable paths (`/tmp`, `/run`). This prevents persistent modifications to the container image layer.

### DNS filtering

Set `network.dns.mode: synthetic` in `policy.yaml` to deploy a CoreDNS sidecar that only resolves allowlisted domains (returns NXDOMAIN for everything else). This closes the DNS tunneling gap. Alternatively, `dns.mode: trusted` redirects DNS to specified resolvers via iptables DNAT — lighter, but does not prevent tunneling.

### Default-deny egress in allowlist mode

The strongest form, an internal network with the proxy as the only way out, is implemented as `network.egress: proxy` (see "Internal-network egress"). For caves that stay in allowlist mode, tighten the generated firewall so an allowlist entry admits only what the policy actually needs:

- When the L7 proxy is enabled, accept only the proxy IP and DNS, and drop everything else. Today allowlisted IPs are still accepted on every port, so only 80 and 443 are actually mediated.
- Without the proxy, match allowlist rules on `-p tcp --dport 443` (plus 80 or 22 where a domain needs them) instead of accepting all protocols and ports.
- Block RFC 1918 and link-local ranges by default, so an allowlisted public domain that resolves to a private address cannot reach LAN services.

This is the iptables approximation of what Gondolin gets by construction from its userspace network stack (see [RELATED-WORK.md](RELATED-WORK.md)).

### Cloud metadata blocking

Add an iptables rule to block access to `169.254.169.254` (and its IPv6 equivalent) by default when the firewall is enabled:

```bash
iptables -A OUTPUT -d 169.254.169.254 -j DROP
```

### User namespace remapping

Enable Docker user namespace remapping so that UID 0 inside the container maps to an unprivileged UID on the host. This mitigates container escapes that rely on the container root being actual host root.

### gVisor, Kata Containers, or a microVM runtime

Replace the default runc runtime with [gVisor](https://gvisor.dev/) (application kernel) or [Kata Containers](https://katacontainers.io/) (lightweight VMs). These provide a stronger isolation boundary than Linux namespaces alone by intercepting syscalls before they reach the host kernel. Both are pluggable Docker runtimes, so they are a `runtime:` line in `compose.yml` and do not touch the image build.

A third option is [Gondolin](https://github.com/earendil-works/gondolin), the QEMU microVM library behind `vmpi`. It gives a hardware boundary like Kata, but also replaces the guest's network path with a host-side userspace stack that classifies every flow (HTTP parsed, TLS intercepted, unknown TCP and all non-DNS UDP dropped) and substitutes secrets only into requests bound for their allowed hosts. Its image builder can take an OCI image as the rootfs, so a Nix- or Guix-built jedicave could run under it unchanged. It is not a Docker runtime, so it would replace the compose layer rather than plug into it. Trade-offs and a spike plan are in [RELATED-WORK.md](RELATED-WORK.md) and [ROADMAP.md](ROADMAP.md).

A fourth option is [Firecracker](https://firecracker-microvm.github.io/) directly, the path Trail of Bits' [coop](https://github.com/trailofbits/coop) takes. The guest gets a full kernel and a TAP device on a host bridge, so the egress allowlist moves from inside the container to the host's `FORWARD` chain, `NET_ADMIN` leaves the guest, the workspace lives on a block device at native speed, and a Docker daemon can run inside the guest. The costs are `sudo` on the host for TAP, bridge, and iptables setup, no macOS, and an ext4 rootfs plus kernel that we would have to build from the Nix or Guix closure ourselves, since coop only boots its own Ubuntu image. How this weighs against Gondolin is in [ROADMAP.md](ROADMAP.md).

### Volume integrity

Sign or checksum critical volume contents (Claude settings, shell history) at creation time and verify on container start. This would detect tampering between sessions.

### Network policy enforcement

For multi-container deployments, use Docker network policies or a service mesh to enforce least-privilege network communication between containers.

### Audit logging

Log all commands executed inside the container (via shell history, auditd, or eBPF-based tracing) to support post-incident analysis. This is especially relevant when reviewing untrusted code that may attempt to cover its tracks.
