#!/usr/bin/env python3
"""jedi — CLI for managing jedicaves (sandboxed containers)."""

import json
import os
import shutil
import socket
import subprocess
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="jedi",
    help="Manage [blue]jedicaves[/blue] — sandboxed containers. Run [green bold]jedi guide[/green bold] for a step-by-step walkthrough.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()
err_console = Console(stderr=True)

CAVES_DIR = Path(os.environ.get("JEDI_CAVES_DIR", Path.home() / ".config" / "jedicaves"))
COMPOSE_SERVICE = "shell"
HOLOCRONIX_URL_DEFAULT = "github:jeditools/holocronix"


# --- Helpers ---

def cave_dir(name: str) -> Path:
    d = (CAVES_DIR / name).resolve()
    if not str(d).startswith(str(CAVES_DIR.resolve()) + "/"):
        err_console.print(f"[red]Invalid cave name '{name}'[/]")
        raise typer.Exit(1)
    return d


def resolve_cave(name: str | None) -> tuple[str, Path]:
    if name:
        d = cave_dir(name)
        if not d.exists():
            err_console.print(f"[red]Cave '{name}' not found.[/] Run: jedi list")
            raise typer.Exit(1)
        return name, d

    caves = _list_caves()
    if len(caves) == 1:
        return caves[0], cave_dir(caves[0])
    elif len(caves) == 0:
        err_console.print("[red]No caves found.[/] Run: jedi init <name>")
        raise typer.Exit(1)
    else:
        err_console.print(f"[red]Multiple caves exist[/] ({', '.join(caves)}). Specify one: jedi <command> <name>")
        raise typer.Exit(1)


def _list_caves() -> list[str]:
    if not CAVES_DIR.exists():
        return []
    return sorted(
        d.name for d in CAVES_DIR.iterdir()
        if d.is_dir() and (d / "flake.nix").exists()
    )


def complete_cave_name(incomplete: str) -> list[str]:
    return [name for name in _list_caves() if name.startswith(incomplete)]


def complete_repo_name(incomplete: str) -> list[str]:
    """Complete repo names from the active or first cave's repos dir."""
    caves = _list_caves()
    if not caves:
        return []
    # Use active cave if set, otherwise first cave
    active_file = CAVES_DIR / ".active"
    cave = active_file.read_text().strip() if active_file.exists() else caves[0]
    repos_dir = CAVES_DIR / cave / "repos"
    if not repos_dir.is_dir():
        return []
    return [
        p.name[:-4]
        for p in repos_dir.iterdir()
        if p.is_dir() and p.name.endswith(".git") and p.name[:-4].startswith(incomplete)
    ]


def _set_compose_project_name(d: Path, project_name: str):
    """Set COMPOSE_PROJECT_NAME in the cave's .env file."""
    env_file = d / ".env"
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    lines = [l for l in lines if not l.strip().startswith("COMPOSE_PROJECT_NAME=")]
    lines.append(f"COMPOSE_PROJECT_NAME={project_name}")
    env_file.write_text("\n".join(lines) + "\n")


def _clear_compose_project_name(d: Path):
    """Remove COMPOSE_PROJECT_NAME from the cave's .env file."""
    env_file = d / ".env"
    if not env_file.exists():
        return
    lines = env_file.read_text().splitlines()
    lines = [l for l in lines if not l.strip().startswith("COMPOSE_PROJECT_NAME=")]
    if any(l.strip() for l in lines):
        env_file.write_text("\n".join(lines) + "\n")
    else:
        env_file.unlink()


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    console.print(f"[dim]  {' '.join(cmd)}[/]")
    return subprocess.run(cmd, cwd=cwd, check=check)


def is_cave_running(d: Path) -> bool:
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", COMPOSE_SERVICE],
        cwd=d, capture_output=True, text=True
    )
    return bool(result.stdout.strip())


def _load_policy(d: Path) -> dict:
    """Load a cave's policy.yaml, falling back to legacy firewall-defaults.conf."""
    policy_file = d / "policy.yaml"
    if policy_file.exists():
        return yaml.safe_load(policy_file.read_text()) or {}

    # Legacy fallback: synthesize a minimal policy from firewall-defaults.conf
    legacy = d / "firewall-defaults.conf"
    if legacy.exists():
        domains = [
            line.strip() for line in legacy.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return {
            "network": {
                "firewall": True,
                "domains": domains,
                "dns": {"mode": "open"},
            },
            "secrets": {},
            "proxy": {"enabled": False},
            "hooks": [],
        }

    err_console.print(f"[red]No policy at {policy_file} (or legacy firewall-defaults.conf)[/]")
    raise typer.Exit(1)


def _policy_domains(policy: dict) -> list[str]:
    """Effective firewall allowlist: network.domains ∪ secrets[*].domains."""
    network = policy.get("network") or {}
    domains = list(network.get("domains") or [])
    for secret in (policy.get("secrets") or {}).values():
        for dom in secret.get("domains") or []:
            if dom not in domains:
                domains.append(dom)
    return domains


def _resolve_secrets(d: Path, policy: dict) -> Path | None:
    """Resolve each secret's value_cmd on the host and write secrets.env.

    Returns the path to secrets.env (for callers to know it exists), or None
    if no secrets are defined. The file is written mode 0600.
    """
    secrets = policy.get("secrets") or {}
    env_file = d / "secrets.env"

    if not secrets:
        if env_file.exists():
            env_file.unlink()
        return None

    lines = []
    for name, cfg in secrets.items():
        inject = (cfg or {}).get("inject", "env")
        cmd = (cfg or {}).get("value_cmd")
        placeholder = (cfg or {}).get("placeholder", "{{" + name + "}}")

        if inject == "proxy":
            # Shell container gets only the placeholder; real value goes to the proxy.
            lines.append(f"{name}={placeholder}")
            continue

        if not cmd:
            err_console.print(f"[red]Secret '{name}' missing value_cmd[/]")
            raise typer.Exit(1)
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            err_console.print(
                f"[red]Secret '{name}' value_cmd failed:[/]\n{result.stderr.strip()}"
            )
            raise typer.Exit(1)
        value = result.stdout.rstrip("\n")
        lines.append(f"{name}={value}")

    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)
    return env_file


def _clear_secrets(d: Path) -> None:
    env_file = d / "secrets.env"
    if env_file.exists():
        env_file.unlink()


def firewall_commands(d: Path) -> str:
    policy = _load_policy(d)
    domains = _policy_domains(policy)
    network = policy.get("network") or {}
    dns_cfg = network.get("dns") or {}
    dns_mode = dns_cfg.get("mode", "open")
    proxy_enabled = bool((policy.get("proxy") or {}).get("enabled", False))

    ipt = "/usr/local/sbin/iptables"
    cmds = [
        # Flush the filter OUTPUT chain so re-applying is idempotent.
        f"{ipt} -F OUTPUT",
    ]

    # Trusted DNS mode: redirect all DNS to the configured resolvers.
    # Only flush nat OUTPUT here — Docker's DNS DNAT rules live there,
    # so we must not touch it in normal mode.
    if dns_mode == "trusted":
        servers = dns_cfg.get("servers") or []
        if not servers:
            err_console.print("[red]dns.mode=trusted requires network.dns.servers[/]")
            raise typer.Exit(1)
        target = servers[0]
        cmds.insert(0, f"{ipt} -t nat -F OUTPUT")
        cmds.append(f"{ipt} -t nat -A OUTPUT -p udp --dport 53 -j DNAT --to-destination {target}:53")
        cmds.append(f"{ipt} -t nat -A OUTPUT -p tcp --dport 53 -j DNAT --to-destination {target}:53")

    if proxy_enabled:
        # L7 proxy enforcement: allow traffic to the proxy, drop direct 80/443.
        # Agent sets HTTP_PROXY/HTTPS_PROXY env vars; iptables ensures bypass
        # is impossible even if the agent unsets them.
        cmds.append(f"{ipt} -A OUTPUT -d {CAVE_NET_PROXY_IP} -j ACCEPT")
        cmds.append(f"{ipt} -A OUTPUT -p tcp --dport 80 -j DROP")
        cmds.append(f"{ipt} -A OUTPUT -p tcp --dport 443 -j DROP")

    for domain in domains:
        # Resolve hostnames to IPs on the host — iptables inside the
        # container may not have working DNS at this point.
        try:
            addrs = set(
                info[4][0] for info in socket.getaddrinfo(domain, None, socket.AF_INET)
            )
        except socket.gaierror:
            err_console.print(f"[yellow]Warning: could not resolve '{domain}', using hostname[/]")
            addrs = {domain}
        for addr in sorted(addrs):
            cmds.append(f"{ipt} -A OUTPUT -d {addr} -j ACCEPT")
    cmds.append(f"{ipt} -A OUTPUT -o lo -j ACCEPT")
    cmds.append(f"{ipt} -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT")
    cmds.append(f"{ipt} -A OUTPUT -j DROP")
    return " && ".join(cmds)


