# Security Model and Limitations

This document describes what the jedicave isolates, what it does not, and where gaps remain. The goal is to help users make informed decisions about their threat model rather than assume the container is a complete sandbox.

## At a glance

| Layer | Status | Notes |
|-------|--------|-------|
| Filesystem | Isolated | Host inaccessible except explicit mounts |
| Processes | Isolated | PID/mount/UTS/IPC namespaces |
| Privileges | Hardened | No sudo, no setuid; firewall rules immutable from container user |
| NPM scripts | Hardened | Disabled by default (`IGNORE_SCRIPTS=true`, 24h release age gate) |
| Network | Open by default | Full egress; use `jedi firewall on` to allowlist |
| DNS | Open | Not filtered even with firewall; tunneling possible |
| Kernel | Shared | Host kernel exposed; container escape via kernel vuln possible |
| Resources | Unlimited | No CPU/memory/PID limits configured |
| Git identity | Isolated | Host `~/.gitconfig` not mounted by default |
| Docker socket | Safe by default | Not mounted, but fatal if added |
| Cloud metadata | Exposed | `169.254.169.254` reachable from container |
| Volumes | Persistent | Survive rebuilds; no integrity verification |

| Future hardening | Impact |
|------------------|--------|
| Resource limits (cgroup) | Prevents fork bombs, memory exhaustion |
| Seccomp profile | Reduces kernel attack surface |
| Read-only root filesystem | Prevents persistent container modifications |
| DNS filtering (CoreDNS) | Closes DNS tunneling, strengthens firewall |
| Cloud metadata blocking | Prevents IAM credential leaks on cloud hosts |
| `no-new-privileges` | Blocks privilege escalation via setuid/execve |
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

### Network (by default)

Containers have full outbound network access unless `jedi firewall on` is used. Even with the firewall enabled:

- **DNS is not blocked.** DNS queries still resolve for all domains. A malicious process could use DNS tunneling to exfiltrate data or receive commands. Blocking DNS entirely would break name resolution for allowlisted domains, so this is a trade-off.
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

- **Container escapes.** A kernel exploit or Docker runtime vulnerability can break out of the namespace boundary. This requires a separate kernel (VM) to mitigate.
- **Compromising the trusted tool chain.** If the Nix cache, Claude's install script, or Oh My Zsh is compromised at build/setup time, the container starts in a compromised state.
- **Persistent volume poisoning.** Malicious code can write to named volumes (Claude config, shell history) that survive rebuilds, establishing persistence across sessions.
- **Data exfiltration via allowed channels.** Even with the firewall enabled, data can leave through DNS, allowed domains, or timing side-channels. The container reduces the surface but cannot eliminate covert channels.
- **Host resource exhaustion.** Without cgroup limits, a process inside the container can starve the host of CPU, memory, or disk.

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

### Seccomp profile

Apply a custom seccomp profile that restricts unnecessary syscalls. Docker's default seccomp profile blocks ~44 syscalls, but a tighter profile tailored to this workload could reduce the kernel attack surface further.

### Read-only root filesystem

Run the container with `read_only: true` and use tmpfs mounts for writable paths (`/tmp`, `/run`). This prevents persistent modifications to the container image layer.

### DNS filtering

Replace plain DNS with a filtering DNS resolver (e.g., CoreDNS with a policy plugin) that only resolves allowlisted domains. This would close the DNS tunneling gap and make the firewall rules more robust than IP-based iptables matching.

### Cloud metadata blocking

Add an iptables rule to block access to `169.254.169.254` (and its IPv6 equivalent) by default when the firewall is enabled:

```bash
iptables -A OUTPUT -d 169.254.169.254 -j DROP
```

### No-new-privileges

Add `security_opt: [no-new-privileges:true]` to prevent processes from gaining additional privileges via setuid binaries, `execve`, or other mechanisms.

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
