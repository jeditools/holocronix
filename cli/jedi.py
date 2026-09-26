#!/usr/bin/env python3
"""jedi — CLI for managing jedicaves (sandboxed containers)."""

import json
import os
import shutil
import socket
import re
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
DATA_DIR = Path(os.environ.get("JEDI_DATA_DIR", Path(__file__).resolve().parent.parent / "config"))
COMPOSE_SERVICE = "shell"
HOLOCRONIX_URL_DEFAULT = "github:jeditools/holocronix"

# Image naming.  Every cave gets its own Docker repository, so building one
# cave never replaces the image another cave runs.  Caves created before this
# scheme shared LEGACY_IMAGE; `jedi up` adopts that image once, see
# _ensure_image().
LEGACY_IMAGE = "jedicave:latest"


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
        if d.is_dir() and ((d / "flake.nix").exists() or (d / "cave.scm").exists())
    )


# --- Backends ---
#
# A cave is Nix-backed when defined by flake.nix, Guix-backed when defined by
# cave.scm.  Everything past `jedi build` (compose, firewall, seed, harvest)
# is backend-agnostic: both produce a jedicave:latest image for Docker.

def cave_backend(d: Path) -> str:
    """'guix' when the cave is defined by cave.scm, else 'nix'."""
    return "guix" if (d / "cave.scm").exists() else "nix"


def _holocronix_dir() -> Path | None:
    """Local holocronix checkout, if known: HOLOCRONIX_DIR, or a path:
    HOLOCRONIX_URL (which the holocronix devShell sets)."""
    explicit = os.environ.get("HOLOCRONIX_DIR")
    if explicit:
        return Path(explicit)
    url = os.environ.get("HOLOCRONIX_URL", "")
    if url.startswith("path:"):
        return Path(url[len("path:"):])
    return None


def _holocronix_channel_url(url: str) -> str:
    """Turn a flake-style holocronix URL into a git URL usable as a Guix channel."""
    if url.startswith("path:"):
        return url[len("path:"):]
    if url.startswith("github:"):
        owner, repo = url[len("github:"):].split("/")[:2]
        return f"https://github.com/{owner}/{repo}"
    if url.startswith("git+"):
        return url[len("git+"):].split("?")[0]
    return url


def _guix_load_path_args() -> list[str]:
    """-L for the local holocronix checkout, so its working tree takes
    precedence over the holocronix channel pinned in channels.scm."""
    d = _holocronix_dir()
    return ["-L", str(d / "guix")] if d and (d / "guix").is_dir() else []


def _guix_cmd(d: Path, *args: str) -> list[str]:
    """A guix command line, under time-machine when the cave pins channels."""
    cmd = ["guix"]
    if (d / "channels.scm").exists():
        cmd += ["time-machine", "-C", "channels.scm", "--"]
    return cmd + list(args)


def _channels_holocronix_url(d: Path) -> str | None:
    """The holocronix channel URL recorded in the cave's channels.scm."""
    f = d / "channels.scm"
    if not f.exists():
        return None
    m = re.search(r"\(name 'holocronix\)\s*\(url \"([^\"]+)\"\)", f.read_text())
    return m.group(1) if m else None


def _mask_scheme(text: str) -> str:
    """TEXT with comment and string contents blanked out, same length, so
    offsets still line up.  Scanning the mask keeps `(channel` inside a
    comment or a string from being mistaken for a real form."""
    out = list(text)
    i, in_string = 0, False
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                out[i] = " "
                if i + 1 < len(text):
                    out[i + 1] = " "
                i += 2
                continue
            if c == '"':
                in_string = False
            else:
                out[i] = " "
        elif c == '"':
            in_string = True
        elif c == ";":
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            out[i:end] = " " * (end - i)
            i = end
            continue
        i += 1
    return "".join(out)


def _channel_blocks(text: str) -> list[tuple[str, int, int]]:
    """Locate each `(channel ...)` form in Scheme TEXT.  Returns
    (channel-name, start, end) offsets, outermost forms only."""
    masked = _mask_scheme(text)
    blocks: list[tuple[str, int, int]] = []
    # The lookahead matters: without it `(channels/guix.scm)` in a comment
    # would match, and so would any other symbol starting with "channel".
    for m in re.finditer(r"\(channel(?=[\s()])", masked):
        start = m.start()
        if blocks and start < blocks[-1][2]:
            continue                            # nested in a form already taken
        depth, j = 0, start
        while j < len(masked):
            if masked[j] == "(":
                depth += 1
            elif masked[j] == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        name = re.search(r"\(name\s+'([\w-]+)\)", masked[start:j])
        blocks.append((name.group(1) if name else "", start, j))
    return blocks