def check_deps() -> None:
    missing = []
    if not shutil.which("nix"):
        missing.append("nix (https://nixos.org/download/)")
    if not shutil.which("docker"):
        missing.append("docker")
    if missing:
        err_console.print("[red]Missing required tools:[/]")
        for m in missing:
            err_console.print(f"  - {m}")
        raise typer.Exit(1)


# --- Templates ---

FLAKE_TEMPLATE = """\
# Jedicave: {name}
#
# Edit the inputs and projectShells below, then build:
#   jedi build {name}
#
{{
  inputs = {{
    holocronix.url = "{holocronix_url}";

    # Add project flake inputs here, e.g.:
    #
    # Plain directory (copies as-is, includes uncommitted changes):
    #   my-project.url = "path:/home/yoda/code/my-project";
    #
    # Local git repo (committed state only):
    #   my-project.url = "git+file:///home/yoda/code/my-project";
    #
    # Local git repo, specific branch:
    #   my-project.url = "git+file:///home/yoda/code/my-project?ref=dev";
    #
    # Local git repo, specific commit:
    #   my-project.url = "git+file:///home/yoda/code/my-project?rev=abc1234";
    #
    # Local git repo, branch + commit:
    #   my-project.url = "git+file:///home/yoda/code/my-project?ref=dev&rev=abc1234";
    #
    # GitHub repo:
    #   my-project.url = "github:owner/repo";
    #   my-project.url = "github:owner/repo/branch-or-rev";
    #
    # Generic git remote:
    #   my-project.url = "git+https://example.com/owner/repo.git";
    #   my-project.url = "git+https://example.com/owner/repo.git?ref=main";
    #
    # FlakeHub:
    #   my-project.url = "https://flakehub.com/f/owner/repo/0.1.*.tar.gz";
    #
    # Nixpkgs (pinned):
    #   nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";
  }};

  outputs = {{ holocronix, ... }}@inputs: let
    system = "x86_64-linux";
    mkJediCave = holocronix.lib.${{system}}.mkJediCave;
  in {{
    packages.${{system}}.container = mkJediCave {{
      # List your project devShells here:
      # projectShells = [
      #   inputs.my-project.devShells.${{system}}.default
      # ];
    }};
  }};
}}
"""

POLICY_DEFAULTS = """\
# jedicave policy — per-cave security configuration
#
# `jedi up` reads this file at start time.

network:
  # Enable the iptables egress allowlist.
  firewall: true

  # Domains allowed through the firewall.
  domains:
    - api.anthropic.com
    # - github.com
    # - raw.githubusercontent.com
    # - registry.npmjs.org
    # - pypi.org
    # - files.pythonhosted.org

  # DNS mode:
  #   open       — no DNS filtering (allows DNS tunneling)
  #   trusted    — redirect DNS to specified resolvers via iptables DNAT
  #   synthetic  — CoreDNS sidecar; only allowlisted domains resolve
  dns:
    mode: open
    # servers: [1.1.1.1, 1.0.0.1]  # required for trusted mode

# Secrets resolved on the host and injected into the cave.
# `value_cmd` runs on the host at `jedi up` time.
#
# inject modes:
#   env    — passed as env var into the shell container
#   proxy  — replaced by the L7 proxy only for matching domains (requires proxy.enabled)
secrets: {}
  # ANTHROPIC_API_KEY:
  #   value_cmd: "cat ~/.config/anthropic/api_key"
  #   inject: env
  #   domains: [api.anthropic.com]
  #
  # GITHUB_TOKEN:
  #   value_cmd: "pass show github/token"
  #   inject: proxy
  #   placeholder: "{{GITHUB_TOKEN}}"
  #   domains: [api.github.com]
  #   headers: [Authorization]

# L7 egress proxy (mitmproxy sidecar). Enables HTTP-level allow/deny,
# proxy-based secret injection, and request/response hooks.
proxy:
  enabled: false

# Request/response hooks run in the proxy container.
hooks: []
  # - name: audit-log
  #   on: [request, response]
  #   type: log
  #   config:
  #     path: ./logs/audit.jsonl
"""

# Static IPs inside the per-cave bridge network. Stable so iptables and
# DNS settings can refer to them without a name-resolution step.
CAVE_NET_SUBNET = "172.30.0.0/24"
CAVE_NET_DNS_IP = "172.30.0.2"
CAVE_NET_PROXY_IP = "172.30.0.3"


def _generate_compose(name: str, policy: dict) -> str:
    """Render compose.yml content from a cave name + policy dict.

    Conditionally adds a CoreDNS sidecar (synthetic DNS mode) and a
    bridge network with static IPs. Layout matches the original
    static template when DNS is `open` and proxy is disabled.
    """
    network = policy.get("network") or {}
    dns_mode = (network.get("dns") or {}).get("mode", "open")
    proxy_enabled = bool((policy.get("proxy") or {}).get("enabled", False))

    needs_net = dns_mode == "synthetic" or proxy_enabled

    parts = ["services:"]

    # --- shell service ---
    env_lines = ["      - TZ=${TZ:-UTC}"]
    if proxy_enabled:
        env_lines.append(f"      - HTTP_PROXY=http://{CAVE_NET_PROXY_IP}:8080")
        env_lines.append(f"      - HTTPS_PROXY=http://{CAVE_NET_PROXY_IP}:8080")
        env_lines.append("      - NO_PROXY=localhost,127.0.0.1")
    env_block = "\n".join(env_lines)

    vol_lines = [
        f"      - {name}-history:/commandhistory",
        f"      - {name}-config:/env/.claude",
        "      - ./repos:/repos:ro",
    ]
    if proxy_enabled:
        vol_lines.append("      - ./proxy-ca/mitmproxy-ca-cert.pem:/proxy-ca/mitmproxy-ca-cert.pem:ro")
    vol_lines.extend([
        "      # Project source mounts go in compose.override.yml:",
        "      #   services:",
        "      #     shell:",
        "      #       volumes:",
        "      #         - /home/yoda/code/my-project:/workspace/my-project",
    ])
    vol_block = "\n".join(vol_lines)

    parts.append(f"""\
  shell:
    image: jedicave:latest
    init: true
    stdin_open: true
    tty: true
    cap_add:
      - NET_ADMIN
      - NET_RAW
    working_dir: /workspace
    environment:
{env_block}
    env_file:
      - path: secrets.env
        required: false
    volumes:
{vol_block}""")

    # depends_on
    depends = []
    if dns_mode == "synthetic":
        depends.append("dns")
    if proxy_enabled:
        depends.append("proxy")
    if depends:
        parts.append("    depends_on:")
        for dep in depends:
            parts.append(f"      - {dep}")

    if dns_mode == "synthetic":
        parts.append(f"""\
    dns:
      - {CAVE_NET_DNS_IP}""")

    if needs_net:
        parts.append("""\
    networks:
      - cave-net""")

    # --- dns sidecar (synthetic mode) ---
    if dns_mode == "synthetic":
        parts.append(f"""
  dns:
    image: coredns/coredns:1.12.0
    command: ["-conf", "/etc/coredns/Corefile"]
    volumes:
      - ./Corefile:/etc/coredns/Corefile:ro
    networks:
      cave-net:
        ipv4_address: {CAVE_NET_DNS_IP}""")

    # --- proxy sidecar (L7 proxy) ---
    if proxy_enabled:
        proxy_vol_lines = [
            "      - ./proxy-ca:/certs:ro",
            "      - ./proxy-policy.py:/policy.py:ro",
        ]
        # Mount secrets.env into proxy for proxy-mode secret injection
        proxy_vol_lines.append("      - ./proxy-secrets.env:/run/secrets/env:ro")
        proxy_vols = "\n".join(proxy_vol_lines)
        parts.append(f"""
  proxy:
    image: mitmproxy/mitmproxy:11
    command: ["mitmdump", "-s", "/policy.py", "--listen-port", "8080", "--set", "confdir=/certs"]
    volumes:
{proxy_vols}
    networks:
      cave-net:
        ipv4_address: {CAVE_NET_PROXY_IP}""")

    # --- volumes ---
    parts.append(f"""
volumes:
  {name}-history:
  {name}-config:""")

    # --- network ---
    if needs_net:
        parts.append(f"""
networks:
  cave-net:
    driver: bridge
    ipam:
      config:
        - subnet: {CAVE_NET_SUBNET}""")

    return "\n".join(parts) + "\n"


