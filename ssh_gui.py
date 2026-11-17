#!/usr/bin/env python3
"""ssh-gui: TUI menu to quickly SSH using a local YAML inventory."""

from __future__ import annotations

import argparse
import curses
import errno
import fcntl
import locale
import os
import pty
import re
import select
import shlex
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

TITLE_ART = [
    "  " + line
    for line in r"""
              __                      _ 
   __________/ /_        ____ ___  __(_)
  / ___/ ___/ __ \______/ __ `/ / / / / 
 (__  |__  ) / / /_____/ /_/ / /_/ / /  
/____/____/_/ /_/      \__, /\__,_/_/   
                      /____/            
""".splitlines()[1:]
]

SUBTITLE_TEXT = "  By Walker #61899"
# Banner = title lines (subtitle shares the last title row) + blank before list
BANNER_LINE_COUNT = len(TITLE_ART) + 1
COMPACT_TITLE = "ssh-gui"
# Spacing between NAME / HOST / NOTES (tripled from the original 2 spaces)
COL_GAP = "      "  # 6 spaces
COL_GAP_LEN = len(COL_GAP)


def _home_tilde(path: str) -> str:
    home = str(Path.home())
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


@dataclass
class Server:
    name: str
    host: str
    user: str
    password: Optional[str] = None
    port: int = 22
    identity: Optional[str] = None
    notes: Optional[str] = None
    web_remote: Optional[str] = None
    web_local: Optional[str] = None
    git_user: Optional[str] = None
    git_token: Optional[str] = None
    remi_php: Optional[str] = None  # e.g. php83 — cached Remi enable from Vendor Drush

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
            data["identity"] = _home_tilde(self.identity)
        if self.notes:
            data["notes"] = self.notes
        if self.web_remote:
            data["web_remote"] = self.web_remote
        if self.web_local:
            data["web_local"] = _home_tilde(self.web_local)
        if self.git_user:
            data["git_user"] = self.git_user
        if self.git_token:
            data["git_token"] = self.git_token
        if self.remi_php:
            data["remi_php"] = self.remi_php
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
                web_remote=(str(data["web_remote"]) if data.get("web_remote") else None),
                web_local=expand_path(str(data["web_local"])) if data.get("web_local") else None,
                git_user=(str(data["git_user"]) if data.get("git_user") else None),
                git_token=(str(data["git_token"]) if data.get("git_token") else None),
                remi_php=(str(data["remi_php"]) if data.get("remi_php") else None),
            )
        )
    return servers


def load_servers(config_path: Path) -> List[Server]:
    return parse_servers(load_raw(config_path))


def _read_raw_map(config_path: Path) -> Dict[str, Any]:
    raw: Dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if loaded and not isinstance(loaded, dict):
            raise ValueError("Existing YAML is not a server map; refusing to overwrite.")
        raw = loaded if isinstance(loaded, dict) else {}
    return raw