def _guix_describe_channels() -> dict[str, str]:
    """Channel name to source text, from the running Guix."""
    result = subprocess.run(["guix", "describe", "-f", "channels"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        err_console.print(f"[red]guix describe failed:[/]\n{result.stderr.strip()}")
        raise typer.Exit(1)
    text = result.stdout
    return {name: text[start:end]
            for name, start, end in _channel_blocks(text)}


def _write_guix_channels(d: Path, name: str, holocronix_url: str,
                         refresh_guix: bool = False) -> list[str]:
    """Write channels.scm and return the names of the channels kept as they
    were.

    jedi owns exactly one entry, `holocronix`, pinned to the checkout's HEAD
    when it is a local git repository.  Every other channel belongs to
    whoever put it there: a project's own channel, a deliberately older `guix`
    pin that another channel's packages were built against.  Re-pinning those
    can silently turn a four-minute build into an overnight one, so on an
    existing file only the holocronix form is rewritten, in place; comments,
    layout and every other form survive untouched.  With REFRESH_GUIX the
    `guix` form is refreshed from `guix describe` as well.
    """
    def holocronix_form(indent: int) -> str:
        pad = " " * indent
        pin = ""
        if Path(holocronix_url).is_dir():
            head = subprocess.run(
                ["git", "-C", holocronix_url, "rev-parse", "HEAD"],
                capture_output=True, text=True)
            if head.returncode == 0:
                pin = f'\n{pad}(commit "{head.stdout.strip()}")'
        return (f"(channel\n{pad}(name 'holocronix)\n"
                f'{pad}(url "{holocronix_url}")\n'
                f'{pad}(branch "main"){pin})')

    path = d / "channels.scm"
    if not path.exists():
        rest = [text for chan, text in _guix_describe_channels().items()
                if chan != "holocronix"]
        channels = "\n      ".join([holocronix_form(7)] + rest)
        path.write_text(CHANNELS_TEMPLATE.format(name=name, channels=channels))
        return []

    text = path.read_text()
    blocks = _channel_blocks(text)
    if not any(chan == "holocronix" for chan, _, _ in blocks):
        err_console.print(
            f"[red]{path} has no holocronix channel;[/] leaving it alone")
        raise typer.Exit(1)

    fresh = _guix_describe_channels() if refresh_guix else {}
    kept: list[str] = []
    edits: list[tuple[int, int, str]] = []
    for chan, start, end in blocks:
        if chan == "holocronix":
            # Indent the fields to match where the form already sits, the
            # way `guix describe -f channels` lays them out: one column in.
            column = start - (text.rfind("\n", 0, start) + 1)
            edits.append((start, end, holocronix_form(column + 1)))
        elif chan == "guix" and "guix" in fresh:
            edits.append((start, end, fresh["guix"].strip()))
        else:
            kept.append(chan)
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    path.write_text(text)
    return kept


def _print_guix_channels(name: str, d: Path) -> None:
    f = d / "channels.scm"
    if not f.exists():
        console.print(f"Cave '{name}' has no channels.scm; it builds with the guix on PATH")
        return
    text = f.read_text()
    table = Table(title=f"Channels for cave '{name}'")
    table.add_column("Channel", style="cyan")
    table.add_column("URL")
    table.add_column("Branch", style="dim")
    table.add_column("Commit", style="dim")
    names = list(re.finditer(r"\(name '([\w-]+)\)", text))
    for i, m in enumerate(names):
        block = text[m.end():names[i + 1].start() if i + 1 < len(names) else len(text)]
        field = lambda key: (re.search(rf'\({key} "([^"]+)"\)', block) or [None, ""])[1]
        table.add_row(m.group(1), field("url"), field("branch"), field("commit")[:12])
    console.print(table)


def image_slug(cave_name: str) -> str:
    """Cave name as a Docker repository component: lowercase, and only the
    characters a repository path allows."""
    slug = re.sub(r"[^a-z0-9._-]", "-", cave_name.lower()).strip("-._")
    return slug or "cave"


def image_ref(cave_name: str) -> str:
    """Docker image reference for a cave, e.g. jedicave-dagobah:latest."""
    return f"jedicave-{image_slug(cave_name)}:latest"


def _image_exists(ref: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", ref],
                          capture_output=True).returncode == 0


def _ensure_image(name: str) -> None:
    """Fail early, with a useful message, when a cave's image is missing.

    Caves built before per-cave image names all produced LEGACY_IMAGE.  Adopt
    it once, so an existing cave keeps starting the image it was already
    running; the next `jedi build` replaces it with a cave-specific one.
    """
    ref = image_ref(name)
    if _image_exists(ref):
        return
    if _image_exists(LEGACY_IMAGE):
        console.print(
            f"[yellow]Cave '{name}' has no image of its own yet; "
            f"adopting {LEGACY_IMAGE}.[/]\n"
            f"[dim]  Caves used to share one image tag. Run 'jedi build "
            f"{name}' to give this cave its own.[/]")
        run(["docker", "tag", LEGACY_IMAGE, ref])
        return
    err_console.print(f"[red]No image for cave '{name}'.[/] Run: jedi build {name}")
    raise typer.Exit(1)


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


def run(cmd: list[str], cwd: Path | None = None, check: bool = True,
        env: dict | None = None) -> subprocess.CompletedProcess:
    console.print(f"[dim]  {' '.join(cmd)}[/]")
    return subprocess.run(cmd, cwd=cwd, check=check, env=env)


def _read_env_project_name(d: Path) -> str | None:
    """Read COMPOSE_PROJECT_NAME from the cave's .env (set by rename)."""
    env_file = d / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        s = line.strip()
        if s.startswith("COMPOSE_PROJECT_NAME="):
            return s.split("=", 1)[1]
    return None


def compose_project(d: Path, cave_name: str, session: str = "default") -> str:
    """Compose project name for a (cave, session).

    Default session keeps the bare cave name to preserve existing volumes;
    other sessions are namespaced as ``<cave>-<session>``. The default
    session also honours the .env COMPOSE_PROJECT_NAME override used by
    ``jedi rename`` while the cave is running.
    """
    if session == "default":
        override = _read_env_project_name(d)
        if override:
            return override
        return cave_name
    return f"{cave_name}-{session}"


def compose_env(session: str = "default") -> dict:
    """Process env for ``docker compose`` calls — passes JEDI_SESSION
    so compose.yml label substitution resolves correctly."""
    env = os.environ.copy()
    env["JEDI_SESSION"] = session
    return env


def compose_cmd(d: Path, cave_name: str, session: str = "default") -> list[str]:
    """Base ``docker compose`` invocation scoped to one session."""
    return ["docker", "compose", "-p", compose_project(d, cave_name, session)]


def is_session_running(d: Path, cave_name: str, session: str = "default") -> bool:
    """True if the named session has a running shell container."""
    result = subprocess.run(
        compose_cmd(d, cave_name, session) + ["ps", "-q", COMPOSE_SERVICE],
        cwd=d, capture_output=True, text=True, env=compose_env(session),
    )
    return bool(result.stdout.strip())


def is_cave_running(d: Path, cave_name: str | None = None) -> bool:
    """True if any session of the cave is running."""
    if cave_name is None:
        cave_name = d.name
    return any(s["running"] for s in list_sessions(d, cave_name))


def list_sessions(d: Path, cave_name: str) -> list[dict]:
    """List sessions for a cave by inspecting docker container labels.

    Returns ``[{session, running}, ...]`` sorted by session name.
    """
    result = subprocess.run(
        ["docker", "ps", "-a",
         "--filter", f"label=jedi.cave={cave_name}",
         "--filter", f"label=com.docker.compose.service={COMPOSE_SERVICE}",
         "--format", '{{.Label "jedi.session"}}|{{.State}}'],
        capture_output=True, text=True,
    )
    seen: dict[str, bool] = {}
    for line in result.stdout.strip().splitlines():
        if "|" not in line:
            continue
        session, state = line.split("|", 1)
        if not session:
            session = "default"
        seen.setdefault(session, False)
        if state == "running":
            seen[session] = True
    return [{"session": s, "running": r} for s, r in sorted(seen.items())]


def complete_session_name(ctx: typer.Context, incomplete: str) -> list[str]:
    """Tab-completion for --session: pulls running sessions of the resolved cave."""
    name = ctx.params.get("name")
    if not name:
        caves = _list_caves()
        active = CAVES_DIR / ".active"
        if active.exists() and active.read_text().strip() in caves:
            name = active.read_text().strip()
        elif len(caves) == 1:
            name = caves[0]
        else:
            return []
    d = CAVES_DIR / name
    if not d.is_dir():
        return []
    return [s["session"] for s in list_sessions(d, name) if s["session"].startswith(incomplete)]


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


def check_deps(backend: str = "nix") -> None:
    missing = []
    if backend == "guix":
        if not shutil.which("guix"):
            missing.append("guix (https://guix.gnu.org/download/)")
    elif not shutil.which("nix"):
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
      # Image name for this cave. Keep it unique per cave: caves that share
      # a name share a Docker tag, so building one replaces the other's image.
      name = "jedicave-{slug}";

      # List your project devShells here:
      # projectShells = [
      #   inputs.my-project.devShells.${{system}}.default
      # ];
    }};
  }};
}}
"""

CAVE_SCM_TEMPLATE = """\
;; Jedicave: {name}  (Guix backend)
;;
;; Edit the package list below, then build:
;;   jedi build {name}
;;
;; `jedi build` evaluates this file with `guix build -f cave.scm`, under the
;; channels pinned in channels.scm, and loads the resulting image into
;; Docker.  It must return the image, which `jedicave-image` does.  See
;; guix/README.md in holocronix for the full option list.

(use-modules (holocronix jedicave)
             (holocronix cargo-vendor)
             (gnu packages))