def _generate_corefile(policy: dict) -> str:
    """Render a CoreDNS Corefile from policy. Allowlisted domains forward
    upstream; everything else returns NXDOMAIN."""
    network = policy.get("network") or {}
    dns_cfg = network.get("dns") or {}
    upstream = dns_cfg.get("upstream") or ["8.8.8.8", "8.8.4.4"]
    domains = _policy_domains(policy)

    upstream_str = " ".join(upstream)
    blocks = ["(forward_upstream) {", f"    forward . {upstream_str}", "}", ""]
    for dom in domains:
        blocks.append(f"{dom} {{")
        blocks.append("    import forward_upstream")
        blocks.append("}")
        blocks.append("")
    blocks.extend([
        ". {",
        "    template IN ANY . {",
        "        rcode NXDOMAIN",
        "    }",
        "}",
    ])
    return "\n".join(blocks) + "\n"


def _generate_proxy_policy(policy: dict) -> str:
    """Render the mitmproxy addon script that enforces the domain allowlist,
    injects proxy-mode secrets, and runs hooks."""
    domains = _policy_domains(policy)
    secrets = policy.get("secrets") or {}

    # Build secret injection map: placeholder → {value_env_var, domains, headers}
    # Real values are loaded at runtime from /run/secrets/env inside the proxy.
    proxy_secrets = {}
    for sname, cfg in secrets.items():
        if (cfg or {}).get("inject") == "proxy":
            placeholder = (cfg or {}).get("placeholder", "{{" + sname + "}}")
            proxy_secrets[placeholder] = {
                "env_var": sname,
                "domains": set(cfg.get("domains") or []),
                "headers": cfg.get("headers") or [],
            }

    # Build hooks
    hooks = policy.get("hooks") or []

    lines = [
        '"""jedicave L7 policy — generated by jedi, do not edit."""',
        "import os, json, datetime",
        "from mitmproxy import http",
        "",
        f"ALLOWED = {set(domains)!r}",
        "",
    ]

    # Secret injection config
    lines.append("# Proxy-mode secrets: placeholder → injection config")
    lines.append("SECRETS = {}")
    lines.append("")
    lines.append("def _load_secrets():")
    lines.append('    env_path = "/run/secrets/env"')
    lines.append("    if not os.path.exists(env_path):")
    lines.append("        return")
    lines.append("    vals = {}")
    lines.append("    for line in open(env_path):")
    lines.append("        line = line.strip()")
    lines.append('        if "=" in line and not line.startswith("#"):')
    lines.append('            k, v = line.split("=", 1)')
    lines.append("            vals[k] = v")

    for placeholder, cfg in proxy_secrets.items():
        env_var = cfg["env_var"]
        doms = cfg["domains"]
        headers = cfg["headers"]
        lines.append(f'    if "{env_var}" in vals:')
        lines.append(f'        SECRETS["{placeholder}"] = {{')
        lines.append(f'            "value": vals["{env_var}"],')
        lines.append(f'            "domains": {doms!r},')
        lines.append(f'            "headers": {headers!r},')
        lines.append(f"        }}")
    lines.append("")
    lines.append("_load_secrets()")
    lines.append("")

    # Audit log hook setup
    audit_hooks = [h for h in hooks if h.get("type") == "log"]
    if audit_hooks:
        lines.append("# Audit log file handles")
        lines.append("_audit_files = {}")
        for h in audit_hooks:
            log_path = h.get("config", {}).get("path", "/var/log/audit.jsonl")
            lines.append(f'_audit_files["{h["name"]}"] = open("{log_path}", "a")')
        lines.append("")

    # Addon class
    lines.extend([
        "class PolicyAddon:",
        "    def request(self, flow: http.HTTPFlow):",
        "        host = flow.request.pretty_host",
        "        if host not in ALLOWED:",
        '            flow.response = http.Response.make(403, b"Blocked by jedicave policy")',
        "            return",
        "",
        "        # Proxy-mode secret injection",
        "        for placeholder, cfg in SECRETS.items():",
        '            if host in cfg["domains"]:',
        '                if cfg["headers"]:',
        '                    for h in cfg["headers"]:',
        "                        if h in flow.request.headers:",
        "                            flow.request.headers[h] = flow.request.headers[h].replace(",
        '                                placeholder, cfg["value"])',
        "                # Also replace in request body",
        "                if flow.request.content:",
        "                    flow.request.content = flow.request.content.replace(",
        '                        placeholder.encode(), cfg["value"].encode())',
        "",
    ])

    # Request hooks
    for h in hooks:
        if "request" in (h.get("on") or []):
            if h["type"] == "log":
                lines.extend([
                    f'        # hook: {h["name"]}',
                    f'        _audit_files["{h["name"]}"].write(json.dumps({{',
                    '            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),',
                    '            "type": "request",',
                    '            "method": flow.request.method,',
                    '            "url": flow.request.pretty_url,',
                    '            "host": host,',
                    '            "size": len(flow.request.content or b""),',
                    '        }) + "\\n")',
                    f'        _audit_files["{h["name"]}"].flush()',
                    "",
                ])
            elif h["type"] == "block":
                max_size = h.get("config", {}).get("max_body_size")
                if max_size:
                    lines.extend([
                        f'        # hook: {h["name"]}',
                        f"        if len(flow.request.content or b'') > {max_size}:",
                        '            flow.response = http.Response.make(413, b"Request too large")',
                        "            return",
                        "",
                    ])

    lines.extend([
        "    def response(self, flow: http.HTTPFlow):",
        "        pass",
    ])

    # Response hooks
    for h in hooks:
        if "response" in (h.get("on") or []):
            if h["type"] == "log":
                lines.extend([
                    f'        # hook: {h["name"]}',
                    f'        _audit_files["{h["name"]}"].write(json.dumps({{',
                    '            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),',
                    '            "type": "response",',
                    '            "method": flow.request.method,',
                    '            "url": flow.request.pretty_url,',
                    '            "status": flow.response.status_code,',
                    '            "size": len(flow.response.content or b""),',
                    '        }) + "\\n")',
                    f'        _audit_files["{h["name"]}"].flush()',
                ])

    lines.extend([
        "",
        "addons = [PolicyAddon()]",
        "",
    ])
    return "\n".join(lines)


def _generate_proxy_ca(d: Path) -> None:
    """Generate a mitmproxy-compatible CA keypair in <cave>/proxy-ca/
    if one doesn't already exist."""
    ca_dir = d / "proxy-ca"
    cert = ca_dir / "mitmproxy-ca-cert.pem"
    key = ca_dir / "mitmproxy-ca.pem"

    if cert.exists() and key.exists():
        return

    ca_dir.mkdir(parents=True, exist_ok=True)

    # Generate self-signed CA via openssl (available on all hosts)
    subprocess.run([
        "openssl", "req", "-x509", "-new", "-nodes",
        "-keyout", str(key),
        "-out", str(cert),
        "-days", "3650",
        "-subj", "/CN=jedicave proxy CA",
    ], check=True, capture_output=True)
    key.chmod(0o600)


def _resolve_proxy_secrets(d: Path, policy: dict) -> None:
    """Resolve proxy-mode secrets and write proxy-secrets.env.

    This file is mounted only into the proxy container, never the shell.
    """
    secrets = policy.get("secrets") or {}
    env_file = d / "proxy-secrets.env"

    proxy_secrets = {
        name: cfg for name, cfg in secrets.items()
        if (cfg or {}).get("inject") == "proxy"
    }

    if not proxy_secrets:
        if env_file.exists():
            env_file.unlink()
        return

    lines = []
    for name, cfg in proxy_secrets.items():
        cmd = (cfg or {}).get("value_cmd")
        if not cmd:
            err_console.print(f"[red]Proxy secret '{name}' missing value_cmd[/]")
            raise typer.Exit(1)
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            err_console.print(
                f"[red]Proxy secret '{name}' value_cmd failed:[/]\n{result.stderr.strip()}"
            )
            raise typer.Exit(1)
        lines.append(f"{name}={result.stdout.rstrip(chr(10))}")

    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)


