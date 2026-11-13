#!/usr/bin/env python3
"""ssh-gui: TUI menu to quickly SSH using a local YAML inventory."""

from __future__ import annotations

import argparse
import curses
import errno
import fcntl
import locale
import os
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass

try:
    import yaml
except ImportError:  # pragma: no cover
    print(
        "PyYAML is missing. Set up the project venv:\n"
        "  python3 -m venv .venv && .venv/bin/pip install PyYAML",
        file=sys.stderr,
    )
    sys.exit(1)

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "servers.yaml"
EXAMPLE_CONFIG = APP_DIR / "servers.example.yaml"

ADD_ACTION = "__add_host__"
MenuItem = Union[str, "Server"]  # Server or ADD_ACTION


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

    def to_yaml_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "host": self.host,
            "user": self.user,
        }
        if self.port != 22:
            data["port"] = self.port
        if self.password:
            data["pass"] = self.password
        if self.identity:
            # Store with ~ if under home, for readability
            home = str(Path.home())
            ident = self.identity
            if ident.startswith(home + os.sep):
                ident = "~" + ident[len(home) :]
            data["identity"] = ident
        if self.notes:
            data["notes"] = self.notes
        return data


def expand_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return str(Path(value).expanduser())


def load_raw(config_path: Path) -> Dict[str, Any]:
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Config not found: {config_path}\n"
            f"Copy the example and fill it in:\n"
            f"  cp {EXAMPLE_CONFIG} {DEFAULT_CONFIG}\n"
            f"  chmod 600 {DEFAULT_CONFIG}"
        )
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("YAML must be a map of servers (key = name).")
    return raw


def parse_servers(raw: Dict[str, Any]) -> List[Server]:
    servers: List[Server] = []
    for name, data in raw.items():
        if not isinstance(data, dict):
            raise ValueError(f"Invalid entry for '{name}': expected a mapping.")
        host = data.get("host")
        user = data.get("user")
        if not host or not user:
            raise ValueError(f"'{name}' requires 'host' and 'user'.")
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
    return servers


def load_servers(config_path: Path) -> List[Server]:
    return parse_servers(load_raw(config_path))


def save_server(config_path: Path, server: Server) -> None:
    """Append or update a server entry in the YAML file."""
    raw: Dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if loaded and not isinstance(loaded, dict):
            raise ValueError("Existing YAML is not a server map; refusing to overwrite.")
        raw = loaded if isinstance(loaded, dict) else {}

    if server.name in raw:
        raise ValueError(f"A host named '{server.name}' already exists.")

    raw[server.name] = server.to_yaml_dict()

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass


def uses_yaml_password(server: Server) -> bool:
    """Use YAML password when set and no identity key is configured."""
    return bool(server.password) and not server.identity