(jedicave-image
 ;; Image name for this cave, matching what compose.yml expects.  Keep it
 ;; unique per cave: caves that share a name share a Docker tag, so building
 ;; one would replace the other's image.
 #:name "jedicave-{slug}"

 ;; Project toolchain, added to the jedicave base tools
 ;; (%jedicave-base-specs).  Package specs as `guix install` takes them.
 #:extra-packages
 (append
  (specifications->packages
   '(;; "rust" "rust:cargo"
     ;; "go" "gopls"
     ))
  ;; Baked Rust dependencies: every crate in a Cargo.lock, so cargo builds
  ;; offline.  Point at the project's lockfile (one project per cave):
  (list
   ;; (cargo-vendor "my-project" "/home/yoda/code/my-project/Cargo.lock")
   ))

 ;; With cargo-vendor above, put its config at / so cargo finds it:
 ;; #:symlinks '(("/.cargo" . "share/cargo-config"))

 ;; Extra environment variables:
 ;; #:env '(("CARGO_ALIAS_XTASK" . "run --package xtask --"))
 )
"""

CHANNELS_TEMPLATE = """\
;; Channels for jedicave {name}.
;;
;; `jedi build` runs `guix time-machine -C channels.scm -- build ...`, so the
;; image is built with exactly these commits: the Guix counterpart of
;; flake.lock.  Delete this file to build with whatever `guix` is on PATH
;; instead.
;;
;; Add other channels here as needed -- a project's own, baobit, ... -- and
;; edit any commit by hand.  `jedi update` re-pins only the holocronix entry
;; and leaves everything else exactly as written, because another channel's
;; packages are only in your store under the `guix` commit they were built
;; against: moving that pin can turn a short build into an overnight one.
;; `jedi update --guix` re-pins the guix entry too, when you do want it.

(list {channels})
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
    image: {image_ref(name)}
    init: true
    stdin_open: true
    tty: true
    cap_add:
      - NET_ADMIN
      - NET_RAW
    security_opt:
      - no-new-privileges:true
      - seccomp:seccomp.json
    working_dir: /workspace
    labels:
      jedi.cave: {name}
      jedi.session: ${{JEDI_SESSION:-default}}
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
    dst_seccomp = d / "seccomp.json"
    dst_seccomp.unlink(missing_ok=True)
    shutil.copy2(DATA_DIR / "seccomp.json", dst_seccomp)

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


# --- Guix model queries ---
#
# `jedi guix <op>` answers questions about the Guix package model as JSON by
# running cli/query.scm under `guix repl`.  Nothing is built.  With --cave
# the query runs under that cave's pinned channels, so the answer is about
# the Guix the cave builds with, not the one on PATH.
#
# query.scm lives beside this file, not under guix/: that directory is the
# holocronix channel, and Guix loads every .scm in a channel before compiling
# it, which would run the script's entry point and fail the channel build.

