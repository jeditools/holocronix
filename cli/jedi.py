#!/usr/bin/env python3
"""jedi — CLI for managing jedicaves (sandboxed dev containers)."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

CAVES_DIR = Path(os.environ.get("JEDI_CAVES_DIR", Path.home() / ".config" / "jedicaves"))
COMPOSE_SERVICE = "shell"

# Resolve the holocron directory (where this repo lives)
HOLOCRON_DIR = Path(__file__).resolve().parent.parent


def cave_dir(name: str) -> Path:
    return CAVES_DIR / name


def resolve_cave(name: str | None) -> tuple[str, Path]:
    """Resolve a cave name to (name, path). Infer if only one cave exists."""
    if name:
        d = cave_dir(name)
        if not d.exists():
            die(f"Cave '{name}' not found. Run: jedi list")
        return name, d

    caves = list_caves()
    if len(caves) == 1:
        return caves[0], cave_dir(caves[0])
    elif len(caves) == 0:
        die("No caves found. Run: jedi init <name>")
    else:
        die(f"Multiple caves exist ({', '.join(caves)}). Specify one: jedi <command> <name>")


def list_caves() -> list[str]:
    if not CAVES_DIR.exists():
        return []
    return sorted(
        d.name for d in CAVES_DIR.iterdir()
        if d.is_dir() and (d / "flake.nix").exists()
    )


def die(msg: str) -> None:
    print(f"\033[0;31m[jedi]\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"\033[0;34m[jedi]\033[0m {msg}")


def success(msg: str) -> None:
    print(f"\033[0;32m[jedi]\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[1;33m[jedi]\033[0m {msg}")


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    info(f"  {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check)


def check_deps() -> None:
    missing = []
    if not shutil.which("nix"):
        missing.append("nix (https://nixos.org/download/)")
    if not shutil.which("docker"):
        missing.append("docker")
    if missing:
        die("Missing required tools:\n" + "\n".join(f"  - {m}" for m in missing))


# --- Templates ---

FLAKE_TEMPLATE = """\
# Jedicave: {name}
#
# Edit the inputs and projectShells below, then build:
#   jedi build {name}
#
{{
  inputs = {{
    holocron.url = "{holocron_url}";

    # Add project flake inputs here:
    # my-project.url = "path:/home/user/code/my-project";
  }};

  outputs = {{ holocron, ... }}@inputs: let
    system = "x86_64-linux";
    mkDevContainer = holocron.lib.${{system}}.mkDevContainer;
  in {{
    packages.${{system}}.container = mkDevContainer {{
      # List your project devShells here:
      # projectShells = [
      #   inputs.my-project.devShells.${{system}}.default
      # ];
    }};
  }};
}}
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
      # Project source mounts go in docker-compose.override.yml:
      #   services:
      #     shell:
      #       volumes:
      #         - /home/user/code/my-project:/workspace/my-project

volumes:
  {name}-history:
  {name}-config:
"""


# --- Commands ---

def cmd_init(args: argparse.Namespace) -> None:
    name = args.name
    d = cave_dir(name)

    if d.exists() and (d / "flake.nix").exists():
        die(f"Cave '{name}' already exists at {d}")

    d.mkdir(parents=True, exist_ok=True)

    holocron_url = f"path:{HOLOCRON_DIR}"

    flake_path = d / "flake.nix"
    flake_path.write_text(FLAKE_TEMPLATE.format(name=name, holocron_url=holocron_url))

    compose_path = d / "compose.yml"
    compose_path.write_text(COMPOSE_TEMPLATE.format(name=name))

    # Copy firewall config
    fw_src = HOLOCRON_DIR / "config" / "firewall-defaults.conf"
    if fw_src.exists():
        (d / "firewall-defaults.conf").write_text(fw_src.read_text())

    success(f"Cave '{name}' created at {d}")
    info(f"Next steps:")
    info(f"  1. Edit {flake_path} — add your project inputs and devShells")
    info(f"  2. Create {d}/docker-compose.override.yml for project mounts")
    info(f"  3. Run: jedi build {name}")


def cmd_build(args: argparse.Namespace) -> None:
    check_deps()
    name, d = resolve_cave(args.name)

    if args.update is not None:
        if args.update:
            info(f"Updating input '{args.update}'...")
            run(["nix", "flake", "update", args.update], cwd=d)
        else:
            info("Updating all inputs...")
            run(["nix", "flake", "update"], cwd=d)

    info(f"Building cave '{name}'...")
    run(["nix", "build", ".#container", "--print-build-logs"], cwd=d)

    result = d / "result"
    if not result.exists():
        die("Build produced no result link")

    info("Loading image into Docker...")
    run(["docker", "load", "-i", str(result)])
    success(f"Cave '{name}' built and loaded")


def cmd_up(args: argparse.Namespace) -> None:
    name, d = resolve_cave(args.name)
    info(f"Starting cave '{name}'...")
    run(["docker", "compose", "up", "-d"], cwd=d)
    success(f"Cave '{name}' running")
    info(f"Run: jedi enter {name}")


def cmd_down(args: argparse.Namespace) -> None:
    name, d = resolve_cave(args.name)
    info(f"Stopping cave '{name}'...")
    run(["docker", "compose", "down"], cwd=d)
    success(f"Cave '{name}' stopped")


def cmd_shell(args: argparse.Namespace) -> None:
    """Ephemeral shell — starts a new container, removed on exit."""
    name, d = resolve_cave(args.name)
    os.execvp("docker", ["docker", "compose", "--project-directory", str(d),
                          "run", "--rm", "-it", COMPOSE_SERVICE, "zsh"])


def cmd_enter(args: argparse.Namespace) -> None:
    """Attach to a running cave (requires 'jedi up' first)."""
    name, d = resolve_cave(args.name)
    os.execvp("docker", ["docker", "compose", "--project-directory", str(d),
                          "exec", COMPOSE_SERVICE, "zsh"])


def cmd_exec(args: argparse.Namespace) -> None:
    """Run a command in a running cave (requires 'jedi up' first)."""
    name, d = resolve_cave(args.name)
    os.execvp("docker", ["docker", "compose", "--project-directory", str(d),
                          "exec", COMPOSE_SERVICE] + args.command)


def cmd_list(args: argparse.Namespace) -> None:
    caves = list_caves()
    if not caves:
        info("No caves found. Run: jedi init <name>")
        return
    for name in caves:
        d = cave_dir(name)
        # Check if container is running
        result = subprocess.run(
            ["docker", "compose", "ps", "-q", COMPOSE_SERVICE],
            cwd=d, capture_output=True, text=True
        )
        status = "running" if result.stdout.strip() else "stopped"
        print(f"  {name:20s} {status:10s} {d}")


def cmd_firewall(args: argparse.Namespace) -> None:
    name, d = resolve_cave(args.name)
    action = args.action

    if action == "on":
        config = d / "firewall-defaults.conf"
        if not config.exists():
            die(f"No firewall config at {config}")

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

        run(["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "bash", "-c", " && ".join(cmds)], cwd=d)
        success(f"Firewall enabled ({len(domains)} domains allowlisted)")

    elif action == "off":
        run(["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "bash", "-c", "iptables -F && iptables -X && iptables -P OUTPUT ACCEPT"], cwd=d)
        success("Firewall disabled")

    elif action == "status":
        run(["docker", "compose", "exec", "--user", "root", COMPOSE_SERVICE,
             "iptables", "-L", "-n", "-v"], cwd=d)


def cmd_destroy(args: argparse.Namespace) -> None:
    name, d = resolve_cave(args.name)

    # Stop container if running
    subprocess.run(["docker", "compose", "down"], cwd=d, capture_output=True)

    if args.yes or input(f"Delete cave '{name}' at {d}? [y/N] ").lower() == "y":
        shutil.rmtree(d)
        success(f"Cave '{name}' destroyed")
    else:
        info("Aborted")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jedi",
        description="Manage jedicaves — sandboxed dev containers",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p = sub.add_parser("init", help="Create a new cave")
    p.add_argument("name", help="Cave name")
    p.set_defaults(func=cmd_init)

    # build
    p = sub.add_parser("build", help="Build cave image")
    p.add_argument("name", nargs="?", help="Cave name")
    p.add_argument("--update", nargs="?", const="", default=None, metavar="INPUT",
                   help="Update flake inputs before building (optionally specify one input)")
    p.set_defaults(func=cmd_build)

    # up
    p = sub.add_parser("up", help="Start cave container")
    p.add_argument("name", nargs="?", help="Cave name")
    p.set_defaults(func=cmd_up)

    # down
    p = sub.add_parser("down", help="Stop cave container")
    p.add_argument("name", nargs="?", help="Cave name")
    p.set_defaults(func=cmd_down)

    # shell (ephemeral)
    p = sub.add_parser("shell", help="Ephemeral shell (no 'up' needed)")
    p.add_argument("name", nargs="?", help="Cave name")
    p.set_defaults(func=cmd_shell)

    # enter (a running cave)
    p = sub.add_parser("enter", help="Enter a running cave (requires 'up')")
    p.add_argument("name", nargs="?", help="Cave name")
    p.set_defaults(func=cmd_enter)

    # exec (in running cave)
    p = sub.add_parser("exec", help="Run command in running cave (requires 'up')")
    p.add_argument("name", nargs="?", help="Cave name")
    p.add_argument("command", nargs=argparse.REMAINDER, help="Command to run")
    p.set_defaults(func=cmd_exec)

    # list
    p = sub.add_parser("list", help="List caves")
    p.set_defaults(func=cmd_list)

    # firewall
    p = sub.add_parser("firewall", help="Manage cave firewall")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("name", nargs="?", help="Cave name")
    p.set_defaults(func=cmd_firewall)

    # destroy
    p = sub.add_parser("destroy", help="Delete a cave")
    p.add_argument("name", help="Cave name")
    p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    p.set_defaults(func=cmd_destroy)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