def build_ssh_argv(server: Server) -> List[str]:
    ssh: List[str] = ["ssh", "-p", str(server.port)]
    if server.identity:
        ssh.extend(["-i", server.identity])
    elif uses_yaml_password(server):
        # Force password auth so OpenSSH does not hang on keys first.
        ssh.extend(
            [
                "-o",
                "PreferredAuthentications=password,keyboard-interactive",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
        )
    ssh.append(f"{server.user}@{server.host}")
    return ssh


def describe_auth(server: Server) -> str:
    if server.identity:
        return f"key: {server.identity}"
    if uses_yaml_password(server):
        return "YAML password (auto-typed at SSH prompt, no sshpass)"
    return "interactive SSH auth (no pass/key in YAML)"


_PASSWORD_RE = re.compile(
    rb"(?:password:|passphrase for key|verification code:)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_HOSTKEY_RE = re.compile(
    rb"are you sure you want to continue connecting",
    re.IGNORECASE,
)


def _copy_winsize(master_fd: int) -> None:
    if not sys.stdin.isatty():
        return
    try:
        packed = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


def _wait_exit(pid: int) -> int:
    _, status = os.waitpid(pid, 0)
    if hasattr(os, "waitstatus_to_exitcode"):
        return os.waitstatus_to_exitcode(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def ssh_with_auto_password(argv: List[str], password: str, prompt_timeout: float = 45.0) -> int:
    """
    Spawn ssh on a PTY, wait for the password prompt, send the YAML password + Enter,
    then relay the interactive session. Uses only the Python standard library.
    """
    pid, master = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            sys.stderr.write(f"Failed to exec {argv[0]}: {exc}\n")
            os._exit(127)

    # parent
    _copy_winsize(master)

    def _on_winch(_signum: int, _frame: Any) -> None:
        _copy_winsize(master)

    prev_winch = signal.signal(signal.SIGWINCH, _on_winch) if hasattr(signal, "SIGWINCH") else None

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_tty: Optional[list] = None
    if sys.stdin.isatty():
        old_tty = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    buf = b""
    password_sent = False
    deadline = time.time() + prompt_timeout

    try:
        # Phase 1: auto-answer host-key / password prompts
        while not password_sent:
            if time.time() > deadline:
                sys.stderr.write("\nTimed out waiting for the SSH password prompt.\n")
                os.kill(pid, signal.SIGTERM)
                return 1

            try:
                exited_pid, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                exited_pid = pid
            if exited_pid == pid:
                # Drain remaining output
                try:
                    while True:
                        chunk = os.read(master, 4096)
                        if not chunk:
                            break
                        os.write(stdout_fd, chunk)
                except OSError:
                    pass
                return _wait_exit(pid) if False else 1  # already reaped below
            # If child already exited, gather status once more after loop

            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not data:
                break

            # Show remote output (including "Password:") so the session looks normal
            os.write(stdout_fd, data)
            buf += data
            # Keep a sliding window so prompts at chunk boundaries still match
            if len(buf) > 8192:
                buf = buf[-4096:]

            if _HOSTKEY_RE.search(buf):
                os.write(master, b"yes\n")
                buf = b""
                continue

            if _PASSWORD_RE.search(buf):
                os.write(master, password.encode("utf-8", errors="surrogateescape") + b"\n")
                password_sent = True
                buf = b""
                deadline = time.time() + prompt_timeout  # unused after this, but clear intent

        # Phase 2: full interactive relay until ssh exits
        while True:
            try:
                exited_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return 0
            if exited_pid == pid:
                # Flush leftover PTY output
                while True:
                    r, _, _ = select.select([master], [], [], 0)
                    if not r:
                        break
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    os.write(stdout_fd, data)
                if hasattr(os, "waitstatus_to_exitcode"):
                    return os.waitstatus_to_exitcode(status)
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                if os.WIFSIGNALED(status):
                    return 128 + os.WTERMSIG(status)
                return 1

            fds = [master]
            if sys.stdin.isatty():
                fds.append(stdin_fd)
            ready, _, _ = select.select(fds, [], [], 0.2)
            if master in ready:
                try:
                    data = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                os.write(stdout_fd, data)
            if stdin_fd in ready:
                try:
                    data = os.read(stdin_fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    # stdin closed
                    try:
                        os.write(master, b"\x04")  # EOT
                    except OSError:
                        pass
                else:
                    os.write(master, data)

        return _wait_exit(pid)
    finally:
        if prev_winch is not None:
            signal.signal(signal.SIGWINCH, prev_winch)
        if old_tty is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty)
        try:
            os.close(master)
        except OSError:
            pass


def connect(server: Server, dry_run: bool = False) -> int:
    argv = build_ssh_argv(server)
    auto_pass = uses_yaml_password(server)

    print(f"→ {server.label}")
    print(f"  auth: {describe_auth(server)}")
    if server.notes:
        print(f"  notes: {server.notes}")
    print(f"  cmd: {' '.join(argv)}")
    if auto_pass:
        print("  password: (will be entered automatically at the prompt)")

    if dry_run:
        print("(dry-run: connection not opened)")
        return 0

    if auto_pass:
        assert server.password is not None
        return ssh_with_auto_password(argv, server.password)

    os.execvp(argv[0], argv)
    return 1  # unreachable


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selected row
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # hints
    curses.init_pair(3, curses.COLOR_GREEN, -1)  # [+]
    curses.init_pair(4, curses.COLOR_WHITE, -1)  # Add host text
    curses.init_pair(5, curses.COLOR_GREEN, curses.COLOR_CYAN)  # [+] selected
    curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_CYAN)  # Add host selected
    curses.init_pair(7, curses.COLOR_RED, -1)  # errors


def _menu_items(servers: List[Server]) -> List[MenuItem]:
    items: List[MenuItem] = list(servers)
    items.append(ADD_ACTION)
    return items


def _fit_cell(text: str, width: int) -> str:
    """Pad or truncate text to an exact column width."""
    if width <= 0:
        return ""
    text = text or ""
    if len(text) <= width:
        return text.ljust(width)
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


def _column_widths(servers: List[Server], term_w: int) -> Tuple[int, int, int]:
    """
    Compute name / host / notes column widths for the menu.
    Leading indent (2) + two gaps (2+2) are reserved outside these widths.
    """
    usable = max(16, term_w - 1 - 2 - 4)  # row budget minus indent/gaps
    name_needed = max((len(s.name) for s in servers), default=4)
    host_needed = max((len(s.host) for s in servers), default=4)
    name_w = min(max(name_needed, 8), 24)
    host_w = min(max(host_needed, 12), 32)
    if name_w + host_w > usable - 8:
        # Shrink proportionally on narrow terminals
        overflow = name_w + host_w - (usable - 8)
        cut_name = min(name_w - 6, overflow // 2)
        name_w -= max(0, cut_name)
        host_w = max(8, host_w - (overflow - max(0, cut_name)))
    notes_w = max(0, usable - name_w - host_w)
    return name_w, host_w, notes_w


def format_server_row(server: Server, name_w: int, host_w: int, notes_w: int) -> str:
    name = _fit_cell(server.name, name_w)
    host = _fit_cell(server.host, host_w)
    notes = _fit_cell(server.notes or "", notes_w) if notes_w > 0 else ""
    if notes_w > 0:
        return f"  {name}  {host}  {notes}"
    return f"  {name}  {host}"


def format_header_row(name_w: int, host_w: int, notes_w: int) -> str:
    name = _fit_cell("NAME", name_w)
    host = _fit_cell("HOST", host_w)
    if notes_w > 0:
        notes = _fit_cell("NOTES", notes_w)
        return f"  {name}  {host}  {notes}"
    return f"  {name}  {host}"


def _draw_add_label(stdscr: "curses._CursesWindow", y: int, w: int, selected: bool) -> None:
    """Draw '[+] Add host' with green [+] and white remaining text."""
    prefix = "  "
    plus = "[+]"
    rest = " Add host"
    x = 0
    if selected:
        plus_attr = curses.color_pair(5) | curses.A_BOLD
        rest_attr = curses.color_pair(6) | curses.A_BOLD
        # Fill selection background across the line
        try:
            stdscr.addnstr(y, 0, " " * max(0, w - 1), w - 1, curses.color_pair(1))
        except curses.error:
            pass
    else:
        plus_attr = curses.color_pair(3) | curses.A_BOLD
        rest_attr = curses.color_pair(4)

    try:
        stdscr.addnstr(y, x, prefix[: max(0, w - 1)], w - 1, rest_attr if selected else curses.A_NORMAL)
        x += len(prefix)
        if x < w - 1:
            stdscr.addnstr(y, x, plus[: max(0, w - 1 - x)], w - 1 - x, plus_attr)
            x += len(plus)
        if x < w - 1:
            stdscr.addnstr(y, x, rest[: max(0, w - 1 - x)], w - 1 - x, rest_attr)
    except curses.error:
        pass


def prompt_field(
    stdscr: "curses._CursesWindow",
    y: int,
    label: str,
    default: str = "",
    secret: bool = False,
) -> Optional[str]:
    """Single-line field editor. Returns None if cancelled (Esc)."""
    h, w = stdscr.getmaxyx()
    curses.curs_set(1)
    buf = list(default)
    pos = len(buf)
    while True:
        try:
            stdscr.move(y, 0)
            stdscr.clrtoeol()
            shown = ("*" * len(buf)) if secret else "".join(buf)
            line = f"{label}{shown}"
            stdscr.addnstr(y, 0, line[: max(0, w - 1)], w - 1)
            cursor_x = min(len(label) + pos, w - 2)
            stdscr.move(y, max(0, cursor_x))
            stdscr.refresh()
        except curses.error:
            pass

        key = stdscr.get_wch()
        if key in ("\n", "\r", curses.KEY_ENTER):
            curses.curs_set(0)
            return "".join(buf)
        if key == "\x1b":  # Esc
            curses.curs_set(0)
            return None
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if pos > 0:
                del buf[pos - 1]
                pos -= 1
        elif key == curses.KEY_LEFT:
            pos = max(0, pos - 1)
        elif key == curses.KEY_RIGHT:
            pos = min(len(buf), pos + 1)
        elif key == curses.KEY_DC:
            if pos < len(buf):
                del buf[pos]
        elif isinstance(key, str) and key.isprintable():
            buf.insert(pos, key)
            pos += 1


def add_host_form(stdscr: "curses._CursesWindow", existing_names: set) -> Optional[Server]:
    """Interactive form to create a new host. Esc cancels."""
    fields = [
        ("Name (unique id): ", "", False, True),
        ("Host (IP/hostname): ", "", False, True),
        ("User: ", "", False, True),
        ("Port [22]: ", "22", False, False),
        ("Password (optional): ", "", True, False),
        ("Identity key path (optional): ", "", False, False),
        ("Notes (optional): ", "", False, False),
    ]
    values: List[str] = [""] * len(fields)

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        title = "Add host  (Enter confirm field · Esc cancel)"
        stdscr.addnstr(0, 0, title[: max(0, w - 1)], w - 1, curses.A_BOLD)
        stdscr.addnstr(
            1,
            0,
            "Required: name, host, user. Prefer a key over a password."[: max(0, w - 1)],
            w - 1,
            curses.color_pair(2),
        )

        for i, (label, default, _secret, _req) in enumerate(fields):
            y = 3 + i
            if y >= h - 2:
                break
            shown = values[i] if values[i] else (default if i != 0 else "")
            display = ("*" * len(shown)) if _secret and shown else shown
            stdscr.addnstr(y, 0, f"  {label}{display}"[: max(0, w - 1)], w - 1)

        stdscr.refresh()

        error: Optional[str] = None
        for i, (label, default, secret, required) in enumerate(fields):
            y = 3 + i
            start = values[i] if values[i] else default
            result = prompt_field(stdscr, y, f"  {label}", start, secret=secret)
            if result is None:
                return None
            values[i] = result.strip()

        name, host, user, port_s, password, identity, notes = values
        if not name:
            error = "Name is required."
        elif name in existing_names:
            error = f"Name '{name}' already exists."
        elif not host:
            error = "Host is required."
        elif not user:
            error = "User is required."
        else:
            try:
                port = int(port_s) if port_s else 22
                if port < 1 or port > 65535:
                    raise ValueError
            except ValueError:
                error = "Port must be a number between 1 and 65535."
                port = 22

        if error:
            try:
                stdscr.addnstr(h - 1, 0, error[: max(0, w - 1)].ljust(w - 1), w - 1, curses.color_pair(7))
                stdscr.refresh()
                stdscr.getch()
            except curses.error:
                pass
            continue

        return Server(
            name=name,
            host=host,
            user=user,
            password=password or None,
            port=port,
            identity=expand_path(identity) if identity else None,
            notes=notes or None,
        )


def menu_loop(config_path: Path, dry_run: bool = False) -> Tuple[str, Optional[Server]]:
    """
    Run the TUI. Returns (action, server):
      ('connect', Server) | ('quit', None)
    """

    def _run(stdscr: "curses._CursesWindow") -> Tuple[str, Optional[Server]]:
        curses.curs_set(0)
        _init_colors()
        idx = 0

        while True:
            try:
                servers = load_servers(config_path)
            except FileNotFoundError:
                # Allow empty/missing: create skeleton so Add host works
                config_path.write_text("{}\n", encoding="utf-8")
                os.chmod(config_path, 0o600)
                servers = []
            except (ValueError, yaml.YAMLError) as exc:
                stdscr.clear()
                stdscr.addstr(0, 0, f"Config error: {exc}")
                stdscr.addstr(2, 0, "Press any key to quit.")
                stdscr.getch()
                return ("quit", None)

            items = _menu_items(servers)
            # Visual rows: servers, blank separator (if any servers), add action
            # Navigation index maps over items only (not the blank line)

            stdscr.clear()
            h, w = stdscr.getmaxyx()
            title = "ssh-gui — select a server (↑↓ Enter, q quit)"
            stdscr.addnstr(0, 0, title[: max(0, w - 1)], w - 1, curses.A_BOLD)
            if h > 2:
                stdscr.addnstr(
                    1,
                    0,
                    "Enter = connect / add · q = quit"[: max(0, w - 1)],
                    w - 1,
                    curses.color_pair(2),
                )

            name_w, host_w, notes_w = _column_widths(servers, w)
            show_header = bool(servers) and h > 4
            if show_header:
                header = format_header_row(name_w, host_w, notes_w)
                try:
                    stdscr.addnstr(
                        2,
                        0,
                        header[: max(0, w - 1)].ljust(min(w - 1, len(header))),
                        w - 1,
                        curses.A_DIM | curses.A_BOLD,
                    )
                except curses.error:
                    pass

            # Build display rows: (item_index or None for blank, kind)
            # kind: 'server' | 'blank' | 'add'
            display_rows: List[Tuple[Optional[int], str]] = []
            for i, item in enumerate(items):
                if item == ADD_ACTION:
                    if servers:
                        display_rows.append((None, "blank"))
                    display_rows.append((i, "add"))
                else:
                    display_rows.append((i, "server"))

            # Find which display row corresponds to idx for scrolling
            focus_row = next(r for r, (ii, _) in enumerate(display_rows) if ii == idx)
            top_pad = 3  # rows 0–1 chrome; row 2 header (or blank)
            visible = max(1, h - top_pad - 1)
            start_row = max(0, min(focus_row - visible + 1, len(display_rows) - visible))

            for row_i, (item_i, kind) in enumerate(display_rows[start_row : start_row + visible]):
                y = top_pad + row_i
                if y >= h:
                    break
                if kind == "blank":
                    continue
                selected = item_i == idx
                if kind == "add":
                    _draw_add_label(stdscr, y, w, selected)
                else:
                    server = items[item_i]  # type: ignore[index]
                    assert isinstance(server, Server)
                    line = format_server_row(server, name_w, host_w, notes_w)
                    attr = curses.color_pair(1) | curses.A_BOLD if selected else curses.A_NORMAL
                    try:
                        stdscr.addnstr(
                            y,
                            0,
                            line[: max(0, w - 1)].ljust(min(w - 1, max(len(line), 2))),
                            w - 1,
                            attr,
                        )
                    except curses.error:
                        pass

            stdscr.refresh()
            key = stdscr.getch()
            n = len(items)
            if key in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % n
            elif key in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % n
            elif key in (curses.KEY_ENTER, 10, 13):
                chosen = items[idx]
                if chosen == ADD_ACTION:
                    existing = {s.name for s in servers}
                    new_server = add_host_form(stdscr, existing)
                    if new_server is None:
                        continue
                    try:
                        save_server(config_path, new_server)
                    except ValueError as exc:
                        stdscr.clear()
                        stdscr.addstr(0, 0, f"Could not save: {exc}")
                        stdscr.addstr(2, 0, "Press any key…")
                        stdscr.getch()
                        continue
                    # Stay in menu; select the new host
                    servers = load_servers(config_path)
                    items = _menu_items(servers)
                    for i, it in enumerate(items):
                        if isinstance(it, Server) and it.name == new_server.name:
                            idx = i
                            break
                    continue
                assert isinstance(chosen, Server)
                return ("connect", chosen)
            elif key in (ord("q"), 27):
                return ("quit", None)

    return curses.wrapper(_run)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ssh-gui",
        description="TUI menu to SSH using a local YAML inventory.",
    )
    p.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"YAML config file (default: {DEFAULT_CONFIG})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the command without running ssh.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List servers and exit (no TUI).",
    )
    p.add_argument(
        "--connect",
        metavar="NAME",
        help="Connect directly to this server name (no menu).",
    )
    p.add_argument(
        "--add",
        action="store_true",
        help="Add a host via prompts (no TUI menu).",
    )
    return p.parse_args(argv)