guix_app = typer.Typer(
    name="guix",
    help="Ask the Guix package model, as JSON: records, inputs, derivations, closures, graphs, lint.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(guix_app, name="guix")

SpecArg = Annotated[str, typer.Argument(
    help="Package spec as guix build takes it (hello, hello@2.12), or a Scheme expression in parentheses")]
ItemArg = Annotated[str, typer.Argument(help="Package spec or /gnu/store path")]
CaveOpt = Annotated[Optional[str], typer.Option(
    "--cave", "-c", help="Run under this Guix cave's pinned channels",
    autocompletion=complete_cave_name)]
SystemOpt = Annotated[Optional[str], typer.Option(
    "--system", help="Guix system type, e.g. aarch64-linux")]
TargetOpt = Annotated[Optional[str], typer.Option(
    "--target", help="Cross-compilation target triplet, e.g. aarch64-linux-gnu")]


def _query_script() -> Path:
    """query.scm from the local checkout, beside this file, or installed."""
    here = Path(__file__).resolve().parent
    candidates = [
        (_holocronix_dir() or here.parent) / "cli" / "query.scm",
        here / "query.scm",                     # running from the checkout
        DATA_DIR / "query.scm",                 # the installed jedi's share dir
        here.parent / "share" / "jedi" / "query.scm",
    ]
    for path in candidates:
        if path.exists():
            return path
    err_console.print("[red]query.scm not found.[/] Set HOLOCRONIX_DIR to a holocronix checkout")
    raise typer.Exit(1)


def _guix_query(cave: str | None, op: str, *args: str,
                system: str | None = None, target: str | None = None) -> None:
    """Run one query op and exit with its status; the JSON goes to stdout."""
    if not shutil.which("guix"):
        err_console.print("[red]guix not found on PATH[/] (https://guix.gnu.org/download/)")
        raise typer.Exit(1)
    script = _query_script()
    opts = [a for a in args if a]
    if system:
        opts.append(f"--system={system}")
    if target:
        opts.append(f"--target={target}")
    cwd: Path | None = None
    if cave:
        name, d = resolve_cave(cave)
        if cave_backend(d) != "guix":
            err_console.print(f"[red]Cave '{name}' is not a Guix cave[/]")
            raise typer.Exit(1)
        cmd = _guix_cmd(d, "repl", *_guix_load_path_args(), "--", str(script), op, *opts)
        cwd = d
    else:
        cmd = ["guix", "repl", *_guix_load_path_args(), "--", str(script), op, *opts]
    result = subprocess.run(cmd, cwd=cwd)
    raise typer.Exit(result.returncode)


@guix_app.command("show")
def guix_show(spec: SpecArg, cave: CaveOpt = None):
    """The package record: source, inputs, arguments, location."""
    _guix_query(cave, "show", spec)


@guix_app.command("inputs")
def guix_inputs(
    spec: SpecArg,
    implicit: Annotated[bool, typer.Option("--implicit", help="The bag: build, host and target inputs, implicit ones included")] = False,
    cave: CaveOpt = None,
    system: SystemOpt = None,
    target: TargetOpt = None,
):
    """Explicit inputs, or with --implicit everything the build system adds."""
    _guix_query(cave, "inputs", spec, "--implicit" if implicit else "",
                system=system, target=target)


@guix_app.command("derivation")
def guix_derivation(
    spec: SpecArg,
    no_grafts: Annotated[bool, typer.Option("--no-grafts", help="The ungrafted derivation")] = False,
    cave: CaveOpt = None,
    system: SystemOpt = None,
    target: TargetOpt = None,
):
    """Derivation path and output paths, and whether each output is built."""
    _guix_query(cave, "derivation", spec, "--no-grafts" if no_grafts else "",
                system=system, target=target)


@guix_app.command("plan")
def guix_plan(
    spec: SpecArg,
    no_substitutes: Annotated[bool, typer.Option("--no-substitutes", help="Do not ask substitute servers")] = False,
    no_grafts: Annotated[bool, typer.Option("--no-grafts", help="Plan the ungrafted build")] = False,
    cave: CaveOpt = None,
    system: SystemOpt = None,
    target: TargetOpt = None,
):
    """What a build would build or download. Nothing is built."""
    _guix_query(cave, "plan", spec,
                "--no-substitutes" if no_substitutes else "",
                "--no-grafts" if no_grafts else "",
                system=system, target=target)


@guix_app.command("references")
def guix_references(item: ItemArg, cave: CaveOpt = None):
    """Run-time references of a built store item or package."""
    _guix_query(cave, "references", item)


@guix_app.command("referrers")
def guix_referrers(item: ItemArg, cave: CaveOpt = None):
    """What in the store refers to a built item."""
    _guix_query(cave, "referrers", item)


@guix_app.command("size")
def guix_size(
    items: Annotated[list[str], typer.Argument(help="Package specs or /gnu/store paths")],
    cave: CaveOpt = None,
):
    """Closure sizes, from the local store or substitute information."""
    _guix_query(cave, "size", *items)


@guix_app.command("graph")
def guix_graph(
    spec: SpecArg,
    kind: Annotated[str, typer.Option("--type", "-t", help="package, bag, bag-emerged, bag-with-origins, reverse-package, reverse-bag, derivation, references, referrers, module")] = "package",
    depth: Annotated[Optional[int], typer.Option("--depth", help="Stop this many edges away from SPEC")] = None,
    cave: CaveOpt = None,
    system: SystemOpt = None,
    target: TargetOpt = None,
):
    """Nodes and edges of a graph slice, as guix graph would draw it."""
    _guix_query(cave, "graph", spec, f"--type={kind}",
                f"--depth={depth}" if depth is not None else "",
                system=system, target=target)


@guix_app.command("lint")
def guix_lint(
    spec: SpecArg,
    network: Annotated[bool, typer.Option("--network", help="Also run the checkers that need the network")] = False,
    checkers: Annotated[Optional[str], typer.Option("--checkers", help="Comma-separated checker names")] = None,
    cave: CaveOpt = None,
):
    """Lint warnings, local checkers by default."""
    _guix_query(cave, "lint", spec, "--network" if network else "",
                f"--checkers={checkers}" if checkers else "")


@guix_app.command("search")
def guix_search(
    pattern: Annotated[str, typer.Argument(help="Regexp matched against name, synopsis and description")],
    limit: Annotated[int, typer.Option("--limit", help="Most packages to list")] = 50,
    cave: CaveOpt = None,
):
    """Packages matching a regexp."""
    _guix_query(cave, "search", pattern, f"--limit={limit}")


@guix_app.command("classify")
def guix_classify(spec: SpecArg, cave: CaveOpt = None):
    """pure-record, custom-arguments, or has-phases."""
    _guix_query(cave, "classify", spec)


# --- Commands ---

@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Cave name", autocompletion=complete_cave_name)],
    holocronix_url: Annotated[Optional[str], typer.Option(help="Holocronix flake URL (or git URL / path for --backend guix)")] = None,
    backend: Annotated[str, typer.Option("--backend", "-b", help="Image backend: nix (default) or guix")] = "nix",
):
    """Create a new cave."""
    if backend not in ("nix", "guix"):
        err_console.print(f"[red]Unknown backend '{backend}'[/] (expected nix or guix)")
        raise typer.Exit(1)
    d = cave_dir(name)

    if d.exists() and ((d / "flake.nix").exists() or (d / "cave.scm").exists()):
        err_console.print(f"[red]Cave '{name}' already exists at {d}[/]")
        raise typer.Exit(1)

    if backend == "guix":
        check_deps("guix")

    d.mkdir(parents=True, exist_ok=True)
    (d / "repos").mkdir(exist_ok=True)

    url = holocronix_url or os.environ.get("HOLOCRONIX_URL", HOLOCRONIX_URL_DEFAULT)

    if backend == "guix":
        (d / "cave.scm").write_text(
            CAVE_SCM_TEMPLATE.format(name=name, slug=image_slug(name)))
        _write_guix_channels(d, name, _holocronix_channel_url(url))
        cave_file = "cave.scm"
        edit_hint = "add your project's packages"
    else:
        (d / "flake.nix").write_text(FLAKE_TEMPLATE.format(
            name=name, slug=image_slug(name), holocronix_url=url))
        cave_file = "flake.nix"
        edit_hint = "add your project inputs and devShells"
    (d / "policy.yaml").write_text(POLICY_DEFAULTS)
    _write_compose(d, name, _load_policy(d))

    console.print(f"[green]Cave '{name}' created at {d}[/] ({backend} backend)")
    console.print("Next steps:")
    console.print(f"  1. Edit {d / cave_file} — {edit_hint}")
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
    """List flake inputs (or Guix channels) and their locked revisions."""
    name, d = resolve_cave(name)
    if cave_backend(d) == "guix":
        _print_guix_channels(name, d)
        return
    check_deps()
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
    guix: Annotated[bool, typer.Option("--guix", help="Guix caves: also re-pin the guix channel from `guix describe`")] = False,
):
    """Update flake inputs, or re-pin a Guix cave's holocronix channel."""
    name, d = resolve_cave(name)
    if cave_backend(d) == "guix":
        if input:
            err_console.print("[red]--input is not supported for Guix caves.[/] "
                              "jedi update re-pins holocronix; add --guix to "
                              "re-pin the guix channel as well")
            raise typer.Exit(1)
        check_deps("guix")
        url = _channels_holocronix_url(d) or _holocronix_channel_url(
            os.environ.get("HOLOCRONIX_URL", HOLOCRONIX_URL_DEFAULT))
        console.print("Re-pinning holocronix"
                      + (" and guix" if guix else "") + "...")
        kept = _write_guix_channels(d, name, url, refresh_guix=guix)
        console.print(f"[green]channels.scm updated for cave '{name}'[/]")
        if kept:
            console.print(f"[dim]  left untouched: {', '.join(kept)}[/]")
        return
    check_deps()

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
    name, d = resolve_cave(name)
    backend = cave_backend(d)
    check_deps(backend)

    if backend == "guix":
        if update:
            url = _channels_holocronix_url(d) or _holocronix_channel_url(
                os.environ.get("HOLOCRONIX_URL", HOLOCRONIX_URL_DEFAULT))
            console.print("Re-pinning holocronix...")
            _write_guix_channels(d, name, url)
        console.print(f"Building cave [cyan]{name}[/] with Guix...")
        # --root=result: a GC-rooted symlink, same shape as nix's result link.
        # Guix's register-root symlink()s without handling EEXIST, so a stale
        # link from an earlier build would abort this one; nix replaces its
        # own link.  Only ever remove a symlink, never a real file.
        result_link = d / "result"
        if result_link.is_symlink():
            result_link.unlink()
        # --max-silent-time=0: no silence timeout.  Compressing a multi-GB
        # image produces no output for a long stretch, which the daemon's
        # default 3600s limit would kill.  0 means "no limit" to the daemon.
        run(_guix_cmd(d, "build", *_guix_load_path_args(),
                      "-f", "cave.scm", "--root=result",
                      "--max-silent-time=0"), cwd=d)
    else:
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
    loaded = subprocess.run(["docker", "load", "-i", str(result_link)],
                            capture_output=True, text=True)
    console.print(f"[dim]  docker load -i {result_link}[/]")
    if loaded.returncode != 0:
        err_console.print(f"[red]docker load failed:[/]\n{loaded.stderr.strip()}")
        raise typer.Exit(1)
    console.print(loaded.stdout.strip())

    # Both builders name the image from their own definition, which knows
    # nothing about the cave directory, and older cave definitions all say
    # "jedicave".  Retag under this cave's own name so building one cave can
    # never replace another's image.
    m = re.search(r"Loaded image(?: ID)?: (\S+)", loaded.stdout)
    if not m:
        err_console.print("[red]Could not tell which image docker loaded[/]")
        raise typer.Exit(1)
    ref = image_ref(name)
    if m.group(1) != ref:
        run(["docker", "tag", m.group(1), ref])
    console.print(f"[green]Cave '{name}' built and loaded as {ref}[/]")


