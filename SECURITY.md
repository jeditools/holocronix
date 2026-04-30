# Security Model and Limitations

This document describes what the jedicave isolates, what it does not, and where gaps remain. The goal is to help users make informed decisions about their threat model rather than assume the container is a complete sandbox.

## At a glance

| Layer | Status | Notes |
|-------|--------|-------|
| Filesystem | Isolated | Host inaccessible except explicit mounts |
| Processes | Isolated | PID/mount/UTS/IPC namespaces |
| Privileges | Hardened | No sudo, no setuid; firewall rules immutable from container user |
| NPM scripts | Hardened | Disabled by default (`IGNORE_SCRIPTS=true`, 24h release age gate) |
| Network | Firewalled by default | Egress restricted to allowlist; use `jedi firewall off` to open |
| DNS | Configurable | `open` (default), `trusted` (redirect), or `synthetic` (CoreDNS allowlist) |
| Kernel | Shared | Host kernel exposed; seccomp blocks AF_ALG (CVE-2026-31431) |
| Resources | Unlimited | No CPU/memory/PID limits configured |
| Git identity | Isolated | Host `~/.gitconfig` not mounted by default |
| Docker socket | Safe by default | Not mounted, but fatal if added |
| Cloud metadata | Exposed | `169.254.169.254` reachable from container |
| Volumes | Persistent | Survive rebuilds; no integrity verification |

| Implemented hardening | Impact |
|------------------------|--------|
| Seccomp profile | Blocks AF_ALG and other unnecessary syscalls (CVE-2026-31431) |
| `no-new-privileges` | Blocks privilege escalation via setuid/execve |
| DNS filtering (CoreDNS) | `dns.mode: synthetic` in `policy.yaml` |

| Future hardening | Impact |
|------------------|--------|
| Resource limits (cgroup) | Prevents fork bombs, memory exhaustion |
| Read-only root filesystem | Prevents persistent container modifications |
| Cloud metadata blocking | Prevents IAM credential leaks on cloud hosts |
| User namespace remapping | Maps container root to unprivileged host UID |
| gVisor / Kata Containers | Kernel-level isolation without full VMs |
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

- **DNS tunneling (in `open` mode).** By default (`dns.mode: open` in `policy.yaml`), DNS queries resolve for all domains. A malicious process could use DNS tunneling to exfiltrate data. Set `dns.mode: synthetic` to run a CoreDNS sidecar that only resolves allowlisted domains (everything else returns NXDOMAIN), or `dns.mode: trusted` to redirect DNS to specific resolvers via iptables DNAT.
- **Exfiltration via allowed domains.** Data can be exfiltrated through any allowlisted endpoint. For example, if `github.com` is allowed, a process could push data to an attacker-controlled repository.
- **IP-based bypass.** The iptables rules use domain names, which are resolved to IPs at rule-creation time. If a domain resolves to multiple IPs or changes its DNS records after rules are applied, traffic may be allowed or blocked unexpectedly.
- **Cloud metadata services.** On cloud instances (AWS, GCP, Azure), the instance metadata endpoint (`169.254.169.254`) is reachable from inside the container by default. This can leak IAM credentials, instance identity tokens, and other sensitive data. The default firewall rules do not block this endpoint.

### Docker socket

The Docker socket is not mounted by default, but if a user adds it, any process inside the container gains full control over the Docker daemon — effectively root on the host.

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

No CPU, memory, or disk limits are configured by default. A runaway or malicious process can exhaust host resources (fork bombs, memory allocation, disk fill via mounted volumes).

### NET_ADMIN capability

The container is granted `NET_ADMIN` and `NET_RAW` capabilities to support iptables-based firewall rules. These capabilities also allow the container's root user (accessible via `docker compose exec --user root`) to manipulate network interfaces, routing tables, and raw sockets. The unprivileged container user cannot exercise these capabilities directly, but they expand the attack surface if a privilege escalation vulnerability exists.

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

### What the container defends against

- **Accidental host damage.** Claude operating with `bypassPermissions` can `rm -rf /` inside the container without touching the host.
- **Malicious build scripts.** `npm install`, `make`, `pip install` may execute attacker-controlled code. The container limits what that code can reach.
- **Dependency supply-chain attacks.** A trojanized package can run arbitrary commands at install time. NPM scripts are disabled by default, and the container constrains the blast radius of anything that still executes.
- **Prompt injection via code.** Adversarial content in reviewed code (comments, docstrings, filenames) may instruct Claude to take harmful actions. The container ensures those actions are confined.

### What the container does NOT defend against

- **Container escapes.** A kernel exploit or Docker runtime vulnerability can break out of the namespace boundary. The seccomp profile reduces the kernel attack surface (e.g., blocking AF_ALG for CVE-2026-31431), but full mitigation requires a separate kernel (VM) via gVisor or Kata Containers.
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

## Future hardening

The following improvements would strengthen isolation. They are listed roughly in order of impact-to-effort ratio.

### Resource limits

Add default CPU, memory, and PID limits to `compose.yml`:

```yaml
deploy:
  resources:
    limits:
      cpus: '4'
      memory: 8G
      pids: 4096
```

### Read-only root filesystem

Run the container with `read_only: true` and use tmpfs mounts for writable paths (`/tmp`, `/run`). This prevents persistent modifications to the container image layer.

### DNS filtering

Set `network.dns.mode: synthetic` in `policy.yaml` to deploy a CoreDNS sidecar that only resolves allowlisted domains (returns NXDOMAIN for everything else). This closes the DNS tunneling gap. Alternatively, `dns.mode: trusted` redirects DNS to specified resolvers via iptables DNAT — lighter, but does not prevent tunneling.

### Cloud metadata blocking

Add an iptables rule to block access to `169.254.169.254` (and its IPv6 equivalent) by default when the firewall is enabled:

```bash
iptables -A OUTPUT -d 169.254.169.254 -j DROP
```

### User namespace remapping

Enable Docker user namespace remapping so that UID 0 inside the container maps to an unprivileged UID on the host. This mitigates container escapes that rely on the container root being actual host root.

### gVisor or Kata Containers

Replace the default runc runtime with [gVisor](https://gvisor.dev/) (application kernel) or [Kata Containers](https://katacontainers.io/) (lightweight VMs). These provide a stronger isolation boundary than Linux namespaces alone by intercepting syscalls before they reach the host kernel.

### Volume integrity

Sign or checksum critical volume contents (Claude settings, shell history) at creation time and verify on container start. This would detect tampering between sessions.

### Network policy enforcement

For multi-container deployments, use Docker network policies or a service mesh to enforce least-privilege network communication between containers.

### Audit logging

Log all commands executed inside the container (via shell history, auditd, or eBPF-based tracing) to support post-incident analysis. This is especially relevant when reviewing untrusted code that may attempt to cover its tracks.
