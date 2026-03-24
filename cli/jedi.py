#!/usr/bin/env python3
"""jedi — CLI for managing jedicaves (sandboxed containers)."""

import json
import os
import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="jedi",
    help="Manage jedicaves — sandboxed containers",
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
    return CAVES_DIR / name


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


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    console.print(f"[dim]  {' '.join(cmd)}[/]")
    return subprocess.run(cmd, cwd=cwd, check=check)


def is_cave_running(d: Path) -> bool:
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", COMPOSE_SERVICE],
        cwd=d, capture_output=True, text=True
    )
    return bool(result.stdout.strip())


def firewall_commands(d: Path) -> str:
    config = d / "firewall-defaults.conf"
    if not config.exists():
        err_console.print(f"[red]No firewall config at {config}[/]")
        raise typer.Exit(1)

    domains = [
        line.strip() for line in config.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    cmds = ["iptables -F OUTPUT"]
    for domain in domains:
        cmds.append(f"iptables -A OUTPUT -d {domain} -j ACCEPT")
    cmds.append("iptables -A OUTPUT -o lo -j ACCEPT")
    cmds.append("iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT")
    cmds.append("iptables -A OUTPUT -j DROP")
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

    # Add project flake inputs here:
    # my-project.url = "path:/home/yoda/code/my-project";
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

FIREWALL_DEFAULTS = """\
# Default allowlisted domains for jedi firewall
# One domain per line. Lines starting with # are comments.
api.anthropic.com
# github.com
# raw.githubusercontent.com
# registry.npmjs.org
# pypi.org
# files.pythonhosted.org
"""

COMPOSE_TEMPLATE = """\
services:
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
      - TZ=${{TZ:-UTC}}
    volumes:
      - {name}-history:/commandhistory
      - {name}-config:/env/.claude
      # Project source mounts go in compose.override.yml:
      #   services:
      #     shell:
      #       volumes:
      #         - /home/yoda/code/my-project:/workspace/my-project

volumes:
  {name}-history:
  {name}-config:
"""


# --- Commands ---

@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Cave name")],
    holocronix_url: Annotated[Optional[str], typer.Option(help="Holocronix flake URL")] = None,
):
    """Create a new cave."""
    d = cave_dir(name)

    if d.exists() and (d / "flake.nix").exists():
        err_console.print(f"[red]Cave '{name}' already exists at {d}[/]")
        raise typer.Exit(1)

    d.mkdir(parents=True, exist_ok=True)

    url = holocronix_url or os.environ.get("HOLOCRONIX_URL", HOLOCRONIX_URL_DEFAULT)

    (d / "flake.nix").write_text(FLAKE_TEMPLATE.format(name=name, holocronix_url=url))
    (d / "compose.yml").write_text(COMPOSE_TEMPLATE.format(name=name))
    (d / "firewall-defaults.conf").write_text(FIREWALL_DEFAULTS)

    console.print(f"[green]Cave '{name}' created at {d}[/]")
    console.print("Next steps:")
    console.print(f"  1. Edit {d / 'flake.nix'} — add your project inputs and devShells")
    console.print(f"  2. Create {d}/compose.override.yml for project mounts")
    console.print(f"  3. Run: jedi build {name}")


@app.command()
def inputs(
    name: Annotated[Optional[str], typer.Argument(help="Cave name")] = None,
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
    input: Annotated[Optional[str], typer.Argument(help="Specific input to update (default: all)")] = None,
    name: Annotated[Optional[str], typer.Argument(help="Cave name")] = None,
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
    name: Annotated[Optional[str], typer.Argument(help="Cave name")] = None,
    update: Annotated[Optional[str], typer.Option(
        help="Update flake inputs before building (optionally specify one input)",
        show_default=False,
    )] = None,
    update_all: Annotated[bool, typer.Option("--update-all", help="Update all flake inputs before building")] = False,
):
    """Build cave image."""
    check_deps()
    name, d = resolve_cave(name)

    if update_all:
        console.print("Updating all inputs...")
        run(["nix", "flake", "update"], cwd=d)
    elif update:
        console.print(f"Updating input [cyan]{update}[/]...")
        run(["nix", "flake", "update", update], cwd=d)

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
    name: Annotated[Optional[str], typer.Argument(help="Cave name")] = None,
    firewall: Annotated[bool, typer.Option(help="Enable firewall on startup")] = False,
):
    """Start cave container."""
    name, d = resolve_cave(name)
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
    name: Annotated[Optional[str], typer.Argument(help="Cave name")] = None,
):
    """Stop cave container."""
    name, d = resolve_cave(name)
    console.print(f"Stopping cave [cyan]{name}[/]...")
    run(["docker", "compose", "down"], cwd=d)
    console.print(f"[green]Cave '{name}' stopped[/]")


@app.command()
def shell(
    name: Annotated[Optional[str], typer.Argument(help="Cave name")] = None,
    firewall: Annotated[bool, typer.Option(help="Enable firewall")] = False,
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
    name: Annotated[Optional[str], typer.Argument(help="Cave name")] = None,
):
    """Enter a running cave (requires 'up')."""
    name, d = resolve_cave(name)
    os.execvp("docker", ["docker", "compose", "--project-directory", str(d),
                          "exec", COMPOSE_SERVICE, "zsh"])


@app.command(context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
def exec(
    ctx: typer.Context,
    name: Annotated[Optional[str], typer.Argument(help="Cave name")] = None,
):
    """Run a command in a running cave (requires 'up')."""
    name, d = resolve_cave(name)
    os.execvp("docker", ["docker", "compose", "--project-directory", str(d),
                          "exec", COMPOSE_SERVICE] + ctx.args)


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
    name: Annotated[Optional[str], typer.Argument(help="Cave name")] = None,
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
        config = d / "firewall-defaults.conf"
        domains = [l.strip() for l in config.read_text().splitlines()
                   if l.strip() and not l.strip().startswith("#")]
        console.print(f"[green]Firewall enabled ({len(domains)} domains allowlisted)[/]")

    elif action == FirewallAction.off:
        run(["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "bash", "-c", "iptables -F && iptables -X && iptables -P OUTPUT ACCEPT"], cwd=d)
        console.print("[green]Firewall disabled[/]")

    elif action == FirewallAction.status:
        result = subprocess.run(
            ["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "iptables", "-L", "OUTPUT", "-n"],
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
                 "iptables", "-L", "-n", "-v"], cwd=d)


@app.command()
def destroy(
    name: Annotated[str, typer.Argument(help="Cave name")],
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation")] = False,
):
    """Delete a cave."""
    name, d = resolve_cave(name)

    subprocess.run(["docker", "compose", "down"], cwd=d, capture_output=True)

    if yes or typer.confirm(f"Delete cave '{name}' at {d}?", default=False):
        shutil.rmtree(d)
        console.print(f"[green]Cave '{name}' destroyed[/]")
    else:
        console.print("Aborted")


if __name__ == "__main__":
    app(prog_name="jedi")