@app.command()
def up(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[str, typer.Option("--session", "-s", help="Session name (default 'default'); use distinct names to run parallel containers from the same cave",
                                          autocompletion=complete_session_name)] = "default",
    firewall: Annotated[bool, typer.Option(help="Enable firewall on startup")] = True,
):
    """Start cave container (one session)."""
    name, d = resolve_cave(name)
    _ensure_image(name)
    policy = _load_policy(d)
    _write_compose(d, name, policy)
    env_file = _resolve_secrets(d, policy)
    if env_file:
        console.print(f"[dim]  resolved {len(policy.get('secrets') or {})} secrets → {env_file.name}[/]")

    cenv = compose_env(session)
    base = compose_cmd(d, name, session)
    label = f"{name}/{session}" if session != "default" else name
    console.print(f"Starting cave [cyan]{label}[/]...")
    run(base + ["up", "-d"], cwd=d, env=cenv)
    if firewall:
        fw_cmds = firewall_commands(d)
        run(base + ["exec", "--user", "root", COMPOSE_SERVICE,
                    "bash", "-c", fw_cmds], cwd=d, env=cenv)
        console.print(f"[green]Cave '{label}' running (firewall on)[/]")
    else:
        console.print(f"[green]Cave '{label}' running[/]")
    enter_args = name if session == "default" else f"{name} -s {session}"
    console.print(f"Run: jedi enter {enter_args}")


@app.command()
def down(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name (default 'default'); use --all to stop every running session",
                                                    autocompletion=complete_session_name)] = None,
    all_sessions: Annotated[bool, typer.Option("--all", help="Stop every running session for this cave")] = False,
):
    """Stop cave container(s)."""
    name, d = resolve_cave(name)

    if all_sessions:
        sessions = [s["session"] for s in list_sessions(d, name) if s["running"]]
        if not sessions:
            console.print(f"No running sessions for cave '{name}'")
            return
    else:
        sessions = [session or "default"]

    for s in sessions:
        label = f"{name}/{s}" if s != "default" else name
        console.print(f"Stopping cave [cyan]{label}[/]...")
        run(compose_cmd(d, name, s) + ["down"], cwd=d, env=compose_env(s))
        if s == "default":
            _clear_compose_project_name(d)
        console.print(f"[green]Cave '{label}' stopped[/]")

    # Secrets file is per-cave; clear it once all sessions are down
    if not any(x["running"] for x in list_sessions(d, name)):
        _clear_secrets(d)


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
    session: Annotated[str, typer.Option("--session", "-s", help="Session name",
                                          autocompletion=complete_session_name)] = "default",
    firewall: Annotated[bool, typer.Option(help="Enable firewall on startup")] = True,
):
    """Restart cave container (down + up) for one session."""
    name, d = resolve_cave(name)
    policy = _load_policy(d)

    label = f"{name}/{session}" if session != "default" else name
    cenv = compose_env(session)
    base = compose_cmd(d, name, session)

    console.print(f"Restarting cave [cyan]{label}[/]...")
    run(base + ["down"], cwd=d, env=cenv)
    if session == "default":
        _clear_compose_project_name(d)
    if not any(x["running"] for x in list_sessions(d, name)):
        _clear_secrets(d)

    _write_compose(d, name, policy)
    env_file = _resolve_secrets(d, policy)
    if env_file:
        console.print(f"[dim]  resolved {len(policy.get('secrets') or {})} secrets → {env_file.name}[/]")

    run(base + ["up", "-d"], cwd=d, env=cenv)
    if firewall:
        fw_cmds = firewall_commands(d)
        run(base + ["exec", "--user", "root", COMPOSE_SERVICE,
                    "bash", "-c", fw_cmds], cwd=d, env=cenv)
        console.print(f"[green]Cave '{label}' restarted (firewall on)[/]")
    else:
        console.print(f"[green]Cave '{label}' restarted[/]")


@app.command()
def shell(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[str, typer.Option("--session", "-s", help="Session name",
                                          autocompletion=complete_session_name)] = "default",
    firewall: Annotated[bool, typer.Option(help="Enable firewall")] = True,
):
    """Ephemeral shell (no 'up' needed)."""
    name, d = resolve_cave(name)
    _ensure_image(name)
    _write_compose(d, name, _load_policy(d))
    project = compose_project(d, name, session)
    os.environ["JEDI_SESSION"] = session
    if firewall:
        fw_cmds = firewall_commands(d)
        os.execvp("docker", ["docker", "compose", "-p", project,
                              "--project-directory", str(d),
                              "run", "--rm", "-it", "--user", "root",
                              COMPOSE_SERVICE, "bash", "-c",
                              f"{fw_cmds} && exec su -s /bin/zsh yoda"])
    else:
        os.execvp("docker", ["docker", "compose", "-p", project,
                              "--project-directory", str(d),
                              "run", "--rm", "-it", COMPOSE_SERVICE, "zsh"])


@app.command()
def enter(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[str, typer.Option("--session", "-s", help="Session name",
                                          autocompletion=complete_session_name)] = "default",
):
    """Enter a running cave (requires 'up')."""
    name, d = resolve_cave(name)
    project = compose_project(d, name, session)
    os.environ["JEDI_SESSION"] = session
    os.execvp("docker", ["docker", "compose", "-p", project,
                          "--project-directory", str(d),
                          "exec", COMPOSE_SERVICE, "zsh"])