def _write_raw_map(config_path: Path, raw: Dict[str, Any]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass


def save_server(config_path: Path, server: Server) -> None:
    """Append a new server entry to the YAML file."""
    raw = _read_raw_map(config_path)
    if server.name in raw:
        raise ValueError(f"A host named '{server.name}' already exists.")
    raw[server.name] = server.to_yaml_dict()
    _write_raw_map(config_path, raw)


def update_server(config_path: Path, original_name: str, server: Server) -> None:
    """Update an existing server; supports renaming while preserving order."""
    raw = _read_raw_map(config_path)
    if original_name not in raw:
        raise ValueError(f"Host '{original_name}' was not found in the config.")
    if server.name != original_name and server.name in raw:
        raise ValueError(f"A host named '{server.name}' already exists.")

    new_raw: Dict[str, Any] = {}
    for key, value in raw.items():
        if key == original_name:
            new_raw[server.name] = server.to_yaml_dict()
        else:
            new_raw[key] = value
    _write_raw_map(config_path, new_raw)


def delete_server(config_path: Path, name: str) -> None:
    """Remove a server entry from the YAML file."""
    raw = _read_raw_map(config_path)
    if name not in raw:
        raise ValueError(f"Host '{name}' was not found in the config.")
    del raw[name]
    _write_raw_map(config_path, raw)


def _identity_display(path: Optional[str]) -> str:
    if not path:
        return ""
    home = str(Path.home())
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


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
    remote_cd = (server.web_remote or "").strip()
    if len(remote_cd) > 1:
        remote_cd = remote_cd.rstrip("/")
    if remote_cd:
        ssh.append("-t")
    ssh.append(f"{server.user}@{server.host}")
    if remote_cd:
        # Start interactive shell in web_remote when set.
        ssh.append(f"cd {shlex.quote(remote_cd)} && exec $SHELL -l")
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
                exited_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return 1
            if exited_pid == pid:
                # Drain remaining output; child already reaped
                try:
                    while True:
                        chunk = os.read(master, 4096)
                        if not chunk:
                            break
                        os.write(stdout_fd, chunk)
                except OSError:
                    pass
                if hasattr(os, "waitstatus_to_exitcode"):
                    return os.waitstatus_to_exitcode(status)
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                return 1

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
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selected row (blue/cyan)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # hints
    curses.init_pair(3, curses.COLOR_GREEN, -1)  # [+]
    curses.init_pair(4, curses.COLOR_WHITE, -1)  # white text / title
    curses.init_pair(5, curses.COLOR_GREEN, curses.COLOR_CYAN)  # [+] on cyan
    curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_CYAN)  # text on cyan
    curses.init_pair(7, curses.COLOR_RED, -1)  # errors / red mark / [-]
    # Gray subtitle: prefer color 8 (bright black) when the terminal has it
    gray = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
    curses.init_pair(8, gray, -1)
    curses.init_pair(9, curses.COLOR_WHITE, curses.COLOR_RED)  # remove focused
    curses.init_pair(10, curses.COLOR_RED, curses.COLOR_RED)  # [-] on red bg
    curses.init_pair(11, curses.COLOR_WHITE, curses.COLOR_GREEN)  # save focused
    curses.init_pair(12, curses.COLOR_GREEN, curses.COLOR_GREEN)  # [+] on green bg
    curses.init_pair(13, curses.COLOR_BLACK, curses.COLOR_CYAN)  # asterisk on selected row
    curses.init_pair(14, curses.COLOR_YELLOW, -1)  # [-] discard
    curses.init_pair(15, curses.COLOR_WHITE, curses.COLOR_YELLOW)  # discard focused


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
    Leading indent (2) + two COL_GAP gaps are reserved outside these widths.
    """
    gaps = COL_GAP_LEN * 2
    usable = max(16, term_w - 1 - 2 - gaps)  # row budget minus indent/gaps
    name_needed = max((len(s.name) for s in servers), default=4)
    host_needed = max((len(s.host) for s in servers), default=4)
    name_w = min(max(name_needed, 8), 24)
    host_w = min(max(host_needed, 12), 32)
    min_notes = 8
    if name_w + host_w > usable - min_notes:
        # Shrink proportionally on narrow terminals
        overflow = name_w + host_w - (usable - min_notes)
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
        return f"  {name}{COL_GAP}{host}{COL_GAP}{notes}"
    return f"  {name}{COL_GAP}{host}"


def format_header_row(name_w: int, host_w: int, notes_w: int) -> str:
    name = _fit_cell("NAME", name_w)
    host = _fit_cell("HOST", host_w)
    if notes_w > 0:
        notes = _fit_cell("NOTES", notes_w)
        return f"  {name}{COL_GAP}{host}{COL_GAP}{notes}"
    return f"  {name}{COL_GAP}{host}"


def _safe_add(stdscr: "curses._CursesWindow", y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w - 1:
        return
    try:
        stdscr.addnstr(y, x, text[: max(0, w - 1 - x)], w - 1 - x, attr)
    except curses.error:
        pass


def draw_banner(stdscr: "curses._CursesWindow", term_h: int, term_w: int) -> int:
    """
    Draw white TITLE_ART; gray subtitle shares the last title row, then a blank spacer.
    Returns the next free row index (top of list chrome).
    Falls back to a one-line compact title when the terminal is too short.
    """
    need = BANNER_LINE_COUNT + 3  # banner + header + ≥2 list rows
    use_full = term_h >= need and term_w >= 40

    title_attr = curses.color_pair(4) | curses.A_BOLD
    # Gray: color pair 8; dim further when we fell back to white
    sub_attr = curses.color_pair(8)
    if curses.COLORS < 16:
        sub_attr |= curses.A_DIM

    y = 0
    if not use_full:
        _safe_add(stdscr, y, 0, COMPACT_TITLE[: max(0, term_w - 1)], title_attr)
        return 2  # blank row 1 for spacing

    last_i = len(TITLE_ART) - 1
    for i, line in enumerate(TITLE_ART):
        if i != last_i:
            _safe_add(stdscr, y, 0, line, title_attr)
        else:
            # e.g. "  By Walker #61899      /____/"
            art_tail = line.lstrip()
            _safe_add(stdscr, y, 0, SUBTITLE_TEXT, sub_attr)
            _safe_add(stdscr, y, len(SUBTITLE_TEXT), COL_GAP + art_tail, title_attr)
        y += 1
    y += 1  # blank before list / column header
    return y


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


# Form field keys / labels. required → red asterisk while empty. secret → mask value.
FORM_FIELDS: List[Tuple[str, str, bool, bool]] = [
    ("name", "Name", True, False),
    ("host", "Host", True, False),
    ("user", "User", True, False),
    ("port", "Port", False, False),
    ("password", "Password", False, True),
    ("identity", "Identity", False, False),
    ("notes", "Notes", False, False),
    ("web_remote", "Web remote", False, False),
    ("web_local", "Web local", False, False),
    ("git_user", "Git user", False, False),
    ("git_token", "Git token", False, True),
]
FORM_LABEL_W = 12
ACTION_REMOVE = "remove"
ACTION_DISCARD = "discard"
ACTION_SAVE = "save"


def _confirm_dialog(stdscr: "curses._CursesWindow", message: str) -> bool:
    """Simple Enter=yes / Esc=no confirm strip at the bottom."""
    h, w = stdscr.getmaxyx()
    prompt = f"{message}  Enter=yes  Esc=no"
    try:
        stdscr.addnstr(
            h - 1,
            0,
            prompt[: max(0, w - 1)].ljust(w - 1),
            w - 1,
            curses.color_pair(2) | curses.A_BOLD,
        )
        stdscr.refresh()
    except curses.error:
        pass
    while True:
        key = stdscr.getch()
        if key in (curses.KEY_ENTER, 10, 13, ord("y"), ord("Y")):
            return True
        if key in (27, ord("n"), ord("N"), ord("q")):
            return False


def _commit_field_buf(values: Dict[str, str], key: str, buf: List[str]) -> None:
    text = "".join(buf)
    values[key] = text if key == "password" else text.strip()


def _build_server_from_values(
    values: Dict[str, str],
    blocked_names: set,
    *,
    remi_php: Optional[str] = None,
) -> Tuple[Optional[Server], Optional[str]]:
    """Validate form values → (Server, None) or (None, error)."""
    name = values["name"].strip()
    host = values["host"].strip()
    user = values["user"].strip()
    port_s = values["port"].strip()
    password = values.get("password", "")
    identity = values.get("identity", "").strip()
    notes = values.get("notes", "").strip()
    web_remote = values.get("web_remote", "").strip()
    web_local = values.get("web_local", "").strip()
    git_user = values.get("git_user", "").strip()
    git_token = values.get("git_token", "")

    if not name:
        return None, "Name is required."
    if name in blocked_names:
        return None, f"Name '{name}' already exists."
    if not host:
        return None, "Host is required."
    if not user:
        return None, "User is required."
    try:
        port = int(port_s) if port_s else 22
        if port < 1 or port > 65535:
            raise ValueError
    except ValueError:
        return None, "Port must be a number between 1 and 65535."

    return (
        Server(
            name=name,
            host=host,
            user=user,
            password=password or None,
            port=port,
            identity=expand_path(identity) if identity else None,
            notes=notes or None,
            web_remote=web_remote or None,
            web_local=expand_path(web_local) if web_local else None,
            git_user=git_user or None,
            git_token=git_token or None,
            remi_php=remi_php or None,
        ),
        None,
    )


def host_form(
    stdscr: "curses._CursesWindow",
    existing_names: set,
    initial: Optional[Server] = None,
) -> Optional[Tuple[str, Optional[Server]]]:
    """
    Navigate fields with ↑↓; typing edits the focused field immediately.
    Enter commits the field and moves to the next row (or runs an action).
    Returns ('save', Server), ('remove', None), or None if cancelled.
    """
    editing = initial is not None
    if initial:
        values: Dict[str, str] = {
            "name": initial.name,
            "host": initial.host,
            "user": initial.user,
            "port": str(initial.port),
            "password": initial.password or "",
            "identity": _identity_display(initial.identity),
            "notes": initial.notes or "",
            "web_remote": initial.web_remote or "",
            "web_local": _identity_display(initial.web_local),
            "git_user": initial.git_user or "",
            "git_token": initial.git_token or "",
        }
        blocked_names = set(existing_names) - {initial.name}
        original_name = initial.name
    else:
        values = {key: "" for key, *_ in FORM_FIELDS}
        values["port"] = "22"
        blocked_names = set(existing_names)
        original_name = ""

    # Selectable rows: fields then actions (Save first)
    # Edit → Save + Remove Host; Add → Save + Discard changes
    actions: List[str] = (
        [ACTION_SAVE, ACTION_REMOVE] if editing else [ACTION_SAVE, ACTION_DISCARD]
    )
    n_fields = len(FORM_FIELDS)
    n_items = n_fields + len(actions)
    idx = 0
    error: Optional[str] = None

    # Live buffer for the focused field (typing applies immediately)
    buf: List[str] = list(values[FORM_FIELDS[0][0]])
    pos = len(buf)
    curses.curs_set(1)

    def focus_row(new_idx: int) -> None:
        nonlocal idx, buf, pos
        if idx < n_fields:
            _commit_field_buf(values, FORM_FIELDS[idx][0], buf)
        idx = new_idx % n_items
        if idx < n_fields:
            buf = list(values[FORM_FIELDS[idx][0]])
            pos = len(buf)
            curses.curs_set(1)
        else:
            buf = []
            pos = 0
            curses.curs_set(0)

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        title = "Edit host" if editing else "Add host"
        focused_key = FORM_FIELDS[idx][0] if idx < n_fields else ""
        if focused_key == "web_local":
            hint = "↑↓ move  type path  Enter browse folders  Esc cancel"
        else:
            hint = "↑↓ move  type to edit  Enter next/action  Esc cancel"
        _safe_add(stdscr, 0, 0, title, curses.A_BOLD)
        _safe_add(stdscr, 1, 0, hint, curses.color_pair(2))

        label_w = FORM_LABEL_W
        value_x = 2 + label_w + 3  # "  LABEL  " + gap before value
        mark_x = 2 + label_w

        field_top = 3
        cursor_y = field_top
        cursor_x = value_x
        for i, (key, label, required, secret) in enumerate(FORM_FIELDS):
            y = field_top + i
            if y >= h - 1:
                break
            selected = idx == i
            if selected:
                raw_val = "".join(buf)
            else:
                raw_val = values[key]
            display_val = ("*" * len(raw_val)) if secret and raw_val else raw_val
            if key == "port" and not display_val:
                display_val = "22"

            if selected:
                try:
                    stdscr.addnstr(y, 0, " " * max(0, w - 1), w - 1, curses.color_pair(1))
                except curses.error:
                    pass
                lab_attr = curses.color_pair(1) | curses.A_BOLD
                val_attr = curses.color_pair(1) | curses.A_BOLD
                star_attr = curses.color_pair(13) | curses.A_BOLD
            else:
                lab_attr = curses.A_NORMAL
                val_attr = curses.color_pair(4)
                star_attr = curses.color_pair(7) | curses.A_BOLD

            _safe_add(stdscr, y, 2, _fit_cell(label, label_w), lab_attr)
            needs_star = required and not raw_val.strip()
            if needs_star:
                _safe_add(stdscr, y, mark_x, "*", star_attr)
            else:
                _safe_add(stdscr, y, mark_x, " ", lab_attr if selected else curses.A_NORMAL)

            avail = max(1, w - 1 - value_x)
            if selected:
                view_start = max(0, pos - avail + 1)
                view = display_val[view_start : view_start + avail]
                _safe_add(stdscr, y, value_x, view.ljust(avail)[:avail], val_attr)
                cursor_y = y
                cursor_x = value_x + min(pos - view_start, avail - 1)
            else:
                _safe_add(stdscr, y, value_x, display_val[:avail], val_attr)

        # Blank line, then actions
        action_top = field_top + n_fields + 1
        for a_i, action in enumerate(actions):
            y = action_top + a_i
            item_i = n_fields + a_i
            selected = idx == item_i
            if y >= h - 1:
                break
            if action == ACTION_REMOVE:
                mark, rest = "[-]", " Remove Host"
                if selected:
                    try:
                        stdscr.addnstr(y, 0, " " * max(0, w - 1), w - 1, curses.color_pair(9))
                    except curses.error:
                        pass
                    _safe_add(stdscr, y, 2, mark, curses.color_pair(9) | curses.A_BOLD)
                    _safe_add(stdscr, y, 2 + len(mark), rest, curses.color_pair(9) | curses.A_BOLD)
                else:
                    _safe_add(stdscr, y, 2, mark, curses.color_pair(7) | curses.A_BOLD)
                    _safe_add(stdscr, y, 2 + len(mark), rest, curses.color_pair(4))
            elif action == ACTION_DISCARD:
                mark, rest = "[-]", " Discard changes"
                if selected:
                    try:
                        stdscr.addnstr(y, 0, " " * max(0, w - 1), w - 1, curses.color_pair(15))
                    except curses.error:
                        pass
                    _safe_add(stdscr, y, 2, mark, curses.color_pair(15) | curses.A_BOLD)
                    _safe_add(stdscr, y, 2 + len(mark), rest, curses.color_pair(15) | curses.A_BOLD)
                else:
                    _safe_add(stdscr, y, 2, mark, curses.color_pair(14) | curses.A_BOLD)
                    _safe_add(stdscr, y, 2 + len(mark), rest, curses.color_pair(4))
            else:  # SAVE
                mark, rest = "[+]", " Save changes"
                if selected:
                    try:
                        stdscr.addnstr(y, 0, " " * max(0, w - 1), w - 1, curses.color_pair(11))
                    except curses.error:
                        pass
                    _safe_add(stdscr, y, 2, mark, curses.color_pair(11) | curses.A_BOLD)
                    _safe_add(stdscr, y, 2 + len(mark), rest, curses.color_pair(11) | curses.A_BOLD)
                else:
                    _safe_add(stdscr, y, 2, mark, curses.color_pair(3) | curses.A_BOLD)
                    _safe_add(stdscr, y, 2 + len(mark), rest, curses.color_pair(4))

        if error:
            try:
                stdscr.addnstr(
                    h - 1,
                    0,
                    error[: max(0, w - 1)].ljust(w - 1),
                    w - 1,
                    curses.color_pair(7) | curses.A_BOLD,
                )
            except curses.error:
                pass

        if idx < n_fields:
            try:
                stdscr.move(cursor_y, max(0, min(cursor_x, w - 2)))
            except curses.error:
                pass
        stdscr.refresh()

        key = stdscr.get_wch()

        if key in (curses.KEY_UP,):
            focus_row(idx - 1)
            error = None
        elif key in (curses.KEY_DOWN,):
            focus_row(idx + 1)
            error = None
        elif key == "\x1b":
            return None
        elif key in ("\n", "\r", curses.KEY_ENTER):
            if idx < n_fields:
                key_name = FORM_FIELDS[idx][0]
                _commit_field_buf(values, key_name, buf)
                if key_name == "web_local":
                    # Directory picker owns durable web_local config
                    from host_actions import pick_local_directory

                    start = values["web_local"] or None
                    picked = pick_local_directory(stdscr, start)
                    if picked is not None:
                        values["web_local"] = _identity_display(picked) or picked
                        buf = list(values["web_local"])
                        pos = len(buf)
                    else:
                        buf = list(values["web_local"])
                        pos = len(buf)
                    curses.curs_set(1)
                    error = None
                else:
                    focus_row(idx + 1)
                    error = None
            else:
                action = actions[idx - n_fields]
                if action == ACTION_DISCARD:
                    return None  # cancel add → back to main menu, nothing saved
                if action == ACTION_REMOVE:
                    label = values["name"].strip() or original_name or "this host"
                    if _confirm_dialog(stdscr, f"Remove '{label}'?"):
                        return ("remove", None)
                    error = None
                    curses.curs_set(0)
                elif action == ACTION_SAVE:
                    missing = [
                        label
                        for key, label, required, _s in FORM_FIELDS
                        if required and not values[key].strip()
                    ]
                    if missing:
                        error = f"Required: {', '.join(missing)}"
                        for i, (key, _l, required, _s) in enumerate(FORM_FIELDS):
                            if required and not values[key].strip():
                                focus_row(i)
                                break
                        continue
                    server, err = _build_server_from_values(
                        values,
                        blocked_names,
                        remi_php=(initial.remi_php if initial else None),
                    )
                    if err or server is None:
                        error = err or "Could not save."
                        continue
                    return ("save", server)
        elif idx < n_fields:
            # Immediate inline editing on the focused field
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if pos > 0:
                    del buf[pos - 1]
                    pos -= 1
                error = None
            elif key == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif key == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif key == curses.KEY_DC:
                if pos < len(buf):
                    del buf[pos]
                error = None
            elif key == curses.KEY_HOME:
                pos = 0
            elif key == curses.KEY_END:
                pos = len(buf)
            elif isinstance(key, str) and key.isprintable():
                buf.insert(pos, key)
                pos += 1
                error = None
        else:
            # On action rows, allow j/k as vim-style navigation
            if key in ("k", "K"):
                focus_row(idx - 1)
                error = None
            elif key in ("j", "J"):
                focus_row(idx + 1)
                error = None
            elif key in ("q", "Q"):
                return None


def add_host_form(
    stdscr: "curses._CursesWindow",
    existing_names: set,
) -> Optional[Tuple[str, Optional[Server]]]:
    return host_form(stdscr, existing_names, initial=None)


def edit_host_form(
    stdscr: "curses._CursesWindow",
    existing_names: set,
    server: Server,
) -> Optional[Tuple[str, Optional[Server]]]:
    return host_form(stdscr, existing_names, initial=server)


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
            list_top = draw_banner(stdscr, h, w)

            name_w, host_w, notes_w = _column_widths(servers, w)
            show_header = bool(servers) and (h - list_top) > 2
            content_top = list_top
            if show_header:
                header = format_header_row(name_w, host_w, notes_w)
                _safe_add(
                    stdscr,
                    list_top,
                    0,
                    header.ljust(min(w - 1, max(len(header), 2))),
                    curses.A_DIM | curses.A_BOLD,
                )
                content_top = list_top + 1

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
            visible = max(1, h - content_top - 1)
            start_row = max(0, min(focus_row - visible + 1, len(display_rows) - visible))

            for row_i, (item_i, kind) in enumerate(display_rows[start_row : start_row + visible]):
                y = content_top + row_i
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
                    _safe_add(
                        stdscr,
                        y,
                        0,
                        line.ljust(min(w - 1, max(len(line), 2))),
                        attr,
                    )

            stdscr.refresh()
            key = stdscr.getch()
            n = len(items)
            if key in (curses.KEY_UP, ord("k")):
                idx = (idx - 1) % n
            elif key in (curses.KEY_DOWN, ord("j")):
                idx = (idx + 1) % n
            elif key == curses.KEY_RIGHT:
                chosen = items[idx]
                if chosen == ADD_ACTION:
                    continue
                assert isinstance(chosen, Server)
                # Late import avoids circular load at startup
                from host_actions import host_actions_menu

                host_actions_menu(stdscr, config_path, chosen)
                # Reload in case actions patched yaml
                servers = load_servers(config_path)
                items = _menu_items(servers)
                # Keep selection on same host name when possible
                for i, it in enumerate(items):
                    if isinstance(it, Server) and it.name == chosen.name:
                        idx = i
                        break
                else:
                    idx = min(idx, max(0, len(items) - 1))
                continue
            elif key in (curses.KEY_ENTER, 10, 13):
                chosen = items[idx]
                if chosen == ADD_ACTION:
                    existing = {s.name for s in servers}
                    result = add_host_form(stdscr, existing)
                    if result is None:
                        continue
                    action, new_server = result
                    if action != "save" or new_server is None:
                        continue
                    try:
                        save_server(config_path, new_server)
                    except ValueError as exc:
                        stdscr.clear()
                        stdscr.addstr(0, 0, f"Could not save: {exc}")
                        stdscr.addstr(2, 0, "Press any key…")
                        stdscr.getch()
                        continue
                    servers = load_servers(config_path)
                    items = _menu_items(servers)
                    for i, it in enumerate(items):
                        if isinstance(it, Server) and it.name == new_server.name:
                            idx = i
                            break
                    continue
                assert isinstance(chosen, Server)
                return ("connect", chosen)
            elif key in (ord("e"), ord("E")):
                chosen = items[idx]
                if chosen == ADD_ACTION:
                    continue  # edit only applies to host rows
                assert isinstance(chosen, Server)
                existing = {s.name for s in servers}
                result = edit_host_form(stdscr, existing, chosen)
                if result is None:
                    continue
                action, edited = result
                if action == "remove":
                    try:
                        delete_server(config_path, chosen.name)
                    except ValueError as exc:
                        stdscr.clear()
                        stdscr.addstr(0, 0, f"Could not remove: {exc}")
                        stdscr.addstr(2, 0, "Press any key…")
                        stdscr.getch()
                        continue
                    servers = load_servers(config_path)
                    items = _menu_items(servers)
                    idx = min(idx, max(0, len(items) - 1))
                    continue
                if action != "save" or edited is None:
                    continue
                try:
                    update_server(config_path, chosen.name, edited)
                except ValueError as exc:
                    stdscr.clear()
                    stdscr.addstr(0, 0, f"Could not save: {exc}")
                    stdscr.addstr(2, 0, "Press any key…")
                    stdscr.getch()
                    continue
                servers = load_servers(config_path)
                items = _menu_items(servers)
                for i, it in enumerate(items):
                    if isinstance(it, Server) and it.name == edited.name:
                        idx = i
                        break
                continue
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
