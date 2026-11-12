#!/usr/bin/env python3
"""ssh-gui: menú TUI per connectar ràpidament via SSH des d'un inventari YAML."""

from __future__ import annotations

import argparse
import curses
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - entorn trencat
    print(
        "Falta PyYAML. Instal·la el venv del projecte:\n"
        "  python3 -m venv .venv && .venv/bin/pip install PyYAML",
        file=sys.stderr,
    )
    sys.exit(1)

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "servers.yaml"
EXAMPLE_CONFIG = APP_DIR / "servers.example.yaml"


@dataclass
class Server:
    name: str
    host: str
    user: str
    password: Optional[str] = None
    port: int = 22
    identity: Optional[str] = None
    notes: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.name}  ({self.host})"


def expand_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return str(Path(value).expanduser())


def load_servers(config_path: Path) -> List[Server]:
    if not config_path.is_file():
        raise FileNotFoundError(
            f"No s'ha trobat {config_path}.\n"
            f"Copia l'exemple i omple'l:\n"
            f"  cp {EXAMPLE_CONFIG} {DEFAULT_CONFIG}\n"
            f"  chmod 600 {DEFAULT_CONFIG}"
        )

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict):
        raise ValueError("El YAML ha de ser un mapa de servidors (clau = nom).")

    servers: List[Server] = []
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ValueError(f"Entrada invàlida per a '{name}': s'espera un objecte.")
        host = data.get("host")
        user = data.get("user")
        if not host or not user:
            raise ValueError(f"'{name}' necessita 'host' i 'user'.")
        port = int(data.get("port", 22))
        servers.append(
            Server(
                name=str(name),
                host=str(host),
                user=str(user),
                password=(str(data["pass"]) if data.get("pass") else None),
                port=port,
                identity=expand_path(data.get("identity") or data.get("key")),
                notes=(str(data["notes"]) if data.get("notes") else None),
            )
        )

    if not servers:
        raise ValueError(f"{config_path} no conté cap servidor.")
    return servers


def build_ssh_argv(server: Server) -> List[str]:
    """Construeix l'argv d'ssh (i sshpass si cal)."""
    ssh: List[str] = ["ssh", "-p", str(server.port)]
    if server.identity:
        ssh.extend(["-i", server.identity])
    # Preferim clau si hi ha identity; pass només si no n'hi ha o com a fallback explícit
    use_password = bool(server.password) and not server.identity
    target = f"{server.user}@{server.host}"
    ssh.append(target)

    if use_password:
        if shutil.which("sshpass"):
            return ["sshpass", "-p", server.password, *ssh]
        # Sense sshpass: deixem que ssh demani la contrasenya (no posem -o BatchMode)
        return ssh

    if server.identity:
        # Evita demanar pass de clau si no cal; l'usuari pot treure BatchMode si cal
        return ssh

    return ssh


def describe_auth(server: Server) -> str:
    if server.identity:
        return f"clau: {server.identity}"
    if server.password:
        if shutil.which("sshpass"):
            return "contrasenya YAML via sshpass"
        return "contrasenya YAML (sshpass NO trobat → SSH demanarà el pass)"
    return "auth interactiva (sense pass ni clau al YAML)"


def connect(server: Server, dry_run: bool = False) -> int:
    argv = build_ssh_argv(server)
    display = list(argv)
    # No mostrem la contrasenya en clar a dry-run / logs
    if display and display[0] == "sshpass" and len(display) >= 3 and display[1] == "-p":
        display[2] = "***"

    print(f"→ {server.label}")
    print(f"  auth: {describe_auth(server)}")
    if server.notes:
        print(f"  notes: {server.notes}")
    print(f"  cmd: {' '.join(display)}")

    if dry_run:
        print("(dry-run: no s'ha obert la connexió)")
        return 0

    if server.password and not server.identity and not shutil.which("sshpass"):
        print(
            "\nAvís: tens 'pass' al YAML però 'sshpass' no està instal·lat.\n"
            "  brew install hudochenkov/sshpass/sshpass\n"
            "o bé afegeix 'identity: ~/.ssh/id_ed25519' i usa clau.\n"
            "Mentrestant SSH et demanarà la contrasenya a mà.\n",
            file=sys.stderr,
        )

    # Entrega el control al terminal per a la sessió SSH
    os.execvp(argv[0], argv)
    return 1  # unreachable


def menu_select(servers: List[Server]) -> Optional[Server]:
    def _draw(stdscr: "curses._CursesWindow") -> Optional[Server]:
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)

        idx = 0
        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            title = "ssh-gui — selecciona un servidor (↑↓ Enter, q surt)"
            stdscr.addnstr(0, 0, title[: max(0, w - 1)], w - 1, curses.A_BOLD)
            hint = "Enter = connectar · q = sortir · / no cal"
            if h > 2:
                stdscr.addnstr(1, 0, hint[: max(0, w - 1)], w - 1, curses.color_pair(2))

            visible = max(1, h - 4)
            start = max(0, min(idx - visible + 1, len(servers) - visible))
            for row, server in enumerate(servers[start : start + visible]):
                line = f"  {server.label}"
                if server.notes:
                    line += f"  — {server.notes}"
                y = row + 3
                if y >= h:
                    break
                attr = curses.color_pair(1) | curses.A_BOLD if (start + row) == idx else curses.A_NORMAL
                stdscr.addnstr(y, 0, line[: max(0, w - 1)].ljust(min(w - 1, len(line) + 2)), w - 1, attr)

            stdscr.refresh()
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % len(servers)
            elif key in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % len(servers)
            elif key in (curses.KEY_ENTER, 10, 13):
                return servers[idx]
            elif key in (ord("q"), 27):
                return None

    return curses.wrapper(_draw)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ssh-gui",
        description="Menú TUI per connectar per SSH amb inventari YAML local.",
    )
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Fitxer YAML (per defecte: {DEFAULT_CONFIG})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra la comanda sense executar ssh.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="Llista servidors i surt (sense TUI).",
    )
    p.add_argument(
        "--connect",
        metavar="NOM",
        help="Connecta directament al servidor amb aquest nom (sense menú).",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        servers = load_servers(args.config)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    by_name: Dict[str, Server] = {s.name: s for s in servers}

    if args.list:
        for s in servers:
            extra = []
            if s.identity:
                extra.append("key")
            elif s.password:
                extra.append("pass")
            note = f" — {s.notes}" if s.notes else ""
            auth = f" [{', '.join(extra)}]" if extra else ""
            print(f"{s.name}\t{s.user}@{s.host}:{s.port}{auth}{note}")
        return 0

    if args.connect:
        server = by_name.get(args.connect)
        if not server:
            print(f"No existeix el servidor '{args.connect}'.", file=sys.stderr)
            return 1
        return connect(server, dry_run=args.dry_run)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "No hi ha TTY. Usa --list, --connect NOM o obre un terminal interactiu.",
            file=sys.stderr,
        )
        return 1

    chosen = menu_select(servers)
    if chosen is None:
        print("Cancel·lat.")
        return 0
    return connect(chosen, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