def _write_compose(d: Path, name: str, policy: dict) -> None:
    """Regenerate compose.yml + supporting files for the current policy."""
    (d / "compose.yml").write_text(_generate_compose(name, policy))

    if ((policy.get("network") or {}).get("dns") or {}).get("mode") == "synthetic":
        (d / "Corefile").write_text(_generate_corefile(policy))

    if (policy.get("proxy") or {}).get("enabled", False):
        _generate_proxy_ca(d)
        (d / "proxy-policy.py").write_text(_generate_proxy_policy(policy))
        _resolve_proxy_secrets(d, policy)
        # Create logs dir for audit hooks
        hooks = policy.get("hooks") or []
        for h in hooks:
            if h.get("type") == "log":
                log_path = h.get("config", {}).get("path", "")
                if log_path.startswith("./"):
                    (d / log_path).parent.mkdir(parents=True, exist_ok=True)


# --- Commands ---

@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Cave name", autocompletion=complete_cave_name)],
    holocronix_url: Annotated[Optional[str], typer.Option(help="Holocronix flake URL")] = None,
):
    """Create a new cave."""
    d = cave_dir(name)

    if d.exists() and (d / "flake.nix").exists():
        err_console.print(f"[red]Cave '{name}' already exists at {d}[/]")
        raise typer.Exit(1)

    d.mkdir(parents=True, exist_ok=True)
    (d / "repos").mkdir(exist_ok=True)

    url = holocronix_url or os.environ.get("HOLOCRONIX_URL", HOLOCRONIX_URL_DEFAULT)

    (d / "flake.nix").write_text(FLAKE_TEMPLATE.format(name=name, holocronix_url=url))
    (d / "policy.yaml").write_text(POLICY_DEFAULTS)
    _write_compose(d, name, _load_policy(d))

    console.print(f"[green]Cave '{name}' created at {d}[/]")
    console.print("Next steps:")
    console.print(f"  1. Edit {d / 'flake.nix'} — add your project inputs and devShells")
    console.print(f"  2. Seed your project: jedi seed <repo-path> {name}")
    console.print(f"  3. Run: jedi build {name}")
    console.print()
    console.print("Then:")
    console.print(f"  jedi up {name}        Start the cave")
    console.print(f"  jedi enter {name}     Enter the cave")
    console.print(f"  jedi guide           Learn more about caves")
    console.print(f"  jedi --help          See all commands")


@app.command()
def inputs(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
):
    """List flake inputs and their locked revisions."""
    check_deps()
    name, d = resolve_cave(name)
    result = subprocess.run(
        ["nix", "flake", "metadata", "--json", "."],
        cwd=d, capture_output=True, text=True
    )
    if result.returncode != 0:
        err_console.print(f"[red]Failed to read flake metadata:[/]\n{result.stderr.strip()}")
        raise typer.Exit(1)

    meta = json.loads(result.stdout)
    locks = meta.get("locks", {}).get("nodes", {})
    root_inputs = locks.get("root", {}).get("inputs", {})

    if not root_inputs:
        console.print(f"Cave '{name}' has no inputs")
        return

    table = Table(title=f"Inputs for cave '{name}'")
    table.add_column("Input", style="cyan")
    table.add_column("Source")
    table.add_column("Ref", style="dim")
    table.add_column("Rev", style="dim")

    for input_name, node_key in sorted(root_inputs.items()):
        node = locks.get(node_key, {})
        locked = node.get("locked", {})
        rev = locked.get("rev", "")[:12]
        ref = locked.get("ref", "")
        input_type = locked.get("type", "")
        if input_type == "path":
            loc = locked.get("path", "")
        elif locked.get("url"):
            loc = locked["url"]
        else:
            owner = locked.get("owner", "")
            repo = locked.get("repo", "")
            loc = f"{owner}/{repo}" if owner else ""
        table.add_row(input_name, loc, ref, rev)

    console.print(table)


@app.command()
def update(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    input: Annotated[Optional[str], typer.Option("--input", "-i", help="Specific input to update (default: all)")] = None,
):
    """Update flake inputs (lock only, no build)."""
    check_deps()
    name, d = resolve_cave(name)

    if input:
        console.print(f"Updating input [cyan]{input}[/]...")
        result = run(["nix", "flake", "update", input], cwd=d, check=False)
    else:
        console.print("Updating all inputs...")
        result = run(["nix", "flake", "update"], cwd=d, check=False)
    if result.returncode != 0:
        err_console.print("[red]Failed to update flake inputs[/]")
        raise typer.Exit(1)
    console.print(f"[green]Lock updated for cave '{name}'[/]")


@app.command()
def build(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    update: Annotated[bool, typer.Option("--update", "-u", help="Update all flake inputs before building")] = False,
):
    """Build cave image."""
    check_deps()
    name, d = resolve_cave(name)

    if update:
        console.print("Updating all inputs...")
        run(["nix", "flake", "update"], cwd=d)

    console.print(f"Building cave [cyan]{name}[/]...")
    run(["nix", "build", ".#container", "--print-build-logs"], cwd=d)

    result_link = d / "result"
    if not result_link.exists():
        err_console.print("[red]Build produced no result link[/]")
        raise typer.Exit(1)

    console.print("Loading image into Docker...")
    run(["docker", "load", "-i", str(result_link)])
    console.print(f"[green]Cave '{name}' built and loaded[/]")


