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


def complete_cave_name(incomplete: str) -> list[str]:
    return [name for name in _list_caves() if name.startswith(incomplete)]


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
      - ./repos:/repos:ro
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
    (d / "compose.yml").write_text(COMPOSE_TEMPLATE.format(name=name))
    (d / "firewall-defaults.conf").write_text(FIREWALL_DEFAULTS)

    console.print(f"[green]Cave '{name}' created at {d}[/]")
    console.print("Next steps:")
    console.print(f"  1. Edit {d / 'flake.nix'} — add your project inputs and devShells")
    console.print(f"  2. Seed your project: jedi seed <repo-path> {name}")
    console.print(f"  3. Run: jedi build {name}")


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
    update: Annotated[Optional[list[str]], typer.Option(
        help="Update inputs before building (specific inputs, or omit for all)",
        show_default=False,
    )] = None,
):
    """Build cave image."""
    check_deps()
    name, d = resolve_cave(name)

    if update is not None:
        if update:
            for inp in update:
                console.print(f"Updating input [cyan]{inp}[/]...")
                run(["nix", "flake", "update", inp], cwd=d)
        else:
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
    console.print(f"[green]Cave '{name}' stopped[/]")


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
def seed(
    repo_path: Annotated[str, typer.Argument(help="Path to source git repo")],
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    branch: Annotated[Optional[str], typer.Option(help="Branch to seed (default: current branch)")] = None,
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
    if not branch:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo, capture_output=True, text=True,
        )
        branch = result.stdout.strip()
        if not branch or branch == "HEAD":
            err_console.print("[red]Detached HEAD — specify --branch[/]")
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

    # Push branch
    run(["git", "push", str(bare_path),
         f"refs/heads/{branch}:refs/heads/{branch}"], cwd=repo)

    # Set HEAD to the seeded branch
    run(["git", "--git-dir", str(bare_path),
         "symbolic-ref", "HEAD", f"refs/heads/{branch}"])

    console.print(f"[green]Seeded '{repo_name}' branch '{branch}' into cave '{name}'[/]")
    console.print(f"  Bare repo: {bare_path}")
    console.print(f"  Will be available at /workspace/{repo_name} inside the container")


@app.command()
def unseed(
    repo_name: Annotated[str, typer.Argument(help="Repo name to remove")],
    name: Annotated[Optional[str], typer.Argument(help="Cave name", autocompletion=complete_cave_name)] = None,
    yes: Annotated[bool, typer.Option("-y", "--yes", help="Skip confirmation")] = False,
):
    """Remove a seeded bare repo from the cave."""
    name, d = resolve_cave(name)
    bare_path = d / "repos" / f"{repo_name}.git"

    if not bare_path.exists():
        err_console.print(f"[red]No seeded repo '{repo_name}' in cave '{name}'[/]")
        raise typer.Exit(1)

    if yes or typer.confirm(f"Remove seeded repo '{repo_name}' from cave '{name}'?", default=False):
        shutil.rmtree(bare_path)
        console.print(f"[green]Removed '{repo_name}' from cave '{name}'[/]")
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

        # Get source repo path
        source_result = subprocess.run(
            ["git", "--git-dir", str(bare), "config", "jedicave.sourceRepo"],
            capture_output=True, text=True,
        )
        source_repo = source_result.stdout.strip()

        # Show branches and recent commits (including harvested refs)
        result = subprocess.run(
            ["git", "--git-dir", str(bare), "log", "--all",
             "--oneline", "--graph", "-15"],
            capture_output=True, text=True,
        )

        console.print(f"\n[cyan]{repo_name}[/]")
        if result.stdout.strip():
            console.print(result.stdout.strip())
        else:
            console.print("  (no commits)")

        console.print(f"\n  To fetch into your repo:")
        if source_repo:
            console.print(f"    cd {source_repo}")
        else:
            console.print(f"    cd /path/to/{repo_name}")
        console.print(f"    git fetch {bare}")
        console.print(f"    git log HEAD..FETCH_HEAD")
        console.print(f"    git diff HEAD..FETCH_HEAD")

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
        "   jedi harvest my-cave         # committed work + fetch commands\n"
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

    subprocess.run(["docker", "compose", "down"], cwd=d, capture_output=True)

    if yes or typer.confirm(f"Delete cave '{name}' at {d}?", default=False):
        shutil.rmtree(d)
        console.print(f"[green]Cave '{name}' destroyed[/]")
    else:
        console.print("Aborted")


if __name__ == "__main__":
    app(prog_name="jedi")