@app.command(context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
def exec(
    ctx: typer.Context,
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[str, typer.Option("--session", "-s", help="Session name",
                                          autocompletion=complete_session_name)] = "default",
):
    """Run a command in a running cave (requires 'up')."""
    name, d = resolve_cave(name)
    project = compose_project(d, name, session)
    os.environ["JEDI_SESSION"] = session
    os.execvp("docker", ["docker", "compose", "-p", project,
                          "--project-directory", str(d),
                          "exec", COMPOSE_SERVICE] + ctx.args)


@app.command()
def cp(
    src: Annotated[str, typer.Argument(help="Source path (prefix with : for container path)")],
    dst: Annotated[str, typer.Argument(help="Destination path (prefix with : for container path)")],
    name: Annotated[Optional[str], typer.Option("--cave", "-c", help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[str, typer.Option("--session", "-s", help="Session name",
                                          autocompletion=complete_session_name)] = "default",
):
    """Copy files between host and cave container.

    Prefix container paths with : (colon).

    \b
    Examples:
      jedi cp :/env/.claude/projects/foo/bar.md ./bar.md
      jedi cp ./config.yml :/workspace/project/config.yml
    """
    name, d = resolve_cave(name)

    if not is_session_running(d, name, session):
        label = f"{name}/{session}" if session != "default" else name
        err_console.print(f"[red]Cave '{label}' is not running.[/] Start it first: jedi up {name}{' -s ' + session if session != 'default' else ''}")
        raise typer.Exit(1)

    container_id = subprocess.run(
        compose_cmd(d, name, session) + ["ps", "-q", COMPOSE_SERVICE],
        cwd=d, capture_output=True, text=True, env=compose_env(session),
    ).stdout.strip()

    if not container_id:
        err_console.print(f"[red]Could not find container for cave '{name}/{session}'[/]")
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
    table.add_column("Sessions", style="magenta")
    table.add_column("Path", style="dim")

    for name in caves:
        d = cave_dir(name)
        sessions = list_sessions(d, name)
        running_names = [s["session"] for s in sessions if s["running"]]
        if running_names:
            status = "[green]running[/]"
            sess_str = ", ".join(running_names)
        else:
            status = "[dim]stopped[/]"
            sess_str = "-"
        table.add_row(name, status, sess_str, str(d))

    console.print(table)


@app.command()
def sessions(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
):
    """List sessions for a cave.

    A session is one container instance spawned from a cave. Multiple
    sessions can run in parallel from the same cave — they share the
    cave's bare repos read-only and isolate their workspace and
    harvested commits under refs/sessions/<session>/heads/*.
    """
    name, d = resolve_cave(name)
    sess = list_sessions(d, name)

    if not sess:
        console.print(f"No sessions for cave '{name}'. Start one: jedi up {name}")
        return

    table = Table(title=f"Sessions for cave '{name}'")
    table.add_column("Session", style="magenta")
    table.add_column("Status")
    table.add_column("Compose project", style="dim")

    for s in sess:
        status = "[green]running[/]" if s["running"] else "[dim]stopped[/]"
        project = compose_project(d, name, s["session"])
        table.add_row(s["session"], status, project)

    console.print(table)


class FirewallAction(str, Enum):
    on = "on"
    off = "off"
    status = "status"


@app.command()
def firewall(
    action: Annotated[FirewallAction, typer.Argument(help="Firewall action")],
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[str, typer.Option("--session", "-s", help="Session name",
                                          autocompletion=complete_session_name)] = "default",
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show full iptables output")] = False,
):
    """Manage cave firewall."""
    name, d = resolve_cave(name)

    if not is_session_running(d, name, session):
        label = f"{name}/{session}" if session != "default" else name
        flag = f" -s {session}" if session != "default" else ""
        err_console.print(
            f"[red]Cave '{label}' is not running.[/] Start it first:\n"
            f"  jedi up {name}{flag}\n"
            f"  jedi up --firewall {name}{flag}\n"
            f"Or use: jedi shell --firewall {name}{flag}"
        )
        raise typer.Exit(1)

    cenv = compose_env(session)
    base = compose_cmd(d, name, session)

    if action == FirewallAction.on:
        fw_cmds = firewall_commands(d)
        run(base + ["exec", "--user", "root", COMPOSE_SERVICE,
                    "bash", "-c", fw_cmds], cwd=d, env=cenv)
        domains = _policy_domains(_load_policy(d))
        console.print(f"[green]Firewall enabled ({len(domains)} domains allowlisted)[/]")

    elif action == FirewallAction.off:
        ipt = "/usr/local/sbin/iptables"
        run(base + ["exec", "--user", "root", COMPOSE_SERVICE,
                    "bash", "-c",
                    f"{ipt} -F OUTPUT && {ipt} -P OUTPUT ACCEPT"], cwd=d, env=cenv)
        console.print("[green]Firewall disabled[/]")

    elif action == FirewallAction.status:
        result = subprocess.run(
            base + ["exec", "--user", "root", COMPOSE_SERVICE,
                    "/usr/local/sbin/iptables", "-L", "OUTPUT", "-n"],
            cwd=d, capture_output=True, text=True, env=cenv,
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
            run(base + ["exec", "--user", "root", COMPOSE_SERVICE,
                        "/usr/local/sbin/iptables", "-L", "-n", "-v"], cwd=d, env=cenv)


@app.command()
def seed(
    repo_path: Annotated[str, typer.Argument(help="Path to source git repo")],
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    branch: Annotated[Optional[str], typer.Option(help="Branch to seed (default: current branch)")] = None,
    all_branches: Annotated[bool, typer.Option("--all", help="Seed all branches")] = False,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force-push (overwrite diverged branches)")] = False,
    depth: Annotated[Optional[int], typer.Option(help="Shallow clone with N commits of history")] = None,
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

    if depth is not None and depth < 1:
        err_console.print("[red]--depth must be at least 1[/]")
        raise typer.Exit(1)

    # Determine branch to push/clone
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

    if depth:
        # Shallow clone: create a truncated bare repo with limited history.
        if (bare_path / "HEAD").exists():
            if not force:
                err_console.print(
                    f"[red]Bare repo already exists at {bare_path}[/]\n"
                    "  Use --force to replace it with a shallow clone"
                )
                raise typer.Exit(1)
            trash_dir = d / ".trash"
            trash_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            trash_dest = trash_dir / f"{repo_name}.git.{timestamp}"
            bare_path.rename(trash_dest)
            console.print(f"[dim]  Moved existing bare repo to {trash_dest}[/]")

        clone_cmd = ["git", "clone", "--bare", f"--depth={depth}"]
        if all_branches:
            clone_cmd.append("--no-single-branch")
        else:
            clone_cmd.extend(["--single-branch", "--branch", branch])
        clone_cmd.extend([str(repo), str(bare_path)])

        try:
            run(clone_cmd)
        except subprocess.CalledProcessError:
            err_console.print("[red]Shallow clone failed[/]")
            raise typer.Exit(1)

        # Store metadata
        run(["git", "--git-dir", str(bare_path), "config",
             "jedicave.sourceRepo", str(repo)], check=False)
        run(["git", "--git-dir", str(bare_path), "config",
             "jedicave.depth", str(depth)], check=False)

    else:
        # Full seed: init bare repo + push branches.
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
    if depth:
        console.print(f"  Depth: {depth} commit(s)")
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


def _session_ref_glob(session: str) -> str:
    """Refspec namespace inside the bare repo for a session's harvested work."""
    return f"refs/sessions/{session}/heads"


def _bare_session_tips(bare: Path, session: str) -> set:
    """Commit hashes at the tips of refs/sessions/<session>/heads/* in a bare repo."""
    result = subprocess.run(
        ["git", "--git-dir", str(bare), "for-each-ref",
         "--format=%(objectname)", f"{_session_ref_glob(session)}/*"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return set()
    return set(result.stdout.strip().splitlines())


def _bare_seed_tips(bare: Path) -> set:
    """Commit hashes at the tips of refs/heads/* (the seeded base) in a bare repo."""
    result = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "--branches"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return set()
    return set(result.stdout.strip().splitlines())


def _sync_session(d: Path, name: str, session: str, bare_repos: list[Path]) -> set:
    """Bundle each repo out of one session's container and fetch into the
    bare under refs/sessions/<session>/heads/*. Returns the set of repo
    names that were synced (i.e. the repo existed in /workspace/)."""
    cenv = compose_env(session)
    base = compose_cmd(d, name, session)
    container_id = subprocess.run(
        base + ["ps", "-q", COMPOSE_SERVICE],
        cwd=d, capture_output=True, text=True, env=cenv,
    ).stdout.strip()
    if not container_id:
        return set()

    synced = set()
    for bare in bare_repos:
        rn = bare.name[:-4]
        workdir = f"/workspace/{rn}"
        bundle_path = f"/tmp/{rn}.bundle"

        check = subprocess.run(
            base + ["exec", COMPOSE_SERVICE, "test", "-d", workdir],
            cwd=d, capture_output=True, env=cenv,
        )
        if check.returncode != 0:
            continue

        result = subprocess.run(
            base + ["exec", "-w", workdir, COMPOSE_SERVICE,
                    "git", "bundle", "create", bundle_path, "--all"],
            cwd=d, capture_output=True, text=True, env=cenv,
        )
        if result.returncode != 0:
            err_console.print(f"[yellow]Could not bundle '{rn}' from '{session}': {result.stderr.strip()}[/]")
            continue

        host_bundle = d / "repos" / f"{rn}.{session}.bundle"
        result = subprocess.run(
            ["docker", "cp", f"{container_id}:{bundle_path}", str(host_bundle)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            err_console.print(f"[yellow]Could not extract bundle for '{rn}' from '{session}'[/]")
            continue

        refspec = f"+refs/heads/*:{_session_ref_glob(session)}/*"
        subprocess.run(
            ["git", "--git-dir", str(bare), "fetch", str(host_bundle), refspec],
            capture_output=True, text=True,
        )
        host_bundle.unlink(missing_ok=True)
        synced.add(rn)

    return synced


@app.command()
def harvest(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session to harvest (default: all running sessions)",
                                                    autocompletion=complete_session_name)] = None,
):
    """Show agent commits in cave repos and how to fetch them.

    Each session's work is namespaced into the bare repo under
    refs/sessions/<session>/heads/*, so parallel sessions never clobber
    each other's branches.
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

    if not bare_repos:
        console.print("No repos seeded. Run: jedi seed <repo-path>")
        return

    # Resolve which session(s) to harvest
    running_sessions = [s["session"] for s in list_sessions(d, name) if s["running"]]
    if session:
        sessions_to_sync = [session] if session in running_sessions else []
        if not sessions_to_sync:
            err_console.print(f"[yellow]Session '{session}' is not running for cave '{name}'[/]")
    else:
        sessions_to_sync = running_sessions

    # Snapshot pre-sync tips per (session, repo) so we can flag what's new.
    pre_tips: dict[tuple[str, str], set] = {}
    for s in sessions_to_sync:
        for bare in bare_repos:
            pre_tips[(s, bare.name[:-4])] = _bare_session_tips(bare, s)

    synced_per_session: dict[str, set] = {}
    for s in sessions_to_sync:
        synced_per_session[s] = _sync_session(d, name, s, bare_repos)

    # Report per repo, splitting out per-session commits.
    for bare in bare_repos:
        repo_name = bare.name[:-4]
        seed_tips = _bare_seed_tips(bare)
        seed_excl = [f"^{t}" for t in seed_tips]

        seeded_result = subprocess.run(
            ["git", "--git-dir", str(bare), "config", "jedicave.seededCount"],
            capture_output=True, text=True,
        )
        seeded_count = int(seeded_result.stdout.strip()) if (
            seeded_result.returncode == 0 and seeded_result.stdout.strip()
        ) else None

        # Discover all sessions that have ever pushed work into this bare,
        # not just the ones we just synced — so 'harvest' is also a viewer.
        all_session_refs = subprocess.run(
            ["git", "--git-dir", str(bare), "for-each-ref",
             "--format=%(refname)", "refs/sessions/"],
            capture_output=True, text=True,
        ).stdout.strip().splitlines()
        all_sessions = sorted({
            r.split("/", 3)[2] for r in all_session_refs if r.startswith("refs/sessions/")
        })

        console.print(f"\n[cyan]{repo_name}[/]")

        if not all_sessions:
            console.print("  [dim]No harvested work yet[/]")
            continue

        for s in all_sessions:
            ns = _session_ref_glob(s)
            session_tips = _bare_session_tips(bare, s)
            if not session_tips:
                continue

            # New = added since pre-sync snapshot (only meaningful if we just synced this session)
            old_tips = pre_tips.get((s, repo_name), session_tips)
            new_excl = [f"^{t}" for t in old_tips]
            new_count_r = subprocess.run(
                ["git", "--git-dir", str(bare), "rev-list", "--count",
                 f"--glob={ns}/*"] + new_excl,
                capture_output=True, text=True,
            )
            new_count = int(new_count_r.stdout.strip()) if new_count_r.returncode == 0 else 0

            # Agent total = commits in this session not reachable from the seed
            agent_total_r = subprocess.run(
                ["git", "--git-dir", str(bare), "rev-list", "--count",
                 f"--glob={ns}/*"] + seed_excl,
                capture_output=True, text=True,
            )
            agent_total = int(agent_total_r.stdout.strip()) if agent_total_r.returncode == 0 else 0

            session_label = f"[magenta]{s}[/]"
            running_marker = " [green]●[/]" if s in running_sessions else " [dim]○[/]"
            line = f"  {session_label}{running_marker}"
            if new_count > 0:
                line += f"  [green]+{new_count} new[/]  ({agent_total} by agent)"
            elif agent_total > 0:
                line += f"  [dim]no new commits[/]  ({agent_total} by agent)"
            else:
                line += "  [dim]no agent commits[/]"
            if seeded_count is not None:
                line += f"  [dim]| seed: {seeded_count}[/]"
            console.print(line)

            if new_count > 0:
                log_r = subprocess.run(
                    ["git", "--git-dir", str(bare), "log", "--oneline", "--graph",
                     "-30", f"--glob={ns}/*"] + new_excl,
                    capture_output=True, text=True,
                )
                if log_r.stdout.strip():
                    for ln in log_r.stdout.rstrip().splitlines():
                        console.print(f"    {ln}")

    console.print(f"\n  To fetch into your repos:")
    console.print(f"    jedi fetch {name}")

    if running_sessions:
        running_str = ", ".join(running_sessions)
        console.print(f"\n[dim]Running sessions: {running_str}[/]")
        console.print(f"[dim]Tip: use 'jedi diff' to see uncommitted changes still inside a container[/]")
    else:
        console.print(f"\n[yellow]No sessions running — only showing previously harvested commits.[/]")
        console.print(f"[yellow]Run 'jedi harvest' while a session is running to sync agent commits.[/]")


@app.command()
def diff(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[str, typer.Option("--session", "-s", help="Session name",
                                          autocompletion=complete_session_name)] = "default",
    repo_name: Annotated[Optional[str], typer.Option("--repo", "-r", help="Specific repo (default: all)")] = None,
    stat: Annotated[bool, typer.Option("--stat", help="Show diffstat instead of full diff")] = False,
):
    """Show uncommitted changes in workspace repos (requires a running session)."""
    name, d = resolve_cave(name)

    if not is_session_running(d, name, session):
        label = f"{name}/{session}" if session != "default" else name
        flag = f" -s {session}" if session != "default" else ""
        err_console.print(f"[red]Cave '{label}' is not running.[/] Start it first: jedi up {name}{flag}")
        raise typer.Exit(1)

    cenv = compose_env(session)
    base = compose_cmd(d, name, session)

    # Discover repos inside the container
    if repo_name:
        repos = [repo_name]
    else:
        result = subprocess.run(
            base + ["exec", COMPOSE_SERVICE,
                    "sh", "-c", "ls -d /workspace/*/.git 2>/dev/null | xargs -I{} dirname {}"],
            cwd=d, capture_output=True, text=True, env=cenv,
        )
        if not result.stdout.strip():
            console.print("No git repos found in /workspace/")
            return
        repos = [Path(p).name for p in result.stdout.strip().splitlines()]

    for rn in repos:
        workdir = f"/workspace/{rn}"

        # Check repo exists
        check = subprocess.run(
            base + ["exec", COMPOSE_SERVICE, "test", "-d", workdir],
            cwd=d, capture_output=True, env=cenv,
        )
        if check.returncode != 0:
            err_console.print(f"[red]Repo '{rn}' not found in /workspace/[/]")
            continue

        console.print(f"\n[cyan bold]{rn}[/]")

        # Tracked changes
        diff_cmd = "git diff HEAD --stat" if stat else "git diff HEAD"
        result = subprocess.run(
            base + ["exec", "-w", workdir, COMPOSE_SERVICE,
                    "sh", "-c", diff_cmd],
            cwd=d, capture_output=True, text=True, env=cenv,
        )
        if result.stdout.strip():
            console.print(result.stdout.rstrip())
        else:
            console.print("  [dim]no tracked changes[/]")

        # Untracked files
        result = subprocess.run(
            base + ["exec", "-w", workdir, COMPOSE_SERVICE,
                    "git", "ls-files", "--others", "--exclude-standard"],
            cwd=d, capture_output=True, text=True, env=cenv,
        )
        untracked = result.stdout.strip().splitlines()
        if untracked:
            console.print(f"\n  [yellow]Untracked files ({len(untracked)}):[/]")
            for f in untracked:
                console.print(f"    {f}")


@app.command()
def fetch(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session to sync first (default: all running). Has no effect on which refs are published to the source repo — every refs/sessions/<s>/heads/<b> becomes cave/<s>/<b>.",
                                                    autocompletion=complete_session_name)] = None,
    repo_name: Annotated[Optional[str], typer.Option("--repo", "-r", help="Specific repo (default: all)")] = None,
):
    """Fetch agent commits from cave into your source repos.

    Each session's work lives under ``refs/sessions/<session>/heads/*`` in the
    bare repo (populated by ``jedi harvest``). This command publishes those refs
    into the source repo as ``cave/<session>/<branch>`` remote-tracking branches.
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

    if not bare_repos:
        console.print("No repos seeded. Run: jedi seed <repo-path>")
        return

    if repo_name:
        bare_repos = [p for p in bare_repos if p.name == f"{repo_name}.git"]
        if not bare_repos:
            err_console.print(f"[red]No seeded repo '{repo_name}' in cave '{name}'[/]")
            raise typer.Exit(1)

    # Sync any running sessions so the bare repo has the freshest work.
    running_sessions = [s["session"] for s in list_sessions(d, name) if s["running"]]
    if session:
        sessions_to_sync = [session] if session in running_sessions else []
        if not sessions_to_sync and session not in (s["session"] for s in list_sessions(d, name)):
            err_console.print(f"[yellow]Session '{session}' not found for cave '{name}'[/]")
    else:
        sessions_to_sync = running_sessions

    for s in sessions_to_sync:
        _sync_session(d, name, s, bare_repos)

    for bare in bare_repos:
        rn = bare.name[:-4]

        source_result = subprocess.run(
            ["git", "--git-dir", str(bare), "config", "jedicave.sourceRepo"],
            capture_output=True, text=True,
        )
        source_repo = source_result.stdout.strip()

        if not source_repo or not Path(source_repo).is_dir():
            err_console.print(
                f"[red]No source repo for '{rn}'.[/] "
                f"Fetch manually per session:\n"
                f"  git fetch {bare} '+refs/sessions/<s>/heads/*:refs/remotes/cave/<s>/*'"
            )
            continue

        # Enumerate sessions that have refs in this bare repo. Git refspecs
        # only allow one '*' per side, so we fetch one session at a time.
        session_refs = subprocess.run(
            ["git", "--git-dir", str(bare), "for-each-ref",
             "--format=%(refname)", "refs/sessions/"],
            capture_output=True, text=True,
        ).stdout.strip().splitlines()
        bare_sessions = sorted({
            r.split("/", 3)[2] for r in session_refs if r.startswith("refs/sessions/")
        })

        if not bare_sessions:
            console.print(f"\n[dim]'{rn}': no harvested sessions yet[/]")
            continue

        any_failed = False
        for s in bare_sessions:
            r = run(
                ["git", "fetch", "--prune", str(bare),
                 f"+refs/sessions/{s}/heads/*:refs/remotes/cave/{s}/*"],
                cwd=Path(source_repo), check=False,
            )
            if r.returncode != 0:
                any_failed = True
        if any_failed:
            err_console.print(f"[red]Some sessions failed to fetch for '{rn}'[/]")
            continue

        branch_result = subprocess.run(
            ["git", "branch", "-r", "--list", "cave/*", "--format=%(refname:short)"],
            cwd=source_repo, capture_output=True, text=True,
        )
        branches = branch_result.stdout.strip().splitlines()

        console.print(f"\n[green]Fetched '{rn}'[/] into {source_repo}")
        if branches:
            # Group by session for legibility
            by_session: dict[str, list[str]] = {}
            for b in branches:
                # b looks like "cave/<session>/<branch>"; the prefix is stripped already
                short = b[len("cave/"):] if b.startswith("cave/") else b
                if "/" in short:
                    s, br = short.split("/", 1)
                else:
                    s, br = "default", short
                by_session.setdefault(s, []).append(br)
            console.print(f"  Remote branches by session:")
            for s in sorted(by_session):
                console.print(f"    [magenta]{s}[/]: {', '.join(sorted(by_session[s]))}")
        console.print(f"\n  Review:")
        console.print(f"    cd {source_repo}")
        console.print(f"    git log --oneline --graph 'cave/*' --not HEAD")
        console.print(f"    git diff HEAD..cave/<session>/<branch>")


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

    sess = list_sessions(d, name)
    running_sessions = [s["session"] for s in sess if s["running"]]
    status = "[green]running[/]" if running_sessions else "[dim]stopped[/]"
    console.print(f"[cyan]{name}[/]  {status}")
    console.print(f"  Path: {d}")

    if sess:
        console.print(f"\n  Sessions:")
        for s in sess:
            marker = "[green]●[/]" if s["running"] else "[dim]○[/]"
            console.print(f"    {marker} [magenta]{s['session']}[/]")

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
        "   jedi init my-cave                  # Nix backend (default)\n"
        "   jedi init --backend guix my-cave   # Guix backend\n"
        "   Creates scaffolding at ~/.config/jedicaves/my-cave/\n"
        "\n"
        "[bold cyan]2. Configure the cave[/]\n"
        "   Nix:  edit flake.nix to add your project inputs and devShells.\n"
        "   Guix: edit cave.scm to add your project's packages.\n"
        "   Both files have commented examples.\n"
        "   jedi show my-cave         # see cave path and details\n"
        "\n"
        "[bold cyan]3. Build the container image[/]\n"
        "   jedi build my-cave\n"
        "   Builds with Nix or Guix and loads the image into Docker.\n"
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
        "   jedi inputs my-cave        # show locked flake inputs / Guix channels\n"
        "   jedi logs -f my-cave       # follow container logs\n"
        "   jedi destroy my-cave       # tear down a cave\n"
    )


@app.command()
def logs(
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    session: Annotated[str, typer.Option("--session", "-s", help="Session name",
                                          autocompletion=complete_session_name)] = "default",
    follow: Annotated[bool, typer.Option("-f", "--follow", help="Follow log output")] = False,
    tail: Annotated[Optional[int], typer.Option("-n", "--tail", help="Number of lines from end")] = None,
):
    """Show cave container logs."""
    name, d = resolve_cave(name)
    project = compose_project(d, name, session)
    os.environ["JEDI_SESSION"] = session
    cmd = ["docker", "compose", "-p", project, "--project-directory", str(d),
           "logs", COMPOSE_SERVICE]
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
        ref = image_ref(name)
        if _image_exists(ref):
            # Kept on purpose: destroy is reversible, and rebuilding the image
            # is the slow part of restoring a cave.
            console.print(f"  Image {ref} kept. To reclaim the space: "
                          f"docker rmi {ref}")
    else:
        console.print("Aborted")


if __name__ == "__main__":
    app(prog_name="jedi")