@app.command()
def up(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    firewall: Annotated[bool, typer.Option(help="Enable firewall on startup")] = True,
):
    """Start cave container."""
    name, d = resolve_cave(name)
    policy = _load_policy(d)
    _write_compose(d, name, policy)
    env_file = _resolve_secrets(d, policy)
    if env_file:
        console.print(f"[dim]  resolved {len(policy.get('secrets') or {})} secrets → {env_file.name}[/]")

    console.print(f"Starting cave [cyan]{name}[/]...")
    run(["docker", "compose", "up", "-d"], cwd=d)
    if firewall:
        fw_cmds = firewall_commands(d)
        run(["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "bash", "-c", fw_cmds], cwd=d)
        console.print(f"[green]Cave '{name}' running (firewall on)[/]")
    else:
        console.print(f"[green]Cave '{name}' running[/]")
    console.print(f"Run: jedi enter {name}")


@app.command()
def down(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
):
    """Stop cave container."""
    name, d = resolve_cave(name)
    console.print(f"Stopping cave [cyan]{name}[/]...")
    run(["docker", "compose", "down"], cwd=d)
    _clear_compose_project_name(d)
    _clear_secrets(d)
    console.print(f"[green]Cave '{name}' stopped[/]")


@app.command()
def rename(
    old_name: Annotated[str, typer.Argument(help="Current cave name", autocompletion=complete_cave_name)],
    new_name: Annotated[str, typer.Argument(help="New cave name")],
):
    """Rename a cave.

    If the cave is running, containers keep running under the old
    compose project name (stored in .env). Next down/up cycle switches
    to the new name automatically.

    Examples:
        jedi rename myproject newname
    """
    _, old_dir = resolve_cave(old_name)
    new_dir = cave_dir(new_name)

    if new_dir.exists():
        err_console.print(f"[red]Cave '{new_name}' already exists[/]")
        raise typer.Exit(1)

    running = is_cave_running(old_dir)

    old_dir.rename(new_dir)

    if running:
        # Only set if not already overridden (handles chained renames)
        env_file = new_dir / ".env"
        has_override = env_file.exists() and any(
            l.strip().startswith("COMPOSE_PROJECT_NAME=")
            for l in env_file.read_text().splitlines()
        )
        if not has_override:
            _set_compose_project_name(new_dir, old_name)

    # Update active cave pointer
    active_file = CAVES_DIR / ".active"
    if active_file.exists() and active_file.read_text().strip() == old_name:
        active_file.write_text(new_name + "\n")

    console.print(f"[green]Renamed cave '{old_name}' → '{new_name}'[/]")
    if running:
        console.print(f"  Container still running (will switch on next down/up)")


@app.command()
def restart(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    firewall: Annotated[bool, typer.Option(help="Enable firewall on startup")] = True,
):
    """Restart cave container (down + up)."""
    name, d = resolve_cave(name)
    policy = _load_policy(d)

    console.print(f"Restarting cave [cyan]{name}[/]...")
    run(["docker", "compose", "down"], cwd=d)
    _clear_compose_project_name(d)
    _clear_secrets(d)

    _write_compose(d, name, policy)
    env_file = _resolve_secrets(d, policy)
    if env_file:
        console.print(f"[dim]  resolved {len(policy.get('secrets') or {})} secrets → {env_file.name}[/]")

    run(["docker", "compose", "up", "-d"], cwd=d)
    if firewall:
        fw_cmds = firewall_commands(d)
        run(["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "bash", "-c", fw_cmds], cwd=d)
        console.print(f"[green]Cave '{name}' restarted (firewall on)[/]")
    else:
        console.print(f"[green]Cave '{name}' restarted[/]")


@app.command()
def shell(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    firewall: Annotated[bool, typer.Option(help="Enable firewall")] = True,
):
    """Ephemeral shell (no 'up' needed)."""
    name, d = resolve_cave(name)
    if firewall:
        fw_cmds = firewall_commands(d)
        os.execvp("docker", ["docker", "compose", "--project-directory", str(d),
                              "run", "--rm", "-it", "--user", "root",
                              COMPOSE_SERVICE, "bash", "-c",
                              f"{fw_cmds} && exec su -s /bin/zsh yoda"])
    else:
        os.execvp("docker", ["docker", "compose", "--project-directory", str(d),
                              "run", "--rm", "-it", COMPOSE_SERVICE, "zsh"])


@app.command()
def enter(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
):
    """Enter a running cave (requires 'up')."""
    name, d = resolve_cave(name)
    os.execvp("docker", ["docker", "compose", "--project-directory", str(d),
                          "exec", COMPOSE_SERVICE, "zsh"])


@app.command(context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
def exec(
    ctx: typer.Context,
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
):
    """Run a command in a running cave (requires 'up')."""
    name, d = resolve_cave(name)
    os.execvp("docker", ["docker", "compose", "--project-directory", str(d),
                          "exec", COMPOSE_SERVICE] + ctx.args)


@app.command()
def cp(
    src: Annotated[str, typer.Argument(help="Source path (prefix with : for container path)")],
    dst: Annotated[str, typer.Argument(help="Destination path (prefix with : for container path)")],
    name: Annotated[Optional[str], typer.Option("--cave", "-c", help="Cave name", autocompletion=complete_cave_name)] = None,
):
    """Copy files between host and cave container.

    Prefix container paths with : (colon).

    \b
    Examples:
      jedi cp :/env/.claude/projects/foo/bar.md ./bar.md
      jedi cp ./config.yml :/workspace/project/config.yml
    """
    name, d = resolve_cave(name)

    if not is_cave_running(d):
        err_console.print(f"[red]Cave '{name}' is not running.[/] Start it first: jedi up {name}")
        raise typer.Exit(1)

    container_id = subprocess.run(
        ["docker", "compose", "ps", "-q", COMPOSE_SERVICE],
        cwd=d, capture_output=True, text=True,
    ).stdout.strip()

    if not container_id:
        err_console.print(f"[red]Could not find container for cave '{name}'[/]")
        raise typer.Exit(1)

    if src.startswith(":") and dst.startswith(":"):
        err_console.print("[red]Both paths are container paths. One must be a host path.[/]")
        raise typer.Exit(1)

    if not src.startswith(":") and not dst.startswith(":"):
        err_console.print("[red]Neither path is a container path. Prefix container paths with :[/]")
        raise typer.Exit(1)

    if src.startswith(":"):
        # Container → host: resolve dst and confirm if outside cwd
        dst_resolved = Path(dst).resolve()
        cwd = Path.cwd().resolve()
        if not str(dst_resolved).startswith(str(cwd) + "/") and dst_resolved != cwd:
            if not typer.confirm(f"Write to '{dst_resolved}' (outside current directory)?", default=False):
                console.print("Aborted")
                return
        run(["docker", "cp", f"{container_id}:{src[1:]}", dst])
    else:
        # Host → container
        run(["docker", "cp", src, f"{container_id}:{dst[1:]}"])


@app.command("list")
def list_cmd():
    """List caves."""
    caves = _list_caves()
    if not caves:
        console.print("No caves found. Run: jedi init <name>")
        return

    table = Table()
    table.add_column("Cave", style="cyan")
    table.add_column("Status")
    table.add_column("Path", style="dim")

    for name in caves:
        d = cave_dir(name)
        running = is_cave_running(d)
        status = "[green]running[/]" if running else "[dim]stopped[/]"
        table.add_row(name, status, str(d))

    console.print(table)


class FirewallAction(str, Enum):
    on = "on"
    off = "off"
    status = "status"


@app.command()
def firewall(
    action: Annotated[FirewallAction, typer.Argument(help="Firewall action")],
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show full iptables output")] = False,
):
    """Manage cave firewall."""
    name, d = resolve_cave(name)

    if not is_cave_running(d):
        err_console.print(
            f"[red]Cave '{name}' is not running.[/] Start it first:\n"
            f"  jedi up {name}\n"
            f"  jedi up --firewall {name}\n"
            f"Or use: jedi shell --firewall {name}"
        )
        raise typer.Exit(1)

    if action == FirewallAction.on:
        fw_cmds = firewall_commands(d)
        run(["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "bash", "-c", fw_cmds], cwd=d)
        domains = _policy_domains(_load_policy(d))
        console.print(f"[green]Firewall enabled ({len(domains)} domains allowlisted)[/]")

    elif action == FirewallAction.off:
        ipt = "/usr/local/sbin/iptables"
        run(["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "bash", "-c",
             f"{ipt} -F OUTPUT && {ipt} -P OUTPUT ACCEPT"], cwd=d)
        console.print("[green]Firewall disabled[/]")

    elif action == FirewallAction.status:
        result = subprocess.run(
            ["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "/usr/local/sbin/iptables", "-L", "OUTPUT", "-n"],
            cwd=d, capture_output=True, text=True
        )
        lines = result.stdout.strip().splitlines()
        rules = [l for l in lines[2:] if l.strip()] if len(lines) > 2 else []
        has_drop = any("DROP" in r for r in rules)
        accept_ips = [r.split()[4] for r in rules if "ACCEPT" in r and r.split()[4] != "0.0.0.0/0"]

        if has_drop:
            console.print("[green]Firewall: ON[/]")
            if accept_ips:
                console.print(f"Allowed destinations: {', '.join(accept_ips)}")
        else:
            console.print("[yellow]Firewall: OFF (all traffic allowed)[/]")

        if verbose:
            console.print()
            run(["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
                 "/usr/local/sbin/iptables", "-L", "-n", "-v"], cwd=d)


@app.command()
def seed(
    repo_path: Annotated[str, typer.Argument(help="Path to source git repo")],
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    branch: Annotated[Optional[str], typer.Option(help="Branch to seed (default: current branch)")] = None,
    all_branches: Annotated[bool, typer.Option("--all", help="Seed all branches")] = False,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force-push (overwrite diverged branches)")] = False,
):
    """Seed a repo into the cave as a bare repo for secure git handoff."""
    name, d = resolve_cave(name)
    repo = Path(repo_path).resolve()

    # Ensure repos dir exists (for caves created before this feature)
    (d / "repos").mkdir(exist_ok=True)

    # Check compose setup
    compose_file = d / "compose.yml"
    if compose_file.exists() and "./repos:/repos" not in compose_file.read_text():
        err_console.print(
            "[yellow]compose.yml missing repos mount.[/] Add under services.shell.volumes:\n"
            "      - ./repos:/repos:ro"
        )

    # Check if there's a direct mount that should be removed
    override_file = d / "compose.override.yml"
    if override_file.exists():
        target = f"/workspace/{repo.name}"
        for line in override_file.read_text().splitlines():
            if line.strip().startswith("- ") and target in line:
                err_console.print(
                    f"[yellow]compose.override.yml has a direct mount for {repo.name}.[/]\n"
                    f"  Consider removing: {line.strip()[2:]}"
                )

    if not (repo / ".git").is_dir():
        err_console.print(f"[red]{repo} is not a git repository[/]")
        raise typer.Exit(1)

    # Determine branch to push
    if not all_branches and not branch:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True,
        )
        branch = result.stdout.strip()
        if not branch or branch == "HEAD":
            err_console.print("[red]Detached HEAD — specify --branch or --all[/]")
            raise typer.Exit(1)

    repo_name = repo.name
    bare_path = d / "repos" / f"{repo_name}.git"

    # Init bare repo if needed
    if not (bare_path / "HEAD").exists():
        bare_path.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--bare", str(bare_path)])

    # Store source repo path for harvest
    run(["git", "--git-dir", str(bare_path), "config",
         "jedicave.sourceRepo", str(repo)], check=False)

    # Push branches
    push_cmd = ["git", "push"] + (["--force"] if force else [])
    try:
        if all_branches:
            run(push_cmd + [str(bare_path), "--all"], cwd=repo)
            # Set HEAD to the current branch if possible
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo, capture_output=True, text=True,
            )
            head_branch = result.stdout.strip()
            if head_branch and head_branch != "HEAD":
                run(["git", "--git-dir", str(bare_path),
                     "symbolic-ref", "HEAD", f"refs/heads/{head_branch}"])
        else:
            run(push_cmd + [str(bare_path),
                 f"refs/heads/{branch}:refs/heads/{branch}"], cwd=repo)
            # Set HEAD to the seeded branch
            run(["git", "--git-dir", str(bare_path),
                 "symbolic-ref", "HEAD", f"refs/heads/{branch}"])
    except subprocess.CalledProcessError:
        err_console.print(
            "[red]Push rejected — the bare repo has diverged (e.g. from harvested agent commits).[/]\n"
            "  Re-run with [bold]--force[/] to overwrite: [dim]jedi seed --force ...[/]"
        )
        raise typer.Exit(1)

    # Record seeded commit count for harvest reporting
    count_result = subprocess.run(
        ["git", "--git-dir", str(bare_path), "rev-list", "--all", "--count"],
        capture_output=True, text=True,
    )
    if count_result.returncode == 0:
        run(["git", "--git-dir", str(bare_path), "config",
             "jedicave.seededCount", count_result.stdout.strip()], check=False)

    if all_branches:
        branch_list = subprocess.run(
            ["git", "--git-dir", str(bare_path), "branch"],
            capture_output=True, text=True,
        )
        branch_count = len(branch_list.stdout.strip().splitlines()) if branch_list.stdout.strip() else 0
        console.print(f"[green]Seeded '{repo_name}' ({branch_count} branches) into cave '{name}'[/]")
    else:
        console.print(f"[green]Seeded '{repo_name}' branch '{branch}' into cave '{name}'[/]")
    console.print(f"  Bare repo: {bare_path}")
    console.print(f"  Will be available at /workspace/{repo_name} inside the container")


@app.command()
def reseed(
    repo_name: Annotated[Optional[str], typer.Argument(help="Repo name (default: all)", autocompletion=complete_repo_name)] = None,
    name: Annotated[Optional[str], typer.Option("--cave", "-c", help="Cave name", autocompletion=complete_cave_name)] = None,
    all_branches: Annotated[bool, typer.Option("--all", help="Push all branches")] = False,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force-push (overwrite diverged branches)")] = False,
):
    """Re-push host repo commits into seeded bare repos.

    Examples:
        jedi reseed myproject          Reseed a specific repo
        jedi reseed                    Reseed all repos in the active cave
        jedi reseed myproject --all    Reseed all branches
        jedi reseed --cave dev -f      Force-reseed all repos in 'dev' cave
    """
    name, d = resolve_cave(name)
    repos_dir = d / "repos"

    if not repos_dir.exists():
        console.print("No repos seeded. Run: jedi seed <repo-path>")
        return

    bare_repos = sorted(
        p for p in repos_dir.iterdir()
        if p.is_dir() and p.name.endswith(".git")
    )

    if repo_name:
        bare_repos = [p for p in bare_repos if p.name == f"{repo_name}.git"]
        if not bare_repos:
            err_console.print(f"[red]No seeded repo '{repo_name}' in cave '{name}'[/]")
            raise typer.Exit(1)

    for bare in bare_repos:
        rn = bare.name[:-4]

        source_result = subprocess.run(
            ["git", "--git-dir", str(bare), "config", "jedicave.sourceRepo"],
            capture_output=True, text=True,
        )
        source_repo = source_result.stdout.strip()

        if not source_repo or not Path(source_repo).is_dir():
            err_console.print(f"[yellow]Skipping '{rn}': source repo not found at {source_repo}[/]")
            continue

        push_cmd = ["git", "push"] + (["--force"] if force else [])
        if all_branches:
            result = run(push_cmd + [str(bare), "--all"],
                         cwd=Path(source_repo), check=False)
        else:
            # Push the current branch of the source repo
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=source_repo, capture_output=True, text=True,
            )
            branch = branch_result.stdout.strip()
            if not branch or branch == "HEAD":
                err_console.print(f"[yellow]Skipping '{rn}': detached HEAD, use --all[/]")
                continue
            result = run(push_cmd + [str(bare),
                          f"refs/heads/{branch}:refs/heads/{branch}"],
                         cwd=Path(source_repo), check=False)

        if result.returncode == 0:
            # Update seeded count
            count_result = subprocess.run(
                ["git", "--git-dir", str(bare), "rev-list", "--all", "--count"],
                capture_output=True, text=True,
            )
            if count_result.returncode == 0:
                run(["git", "--git-dir", str(bare), "config",
                     "jedicave.seededCount", count_result.stdout.strip()], check=False)
            console.print(f"[green]Reseeded '{rn}'[/]")
        else:
            err_console.print(
                f"[red]Failed to reseed '{rn}' — push rejected (branch may have diverged).[/]\n"
                "  Re-run with [bold]--force[/] to overwrite: [dim]jedi reseed --force ...[/]"
            )


@app.command()
def unseed(
    repo_name: Annotated[str, typer.Argument(help="Repo name to remove")],
    name: Annotated[Optional[str], typer.Option("--cave", "-c", help="Cave name", autocompletion=complete_cave_name)] = None,
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation")] = False,
):
    """Remove a seeded bare repo from the cave."""
    name, d = resolve_cave(name)
    repos_dir = (d / "repos").resolve()
    bare_path = (repos_dir / f"{repo_name}.git").resolve()

    # Prevent path traversal (e.g. "../../something")
    if not str(bare_path).startswith(str(repos_dir) + "/"):
        err_console.print(f"[red]Invalid repo name '{repo_name}'[/]")
        raise typer.Exit(1)

    if not bare_path.exists():
        err_console.print(f"[red]No seeded repo '{repo_name}' in cave '{name}'[/]")
        raise typer.Exit(1)

    # Warn about unfetched agent commits
    count_result = subprocess.run(
        ["git", "--git-dir", str(bare_path), "rev-list", "--all", "--count"],
        capture_output=True, text=True,
    )
    commit_count = count_result.stdout.strip() if count_result.returncode == 0 else "unknown"
    console.print(f"  [yellow]Bare repo has {commit_count} commit(s). This may include unharvested agent work.[/]")

    if yes or typer.confirm(f"Unseed repo '{repo_name}' from cave '{name}'?", default=False):
        trash_dir = d / ".trash"
        trash_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        trash_dest = trash_dir / f"{repo_name}.git.{timestamp}"
        bare_path.rename(trash_dest)
        console.print(f"[green]Unseeded '{repo_name}' from cave '{name}'[/]")
        console.print(f"  Moved to: {trash_dest}")
        console.print(f"  To restore: mv {trash_dest} {bare_path}")
    else:
        console.print("Aborted")


@app.command()
def harvest(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
):
    """Show agent commits in cave repos and how to fetch them."""
    name, d = resolve_cave(name)
    repos_dir = d / "repos"

    if not repos_dir.exists():
        console.print("No repos seeded. Run: jedi seed <repo-path>")
        return

    bare_repos = sorted(
        p for p in repos_dir.iterdir()
        if p.is_dir() and p.name.endswith(".git")
    )

    if not bare_repos:
        console.print("No repos seeded. Run: jedi seed <repo-path>")
        return

    running = is_cave_running(d)

    # Track pre-harvest branch tips per repo to identify new agent commits
    pre_harvest_tips = {}  # repo_name -> set of commit hashes

    def get_branch_tips(bare_path):
        """Get all branch tip commit hashes from a bare repo."""
        result = subprocess.run(
            ["git", "--git-dir", str(bare_path), "rev-parse", "--branches"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return set(result.stdout.strip().splitlines())
        return set()

    # Snapshot current branch tips before sync
    for bare in bare_repos:
        rn = bare.name[:-4]
        pre_harvest_tips[rn] = get_branch_tips(bare)

    # Sync agent commits from container into bare repos via bundle
    if running:
        container_id = subprocess.run(
            ["docker", "compose", "ps", "-q", COMPOSE_SERVICE],
            cwd=d, capture_output=True, text=True,
        ).stdout.strip()

        for bare in bare_repos:
            rn = bare.name[:-4]
            workdir = f"/workspace/{rn}"
            bundle_path = f"/tmp/{rn}.bundle"

            # Check if repo exists in container
            check = subprocess.run(
                ["docker", "compose", "exec", COMPOSE_SERVICE, "test", "-d", workdir],
                cwd=d, capture_output=True,
            )
            if check.returncode != 0:
                continue

            # Create bundle inside container
            result = subprocess.run(
                ["docker", "compose", "exec", "-w", workdir, COMPOSE_SERVICE,
                 "git", "bundle", "create", bundle_path, "--all"],
                cwd=d, capture_output=True, text=True,
            )
            if result.returncode != 0:
                err_console.print(f"[yellow]Could not bundle '{rn}': {result.stderr.strip()}[/]")
                continue

            # Copy bundle out via docker cp
            host_bundle = d / "repos" / f"{rn}.bundle"
            result = subprocess.run(
                ["docker", "cp", f"{container_id}:{bundle_path}", str(host_bundle)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                err_console.print(f"[yellow]Could not extract bundle for '{rn}'[/]")
                continue

            # Fetch bundle into bare repo (update branch refs)
            result = subprocess.run(
                ["git", "--git-dir", str(bare), "fetch", str(host_bundle),
                 "+refs/heads/*:refs/heads/*"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                fetched = [l for l in result.stderr.strip().splitlines() if "->" in l]
                if fetched:
                    console.print(f"[green]Harvested new commits:[/] {rn}")

            # Clean up bundle
            host_bundle.unlink(missing_ok=True)

    for bare in bare_repos:
        repo_name = bare.name[:-4]
        old_tips = pre_harvest_tips.get(repo_name, set())

        # Build exclusion args: exclude commits reachable from pre-harvest tips
        # This shows only commits added by the agent
        exclude_args = [f"^{tip}" for tip in old_tips]

        # Count new agent commits (not reachable from pre-harvest tips)
        if old_tips:
            count_result = subprocess.run(
                ["git", "--git-dir", str(bare), "rev-list", "--all", "--count"] + exclude_args,
                capture_output=True, text=True,
            )
            new_count = int(count_result.stdout.strip()) if count_result.returncode == 0 else 0

            agent_result = subprocess.run(
                ["git", "--git-dir", str(bare), "log", "--all",
                 "--oneline", "--graph", "-30"] + exclude_args,
                capture_output=True, text=True,
            )
            agent_log = agent_result.stdout.strip() if agent_result.returncode == 0 else ""
        else:
            new_count = 0
            agent_log = ""

        # Count total commits
        total_result = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-list", "--all", "--count"],
            capture_output=True, text=True,
        )
        total_count = int(total_result.stdout.strip()) if total_result.returncode == 0 else 0

        # Get seeded commit count (set during jedi seed)
        seeded_result = subprocess.run(
            ["git", "--git-dir", str(bare), "config", "jedicave.seededCount"],
            capture_output=True, text=True,
        )
        has_seeded_count = seeded_result.returncode == 0 and seeded_result.stdout.strip()
        seeded_count = int(seeded_result.stdout.strip()) if has_seeded_count else None

        console.print(f"\n[cyan]{repo_name}[/]")

        if new_count > 0:
            summary = f"  [green]+{new_count} new[/]"
            if seeded_count is not None:
                agent_total = total_count - seeded_count
                if agent_total > new_count:
                    summary += f" ({agent_total} by agent, {total_count} total)"
                else:
                    summary += f" ({total_count} total)"
            else:
                summary += f" ({total_count} total)"
            console.print(summary)
            console.print(agent_log)
        elif old_tips:
            if seeded_count is not None:
                agent_total = total_count - seeded_count
                if agent_total > 0:
                    console.print(f"  [dim]No new commits[/] ({agent_total} by agent, {total_count} total)")
                else:
                    console.print(f"  [dim]No new commits[/] ({total_count} total)")
            else:
                console.print(f"  [dim]No new commits[/] ({total_count} total)")
        else:
            # No pre-harvest tips means repo was empty before — all commits are agent's
            result = subprocess.run(
                ["git", "--git-dir", str(bare), "log", "--all",
                 "--oneline", "--graph", "-30"],
                capture_output=True, text=True,
            )
            if result.stdout.strip():
                console.print(f"  [green]+{total_count} new[/]")
                console.print(result.stdout.strip())
            else:
                console.print("  (no commits)")

    console.print(f"\n  To fetch into your repos:")
    console.print(f"    jedi fetch {name}")

    if running:
        console.print(f"\n[dim]Tip: use 'jedi diff' to see uncommitted changes still inside the container[/]")
    else:
        console.print(f"\n[yellow]Cave is stopped — only showing previously harvested commits.[/]")
        console.print(f"[yellow]Run 'jedi harvest' while the cave is running to sync agent commits.[/]")


@app.command()
def diff(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    repo_name: Annotated[Optional[str], typer.Option("--repo", "-r", help="Specific repo (default: all)")] = None,
    stat: Annotated[bool, typer.Option("--stat", help="Show diffstat instead of full diff")] = False,
):
    """Show uncommitted changes in workspace repos (requires running cave)."""
    name, d = resolve_cave(name)

    if not is_cave_running(d):
        err_console.print(f"[red]Cave '{name}' is not running.[/] Start it first: jedi up {name}")
        raise typer.Exit(1)

    # Discover repos inside the container
    if repo_name:
        repos = [repo_name]
    else:
        result = subprocess.run(
            ["docker", "compose", "exec", COMPOSE_SERVICE,
             "sh", "-c", "ls -d /workspace/*/.git 2>/dev/null | xargs -I{} dirname {}"],
            cwd=d, capture_output=True, text=True,
        )
        if not result.stdout.strip():
            console.print("No git repos found in /workspace/")
            return
        repos = [Path(p).name for p in result.stdout.strip().splitlines()]

    for rn in repos:
        workdir = f"/workspace/{rn}"

        # Check repo exists
        check = subprocess.run(
            ["docker", "compose", "exec", COMPOSE_SERVICE, "test", "-d", workdir],
            cwd=d, capture_output=True,
        )
        if check.returncode != 0:
            err_console.print(f"[red]Repo '{rn}' not found in /workspace/[/]")
            continue

        console.print(f"\n[cyan bold]{rn}[/]")

        # Tracked changes
        diff_cmd = "git diff HEAD --stat" if stat else "git diff HEAD"
        result = subprocess.run(
            ["docker", "compose", "exec", "-w", workdir, COMPOSE_SERVICE,
             "sh", "-c", diff_cmd],
            cwd=d, capture_output=True, text=True,
        )
        if result.stdout.strip():
            console.print(result.stdout.rstrip())
        else:
            console.print("  [dim]no tracked changes[/]")

        # Untracked files
        result = subprocess.run(
            ["docker", "compose", "exec", "-w", workdir, COMPOSE_SERVICE,
             "git", "ls-files", "--others", "--exclude-standard"],
            cwd=d, capture_output=True, text=True,
        )
        untracked = result.stdout.strip().splitlines()
        if untracked:
            console.print(f"\n  [yellow]Untracked files ({len(untracked)}):[/]")
            for f in untracked:
                console.print(f"    {f}")


@app.command()
def fetch(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    repo_name: Annotated[Optional[str], typer.Option("--repo", "-r", help="Specific repo (default: all)")] = None,
):
    """Fetch agent commits from cave into your source repos."""
    name, d = resolve_cave(name)
    repos_dir = d / "repos"

    if not repos_dir.exists():
        console.print("No repos seeded. Run: jedi seed <repo-path>")
        return

    bare_repos = sorted(
        p for p in repos_dir.iterdir()
        if p.is_dir() and p.name.endswith(".git")
    )

    if not bare_repos:
        console.print("No repos seeded. Run: jedi seed <repo-path>")
        return

    # If cave is running, sync first (same as harvest)
    if is_cave_running(d):
        container_id = subprocess.run(
            ["docker", "compose", "ps", "-q", COMPOSE_SERVICE],
            cwd=d, capture_output=True, text=True,
        ).stdout.strip()

        for bare in bare_repos:
            rn = bare.name[:-4]
            if repo_name and rn != repo_name:
                continue
            workdir = f"/workspace/{rn}"
            bundle_path = f"/tmp/{rn}.bundle"

            check = subprocess.run(
                ["docker", "compose", "exec", COMPOSE_SERVICE, "test", "-d", workdir],
                cwd=d, capture_output=True,
            )
            if check.returncode != 0:
                continue

            result = subprocess.run(
                ["docker", "compose", "exec", "-w", workdir, COMPOSE_SERVICE,
                 "git", "bundle", "create", bundle_path, "--all"],
                cwd=d, capture_output=True, text=True,
            )
            if result.returncode != 0:
                continue

            host_bundle = d / "repos" / f"{rn}.bundle"
            subprocess.run(
                ["docker", "cp", f"{container_id}:{bundle_path}", str(host_bundle)],
                capture_output=True, text=True,
            )

            subprocess.run(
                ["git", "--git-dir", str(bare), "fetch", str(host_bundle),
                 "+refs/heads/*:refs/heads/*"],
                capture_output=True, text=True,
            )
            host_bundle.unlink(missing_ok=True)

    for bare in bare_repos:
        rn = bare.name[:-4]
        if repo_name and rn != repo_name:
            continue

        source_result = subprocess.run(
            ["git", "--git-dir", str(bare), "config", "jedicave.sourceRepo"],
            capture_output=True, text=True,
        )
        source_repo = source_result.stdout.strip()

        if not source_repo or not Path(source_repo).is_dir():
            err_console.print(
                f"[red]No source repo for '{rn}'.[/] "
                f"Fetch manually:\n  git fetch {bare}"
            )
            continue

        result = run(
            ["git", "fetch", str(bare), "+refs/heads/*:refs/remotes/cave/*"],
            cwd=Path(source_repo), check=False,
        )
        if result.returncode != 0:
            err_console.print(f"[red]Failed to fetch '{rn}'[/]")
            continue

        # Show what's on cave/ branches
        branch_result = subprocess.run(
            ["git", "branch", "-r", "--list", "cave/*", "--format=%(refname:short)"],
            cwd=source_repo, capture_output=True, text=True,
        )
        branches = branch_result.stdout.strip().splitlines()

        console.print(f"\n[green]Fetched '{rn}'[/] into {source_repo}")
        if branches:
            console.print(f"  Remote branches:")
            for b in branches:
                console.print(f"    {b}")
        console.print(f"\n  Review:")
        console.print(f"    cd {source_repo}")
        console.print(f"    git log --oneline --graph cave/ --not HEAD")
        console.print(f"    git diff HEAD..cave/<branch>")


@app.command("dir")
def dir_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
):
    """Print cave directory path."""
    _name, d = resolve_cave(name)
    print(d)


@app.command()
def show(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
):
    """Show cave overview."""
    name, d = resolve_cave(name)

    running = is_cave_running(d)
    status = "[green]running[/]" if running else "[dim]stopped[/]"
    console.print(f"[cyan]{name}[/]  {status}")
    console.print(f"  Path: {d}")

    # Repos
    repos_dir = d / "repos"
    if repos_dir.exists():
        bare_repos = sorted(
            p for p in repos_dir.iterdir()
            if p.is_dir() and p.name.endswith(".git")
        )
        if bare_repos:
            console.print(f"\n  Repos ({len(bare_repos)}):")
            for bare in bare_repos:
                repo_name = bare.name[:-4]
                source_result = subprocess.run(
                    ["git", "--git-dir", str(bare), "config", "jedicave.sourceRepo"],
                    capture_output=True, text=True,
                )
                source = source_result.stdout.strip()
                # Count commits
                count_result = subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-list", "--all", "--count"],
                    capture_output=True, text=True,
                )
                count = count_result.stdout.strip() or "0"
                # Branches
                branch_result = subprocess.run(
                    ["git", "--git-dir", str(bare), "branch", "--format=%(refname:short)"],
                    capture_output=True, text=True,
                )
                branches = branch_result.stdout.strip().splitlines()
                branch_str = ", ".join(branches) if branches else "none"
                console.print(f"    [cyan]{repo_name}[/]  {count} commits  [{branch_str}]")
                if source:
                    console.print(f"      source: {source}")

    # Volumes from compose
    compose_file = d / "compose.yml"
    override_file = d / "compose.override.yml"
    volume_lines = []
    for f in [compose_file, override_file]:
        if f.exists():
            for line in f.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("- ") and ":" in stripped and not stripped.startswith("- TZ"):
                    volume_lines.append((f.name, stripped[2:]))

    if volume_lines:
        console.print(f"\n  Volumes:")
        for source_file, vol in volume_lines:
            label = f" [dim]({source_file})[/]" if source_file == "compose.override.yml" else ""
            console.print(f"    {vol}{label}")


@app.command()
def guide():
    """Step-by-step walkthrough for setting up a new cave."""
    console.print(
        "\n"
        "[bold]Setting up a new jedicave (reproducible offline-capable sandboxed container)[/]\n"
        "\n"
        "[bold cyan]1. Create the cave[/]\n"
        "   jedi init my-cave\n"
        "   Creates scaffolding at ~/.config/jedicaves/my-cave/\n"
        "\n"
        "[bold cyan]2. Configure the flake[/]\n"
        "   Edit the generated flake.nix to add your project inputs\n"
        "   and devShells. The file has commented examples.\n"
        "   jedi show my-cave         # see cave path and details\n"
        "\n"
        "[bold cyan]3. Build the container image[/]\n"
        "   jedi build my-cave\n"
        "   Builds with Nix and loads the image into Docker.\n"
        "\n"
        "[bold cyan]4. Seed your source code[/]\n"
        "   jedi seed ~/code/my-project my-cave\n"
        "   Pushes a branch into a bare repo inside the cave.\n"
        "   The container auto-clones it to /workspace/my-project.\n"
        "\n"
        "[bold cyan]5. Launch[/]\n"
        "   jedi up my-cave            # start in background\n"
        "   jedi enter my-cave         # attach a shell\n"
        "   [dim]or[/]\n"
        "   jedi shell my-cave         # one-shot ephemeral shell\n"
        "\n"
        "[bold cyan]6. Check progress & harvest results[/]\n"
        "   jedi diff my-cave            # uncommitted changes inside container\n"
        "   jedi harvest my-cave         # committed work overview\n"
        "   jedi fetch my-cave           # fetch agent commits into your repos\n"
        "\n"
        "[bold cyan]Other useful commands[/]\n"
        "   jedi list                  # list all caves\n"
        "   jedi firewall status       # check firewall state\n"
        "   jedi inputs my-cave        # show locked flake inputs\n"
        "   jedi logs -f my-cave       # follow container logs\n"
        "   jedi destroy my-cave       # tear down a cave\n"
    )


@app.command()
def logs(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    follow: Annotated[bool, typer.Option("-f", "--follow", help="Follow log output")] = False,
    tail: Annotated[Optional[int], typer.Option("-n", "--tail", help="Number of lines from end")] = None,
):
    """Show cave container logs."""
    name, d = resolve_cave(name)
    cmd = ["docker", "compose", "--project-directory", str(d), "logs", COMPOSE_SERVICE]
    if follow:
        cmd.append("-f")
    if tail is not None:
        cmd.extend(["--tail", str(tail)])
    os.execvp("docker", cmd)


@app.command()
def destroy(
    name: Annotated[str, typer.Argument(help="Cave name", autocompletion=complete_cave_name)],
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation")] = False,
):
    """Delete a cave."""
    name, d = resolve_cave(name)

    # Check for repos with commits that may not have been fetched
    repos_dir = d / "repos"
    if repos_dir.exists():
        bare_repos = [p for p in repos_dir.iterdir() if p.is_dir() and p.name.endswith(".git")]
        if bare_repos:
            console.print(f"  [yellow]Cave has {len(bare_repos)} seeded repo(s). Make sure you've fetched all agent work first.[/]")
            for bare in bare_repos:
                count = subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-list", "--all", "--count"],
                    capture_output=True, text=True,
                )
                commits = count.stdout.strip() if count.returncode == 0 else "?"
                console.print(f"    {bare.name[:-4]}: {commits} commit(s)")

    subprocess.run(["docker", "compose", "down"], cwd=d, capture_output=True)

    if yes or typer.confirm(f"Destroy cave '{name}'?", default=False):
        trash_dir = CAVES_DIR / ".trash"
        trash_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        trash_dest = trash_dir / f"{name}.{timestamp}"
        d.rename(trash_dest)
        console.print(f"[green]Cave '{name}' destroyed[/]")
        console.print(f"  Moved to: {trash_dest}")
        console.print(f"  To restore: mv {trash_dest} {d}")
    else:
        console.print("Aborted")


if __name__ == "__main__":
    app(prog_name="jedi")