def add_host_cli(config_path: Path) -> int:
    """Non-TUI prompts to add a host (for scripting / dry terminals)."""
    try:
        existing = {s.name for s in load_servers(config_path)} if config_path.is_file() else set()
    except (ValueError, yaml.YAMLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    def ask(label: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        val = input(f"{label}{suffix}: ").strip()
        return val if val else default

    print("Add host (leave optional fields empty to skip)")
    name = ask("Name (unique id)")
    host = ask("Host (IP/hostname)")
    user = ask("User")
    port_s = ask("Port", "22")
    password = ask("Password (optional)")
    identity = ask("Identity key path (optional)")
    notes = ask("Notes (optional)")

    if not name or not host or not user:
        print("Name, host and user are required.", file=sys.stderr)
        return 1
    if name in existing:
        print(f"A host named '{name}' already exists.", file=sys.stderr)
        return 1
    try:
        port = int(port_s)
    except ValueError:
        print("Invalid port.", file=sys.stderr)
        return 1

    server = Server(
        name=name,
        host=host,
        user=user,
        password=password or None,
        port=port,
        identity=expand_path(identity) if identity else None,
        notes=notes or None,
    )
    try:
        save_server(config_path, server)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved '{name}' to {config_path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if args.add:
        return add_host_cli(args.config)

    if args.list:
        if not args.config.is_file():
            print(f"Error: config not found: {args.config}", file=sys.stderr)
            return 1
        try:
            servers = load_servers(args.config)
        except (ValueError, yaml.YAMLError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
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
        try:
            servers = load_servers(args.config)
        except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        by_name = {s.name: s for s in servers}
        server = by_name.get(args.connect)
        if not server:
            print(f"Unknown server '{args.connect}'.", file=sys.stderr)
            return 1
        return connect(server, dry_run=args.dry_run)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "No TTY. Use --list, --connect NAME, --add, or open an interactive terminal.",
            file=sys.stderr,
        )
        return 1

    action, chosen = menu_loop(args.config, dry_run=args.dry_run)
    if action == "quit" or chosen is None:
        print("Cancelled.")
        return 0
    return connect(chosen, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
