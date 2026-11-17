#!/usr/bin/env python3
"""Host actions submenu: runtime ops (git pull/log, rsync, chmod). Config lives in Edit."""

from __future__ import annotations

import curses
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any, List, Optional, Tuple

import ssh_gui as core

# Preset permission mode applied after upload (and available alone)
PERM_PRESET_DIRS = "755"
PERM_PRESET_FILES = "644"


def _suspend_curses(stdscr: "curses._CursesWindow") -> None:
    curses.def_prog_mode()
    curses.endwin()


def _resume_curses(stdscr: "curses._CursesWindow") -> None:
    curses.reset_prog_mode()
    stdscr.clear()
    stdscr.refresh()


_CANCEL_HINT = "Cancelled. Press Esc or ← to leave the actions menu."
_RUN_KEYS_NOTE = "keys:  Enter to run · Esc / ← in menu to cancel"


def _pause(msg: str = "Press Enter to continue…") -> bool:
    """Return False if cancelled (Ctrl+C). Never raises KeyboardInterrupt."""
    try:
        input(msg)
        return True
    except EOFError:
        return True
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return False


# ANSI helpers for suspended-curses stdout panels (TTY only).
_ANSI_RST = "\033[0m"
_ANSI_RED = "\033[1;31m"
_ANSI_GREEN = "\033[1;32m"
_ANSI_YELLOW = "\033[1;33m"
_ANSI_CYAN = "\033[1;36m"
_ANSI_CYAN_DIM = "\033[0;36m"
_ANSI_SUCCESS_TAG = "\033[1;30;42m"  # bold black on green

_GIT_FILE_STAT_RE = re.compile(r"\|\s*\d+\s+([+\-]+)\s*$")


def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{_ANSI_RST}"


def _colorize_git_pull_line(line: str) -> str:
    """Green add, red delete, yellow modify; everything else yellow."""
    s = line.rstrip("\n")
    stripped = s.lstrip()
    low = stripped.lower()

    if stripped.startswith("+++") or stripped.startswith("---"):
        return _c(_ANSI_YELLOW, s)
    if stripped.startswith("+"):
        return _c(_ANSI_GREEN, s)
    if stripped.startswith("-"):
        return _c(_ANSI_RED, s)

    if "create mode" in low or "new file" in low or "[new branch]" in low:
        return _c(_ANSI_GREEN, s)
    if "delete mode" in low or "deleted file" in low or "deleted branch" in low:
        return _c(_ANSI_RED, s)

    m = _GIT_FILE_STAT_RE.search(stripped)
    if m:
        marks = m.group(1)
        has_plus = "+" in marks
        has_minus = "-" in marks
        if has_plus and not has_minus:
            return _c(_ANSI_GREEN, s)
        if has_minus and not has_plus:
            return _c(_ANSI_RED, s)
        return _c(_ANSI_YELLOW, s)

    if "insertion" in low and "deletion" not in low:
        return _c(_ANSI_GREEN, s)
    if "deletion" in low and "insertion" not in low:
        return _c(_ANSI_RED, s)

    return _c(_ANSI_YELLOW, s)


def _print_git_pull_output(out: str) -> None:
    """Blank/separator lines around colorized git pull output."""
    sep = "-" * 56
    print()
    print(_c(_ANSI_CYAN, sep))
    print()
    text = (out or "").rstrip()
    if not text:
        print(_c(_ANSI_YELLOW, "(no output)"))
    else:
        for line in text.splitlines():
            print(_colorize_git_pull_line(line))
    print()
    print(_c(_ANSI_CYAN, sep))
    print()


def _print_info_panel(title: str, lines: List[str]) -> None:
    """Blank / asterisks / colored info / asterisks / blank — intentional confirm look."""
    bar = "*" * 56
    print()
    print(_c(_ANSI_CYAN, bar))
    print(_c(_ANSI_CYAN, f"  {title}"))
    print(_c(_ANSI_CYAN, bar))
    print()
    for line in lines:
        print(_c(_ANSI_CYAN_DIM, f"  {line}"))
    print()
    print(_c(_ANSI_CYAN, bar))
    print()


def _print_error_panel(title: str, details: List[str], output: str) -> None:
    """Clear RED failure block with pasteable command output."""
    bar = "*" * 56
    print()
    print(_c(_ANSI_RED, bar))
    print(_c(_ANSI_RED, f"  ERROR: {title}"))
    print(_c(_ANSI_RED, bar))
    print()
    for line in details:
        print(_c(_ANSI_RED, f"  {line}"))
    print()
    body = (output or "").rstrip() or "(no command output / stderr captured)"
    print(_c(_ANSI_RED, body))
    print()
    print(_c(_ANSI_RED, bar))
    print()


def _confirm_yn(prompt: str, *, default_yes: bool = False) -> bool:
    """Ask (y/N) or (Y/n). Empty input uses default. Ctrl+C → False (cancel)."""
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    try:
        ans = input(prompt + suffix).strip().lower()
    except EOFError:
        return default_yes
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return False
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def _ssh_target(server: core.Server) -> str:
    return f"{server.user}@{server.host}"


def _ssh_opts(server: core.Server) -> List[str]:
    """ssh options only (no user@host)."""
    opts = ["-p", str(server.port)]
    if server.identity:
        opts.extend(["-i", server.identity])
    elif core.uses_yaml_password(server):
        opts.extend(
            [
                "-o",
                "PreferredAuthentications=password,keyboard-interactive",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
        )
    opts.extend(["-o", "StrictHostKeyChecking=accept-new"])
    return opts


def run_argv(argv: List[str], server: core.Server, interactive: bool = True) -> int:
    """Run argv; auto-type YAML password when needed."""
    password = server.password if core.uses_yaml_password(server) else None
    if password:
        return core.ssh_with_auto_password(argv, password)
    return subprocess.call(argv)


def ssh_remote(server: core.Server, remote_cmd: str, interactive: bool = False) -> int:
    argv = ["ssh", *_ssh_opts(server), _ssh_target(server), remote_cmd]
    if interactive or core.uses_yaml_password(server):
        return run_argv(argv, server, interactive=True)
    return subprocess.call(argv)


def ssh_remote_capture(server: core.Server, remote_cmd: str, timeout: float = 60.0) -> Tuple[int, str]:
    """Run a short remote command and capture stdout+stderr."""
    argv = ["ssh", *_ssh_opts(server), _ssh_target(server), remote_cmd]
    if not core.uses_yaml_password(server):
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            return 124, "Timed out."

    return _ssh_capture_with_password(argv, server.password or "", timeout)


def _can_sudo(server: core.Server) -> bool:
    """sudo -S needs the YAML host password."""
    return bool(server.password)


def wrap_sudo(remote_cmd: str, password: str) -> str:
    """
    Run remote_cmd via `sudo -S` (password on stdin, quiet prompt).
    Uses bash -lc so pipelines/scripts work.
    """
    return (
        f"printf '%s\\n' {shlex.quote(password)} | "
        f"sudo -S -p '' bash -lc {shlex.quote(remote_cmd)}"
    )


def _looks_like_privilege_failure(code: int, out: str = "") -> bool:
    if code == 0:
        return False
    low = (out or "").lower()
    needles = (
        "permission denied",
        "operation not permitted",
        "not permitted",
        "access denied",
        "must be root",
        "password is required",
        "a password is required",
        "sudo:",
    )
    if any(n in low for n in needles):
        return True
    # Common non-zero exits when the user cannot read/traverse the path
    return code in (1, 126, 127)


def ssh_remote_capture_sudo_fallback(
    server: core.Server,
    remote_cmd: str,
    timeout: float = 60.0,
    *,
    always_retry: bool = False,
) -> Tuple[int, str, bool]:
    """
    Try remote_cmd without sudo; on failure retry with sudo -S when YAML
    password is present (privilege-like errors, or always_retry=True for detect).
    Returns (code, output, used_sudo).
    """
    code, out = ssh_remote_capture(server, remote_cmd, timeout=timeout)
    if code == 0:
        return code, out, False
    if not _can_sudo(server):
        return code, out, False
    if not always_retry and not _looks_like_privilege_failure(code, out):
        return code, out, False
    assert server.password is not None
    sudo_cmd = wrap_sudo(remote_cmd, server.password)
    code2, out2 = ssh_remote_capture(server, sudo_cmd, timeout=timeout)
    return code2, out2, True


def ssh_remote_sudo_fallback(
    server: core.Server,
    remote_cmd: str,
    interactive: bool = True,
) -> Tuple[int, bool]:
    """
    Run remote_cmd; if it fails and YAML password exists, retry with sudo -S.
    Returns (exit_code, used_sudo).
    """
    code = ssh_remote(server, remote_cmd, interactive=interactive)
    if code == 0:
        return code, False
    if not _can_sudo(server):
        return code, False
    assert server.password is not None
    print("  retrying with sudo -S …")
    sudo_cmd = wrap_sudo(remote_cmd, server.password)
    code2 = ssh_remote(server, sudo_cmd, interactive=interactive)
    return code2, True


def _exit_status_code(status: int) -> int:
    if hasattr(os, "waitstatus_to_exitcode"):
        return os.waitstatus_to_exitcode(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _drain_pty(master: int) -> bytes:
    import select

    out = b""
    while True:
        r, _, _ = select.select([master], [], [], 0)
        if not r:
            break
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return out


def _ssh_capture_with_password(argv: List[str], password: str, timeout: float) -> Tuple[int, str]:
    """
    Run argv on a PTY, auto-answer the SSH password prompt, capture output.
    Important: on PTY EOF/EIO after the remote command finishes, reap the real
    exit code — do not report timeout (124) just because the master hit EOF.
    """
    import errno
    import pty
    import select
    import signal
    import time

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        except OSError:
            os._exit(127)

    buf = b""
    collected = b""
    password_sent = False
    deadline = time.time() + timeout
    timed_out = False
    try:
        while True:
            now = time.time()
            if now >= deadline:
                timed_out = True
                break

            try:
                exited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                collected += _drain_pty(master)
                return 1, collected.decode("utf-8", errors="replace")
            if exited == pid:
                collected += _drain_pty(master)
                return _exit_status_code(status), collected.decode("utf-8", errors="replace")

            r, _, _ = select.select([master], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # Child closed the PTY — fall through to waitpid
                    break
                raise
            if not data:
                # EOF — remote command finished; reap below
                break

            collected += data
            buf += data
            if len(buf) > 8192:
                buf = buf[-4096:]
            if not password_sent and core._HOSTKEY_RE.search(buf):
                os.write(master, b"yes\n")
                buf = b""
            elif not password_sent and core._PASSWORD_RE.search(buf):
                os.write(
                    master,
                    password.encode("utf-8", errors="surrogateescape") + b"\n",
                )
                password_sent = True
                buf = b""

        if timed_out:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        # Reap after EOF/EIO/timeout (blocking wait with short grace on timeout)
        try:
            if timed_out:
                # Give process a moment to die after SIGTERM
                end = time.time() + 2
                status = None
                while time.time() < end:
                    exited, status = os.waitpid(pid, os.WNOHANG)
                    if exited == pid:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                    _, status = os.waitpid(pid, 0)
                    collected += _drain_pty(master)
                    return 124, collected.decode("utf-8", errors="replace")
                collected += _drain_pty(master)
                # Timed out waiting for prompt/command — still prefer real code if reaped
                return 124, collected.decode("utf-8", errors="replace")
            _, status = os.waitpid(pid, 0)
            collected += _drain_pty(master)
            return _exit_status_code(status), collected.decode("utf-8", errors="replace")
        except ChildProcessError:
            collected += _drain_pty(master)
            return (124 if timed_out else 1), collected.decode("utf-8", errors="replace")
    finally:
        try:
            os.close(master)
        except OSError:
            pass


def normalize_remote_path(path: str) -> str:
    """Strip whitespace and trailing slashes (keep root '/')."""
    p = (path or "").strip()
    if len(p) > 1:
        p = p.rstrip("/")
    return p


def remote_detect_flags(server: core.Server) -> Tuple[bool, bool]:
    """Return (is_git, has_vendor) for web_remote. Non-sudo first, then sudo -S."""
    if not server.web_remote:
        return False, False
    path = normalize_remote_path(server.web_remote)
    if not path:
        return False, False
    quoted = shlex.quote(path)
    # Prefer explicit checks; .git may be a dir or a gitfile (worktrees).
    cmd = (
        f"git=0; vendor=0; "
        f"if [ -e {quoted}/.git ] || git -C {quoted} rev-parse --is-inside-work-tree >/dev/null 2>&1; "
        f"then git=1; fi; "
        f"if [ -d {quoted}/vendor ]; then vendor=1; fi; "
        f"printf 'GIT=%s VENDOR=%s\\n' \"$git\" \"$vendor\""
    )
    code, out, _sudo = ssh_remote_capture_sudo_fallback(
        server, cmd, always_retry=True
    )
    if code != 0:
        return False, False
    return ("GIT=1" in out), ("VENDOR=1" in out)


def remote_is_git(server: core.Server) -> bool:
    return remote_detect_flags(server)[0]


REMI_PHP_ENABLE_CANDIDATES = tuple(
    f"/opt/remi/php{v}/enable" for v in ("81", "82", "83", "84")
)
_REMI_PHP_RE = re.compile(r"^php8[1-4]$")
_REMI_USING_RE = re.compile(r"Using Remi:\s*source\s+(\S+)")
_DRUSH_SUCCESS_RE = re.compile(r"\[success\]", re.IGNORECASE)


def remi_php_label(enable_path: str) -> str:
    """'/opt/remi/php83/enable' → 'php83'."""
    name = Path(enable_path).parent.name
    return name if _REMI_PHP_RE.match(name) else ""


def remi_enable_for(remi_php: str) -> str:
    return f"/opt/remi/{remi_php}/enable"


def remi_drush_cmd(
    path_q: str,
    drush_args: str,
    *,
    preferred: Optional[str] = None,
    quiet: bool = False,
) -> str:
    """
    Source Remi enable then run drush.
    preferred=php83 → that path only; else first existing php81→php84.
    """
    announce = "" if quiet else 'echo "Using Remi: source $enable" >&2'
    if preferred and _REMI_PHP_RE.match(preferred):
        enable_q = shlex.quote(remi_enable_for(preferred))
        return f"""
set -e
cd {path_q}
enable={enable_q}
if [ ! -f "$enable" ]; then
  echo "Cached Remi enable missing: $enable" >&2
  exit 127
fi
{announce}
# shellcheck disable=SC1090
source "$enable"
vendor/bin/drush {drush_args}
"""
    checks = " ".join(shlex.quote(p) for p in REMI_PHP_ENABLE_CANDIDATES)
    return f"""
set -e
cd {path_q}
enable=
for f in {checks}; do
  if [ -f "$f" ]; then
    enable=$f
    break
  fi
done
if [ -z "$enable" ]; then
  echo "No Remi PHP enable found (tried php81–php84 under /opt/remi/)." >&2
  exit 127
fi
{announce}
# shellcheck disable=SC1090
source "$enable"
vendor/bin/drush {drush_args}
"""


def _print_red_block(lines: List[str]) -> None:
    """Failed-attempt lines in red, then a blank line under the block."""
    for line in lines:
        text = line.rstrip("\n")
        if text.strip():
            print(_c(_ANSI_RED, text))
    print()


def _print_success_line(line: str) -> None:
    """[success] black on green; rest of line uses default terminal colors."""
    text = line.rstrip("\n")
    m = _DRUSH_SUCCESS_RE.search(text)
    if not m:
        print(text)
        return
    before = text[: m.start()]
    tag = text[m.start() : m.end()]
    after = text[m.end() :]
    if not sys.stdout.isatty():
        print(text)
        return
    print(f"{before}{_ANSI_SUCCESS_TAG}{tag}{_ANSI_RST}{after}")


def _print_drush_output_line(line: str, *, quiet: bool = False) -> None:
    text = line.rstrip("\n")
    if not text.strip():
        return
    if _REMI_USING_RE.search(text):
        if quiet:
            return
        print(_c(_ANSI_YELLOW, text))
        return
    if _DRUSH_SUCCESS_RE.search(text):
        _print_success_line(text)
        return
    if quiet:
        return
    print(text)


def _parse_remi_enable_used(out: str) -> str:
    for line in (out or "").splitlines():
        m = _REMI_USING_RE.search(line)
        if m:
            return m.group(1).strip()
        for cand in REMI_PHP_ENABLE_CANDIDATES:
            if cand in line and "Using Remi" in line:
                return cand
    return ""


def _success_lines_from(out: str, title: str) -> List[str]:
    found = [ln for ln in (out or "").splitlines() if _DRUSH_SUCCESS_RE.search(ln)]
    if found:
        return found
    return [f"[success] {title} finished."]


def _persist_remi_php(
    config_path: Optional[Path],
    server: core.Server,
    remi_php: str,
) -> core.Server:
    if not remi_php or not config_path:
        return server
    if server.remi_php == remi_php:
        return server
    updated = replace(server, remi_php=remi_php)
    try:
        core.update_server(config_path, server.name, updated)
    except Exception:  # noqa: BLE001
        return server
    return updated


def action_drush(
    server: core.Server,
    title: str,
    drush_args: str,
    config_path: Optional[Path] = None,
) -> core.Server:
    """Run vendor/bin/drush <args> in web_remote (sudo + Remi PHP retries)."""
    if not server.web_remote:
        return server
    path = normalize_remote_path(server.web_remote)
    path_q = shlex.quote(path)
    plain_cmd = f"cd {path_q} && vendor/bin/drush {drush_args}"
    cached = server.remi_php if (server.remi_php and _REMI_PHP_RE.match(server.remi_php)) else None
    quiet = bool(cached)

    note = (
        f"note:  Remi {cached} (cached) → sudo fallback"
        if cached
        else "note:  plain → sudo → Remi php81–84 enable (+ sudo)"
    )
    _print_info_panel(
        title,
        [
            f"host:  {server.label}",
            f"path:  {path}",
            f"cmd:   vendor/bin/drush {drush_args}",
            note,
            _RUN_KEYS_NOTE,
        ],
    )
    if not _pause("Press Enter to run…"):
        return server
    print()  # blank after first Press Enter

    used_sudo = False
    used_remi = False
    remi_enable_used = ""
    out = ""
    code = 1

    try:
        if cached:
            remi_cmd = remi_drush_cmd(
                path_q, drush_args, preferred=cached, quiet=True
            )
            code, out, used_sudo = ssh_remote_capture_sudo_fallback(
                server, remi_cmd, timeout=180.0, always_retry=True
            )
            used_remi = True
            remi_enable_used = remi_enable_for(cached)
            if code != 0:
                # Cached path failed — fall through to discovery (verbose).
                quiet = False
                _print_red_block(
                    [
                        f"Cached Remi {cached} failed (exit {code}).",
                        "Retrying: plain → Remi php81–84 probe…",
                    ]
                )
                code, out1, used_sudo1 = ssh_remote_capture_sudo_fallback(
                    server, plain_cmd, timeout=180.0, always_retry=True
                )
                used_sudo = used_sudo or used_sudo1
                if code != 0:
                    fail_lines = [ln for ln in (out1 or "").splitlines() if ln.strip()]
                    fail_lines.append(
                        "retrying with first existing Remi enable (php81–php84) …"
                    )
                    _print_red_block(fail_lines)
                    remi_cmd = remi_drush_cmd(path_q, drush_args, quiet=False)
                    code, out, used_sudo2 = ssh_remote_capture_sudo_fallback(
                        server, remi_cmd, timeout=180.0, always_retry=True
                    )
                    used_sudo = used_sudo or used_sudo2
                    used_remi = True
                    remi_enable_used = _parse_remi_enable_used(out) or remi_enable_used
                else:
                    out = out1
                    used_remi = False
                    remi_enable_used = ""
        else:
            code, out1, used_sudo = ssh_remote_capture_sudo_fallback(
                server, plain_cmd, timeout=180.0, always_retry=True
            )
            if code != 0:
                fail_lines = [ln for ln in (out1 or "").splitlines() if ln.strip()]
                fail_lines.append(
                    "retrying with first existing Remi enable (php81–php84) …"
                )
                _print_red_block(fail_lines)
                remi_cmd = remi_drush_cmd(path_q, drush_args, quiet=False)
                code, out, used_sudo2 = ssh_remote_capture_sudo_fallback(
                    server, remi_cmd, timeout=180.0, always_retry=True
                )
                used_sudo = used_sudo or used_sudo2
                used_remi = True
                remi_enable_used = _parse_remi_enable_used(out)
            else:
                out = out1
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return server

    if code == 0:
        if quiet and used_remi:
            for line in _success_lines_from(out, title):
                _print_success_line(line)
        else:
            for line in (out or "").splitlines():
                _print_drush_output_line(line, quiet=False)
            # Ensure a styled success line exists
            if not any(_DRUSH_SUCCESS_RE.search(ln) for ln in (out or "").splitlines()):
                _print_success_line(f"[success] {title} finished.")
        if used_remi and remi_enable_used:
            label = remi_php_label(remi_enable_used)
            if label:
                server = _persist_remi_php(config_path, server, label)
        print()  # blank before final Press Enter
        _pause()
        return server

    # Failure
    for line in (out or "").splitlines():
        _print_drush_output_line(line, quiet=False)
    last_cmd = (
        f"cd {path} && source {remi_enable_used or '<remi>'} && vendor/bin/drush {drush_args}"
        if used_remi
        else plain_cmd
    )
    _print_error_panel(
        f"{title} failed",
        [
            f"host: {server.label}",
            f"path: {path}",
            f"exit: {code}",
            f"cmd:  {last_cmd}",
            "Tried: plain → sudo → Remi php81–84 (+ sudo).",
            "Copy the output below for debugging.",
        ],
        out,
    )
    print()
    _pause("Press Enter to return…")
    return server


def pick_local_directory(
    stdscr: "curses._CursesWindow",
    start: Optional[str] = None,
) -> Optional[str]:
    """
    Terminal folder browser (used from Edit form for web_local).
    ↑↓ move · Enter open dir · s select current · ← up · Esc cancel
    """
    cwd = Path(start or Path.home()).expanduser().resolve()
    if not cwd.is_dir():
        cwd = Path.home()
    idx = 0
    curses.curs_set(0)

    while True:
        try:
            entries = sorted(
                [p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")],
                key=lambda p: p.name.lower(),
            )
        except PermissionError:
            entries = []

        rows: List[Tuple[str, Optional[Path]]] = [
            (f"[Select this folder]  {cwd}", None),
        ]
        if cwd.parent != cwd:
            rows.append(("[..]", cwd.parent))
        for p in entries:
            rows.append((p.name + "/", p))

        stdscr.clear()
        h, w = stdscr.getmaxyx()
        core._safe_add(stdscr, 0, 0, "Pick local project directory", curses.A_BOLD)
        core._safe_add(
            stdscr,
            1,
            0,
            "↑↓ move · Enter open · s select folder · ← up · Esc cancel",
            curses.color_pair(2),
        )
        core._safe_add(stdscr, 2, 0, str(cwd)[: max(0, w - 1)], curses.A_DIM)

        top = 4
        visible = max(1, h - top - 1)
        idx = max(0, min(idx, len(rows) - 1))
        start_i = max(0, min(idx - visible + 1, len(rows) - visible))
        for row_i, (label, _path) in enumerate(rows[start_i : start_i + visible]):
            y = top + row_i
            selected = (start_i + row_i) == idx
            attr = curses.color_pair(1) | curses.A_BOLD if selected else curses.A_NORMAL
            if selected:
                try:
                    stdscr.addnstr(y, 0, " " * max(0, w - 1), w - 1, curses.color_pair(1))
                except curses.error:
                    pass
            core._safe_add(stdscr, y, 2, label[: max(0, w - 3)], attr)

        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(rows)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(rows)
        elif key in (curses.KEY_LEFT,):
            if cwd.parent != cwd:
                cwd = cwd.parent
                idx = 0
        elif key in (27, ord("q")):
            return None
        elif key in (ord("s"), ord("S")):
            return str(cwd)
        elif key in (curses.KEY_ENTER, 10, 13):
            _label, path = rows[idx]
            if path is None:
                return str(cwd)
            cwd = path
            idx = 0


def chmod_preset_cmd(web_remote: str) -> str:
    path = shlex.quote(web_remote)
    return (
        f"find {path} -type d -exec chmod {PERM_PRESET_DIRS} {{}} + && "
        f"find {path} -type f -exec chmod {PERM_PRESET_FILES} {{}} +"
    )


def action_fix_permissions(server: core.Server) -> None:
    if not server.web_remote:
        return
    remote = normalize_remote_path(server.web_remote)
    cmd = chmod_preset_cmd(remote)
    _print_info_panel(
        "Fix permissions",
        [
            f"host:    {server.label}",
            f"remote:  {remote}",
            f"preset:  dirs {PERM_PRESET_DIRS} / files {PERM_PRESET_FILES}",
            "note:    non-sudo first; sudo -S fallback if password in YAML",
            _RUN_KEYS_NOTE,
        ],
    )
    if not _pause("Press Enter to run…"):
        return
    try:
        code, used_sudo = ssh_remote_sudo_fallback(server, cmd, interactive=True)
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
    if used_sudo:
        print("  used: sudo -S")
    print(f"Exit: {code}")
    _pause()


def action_rsync_upload(server: core.Server) -> None:
    local = server.web_local
    remote = server.web_remote
    if not local or not remote:
        return
    local_path = Path(local).expanduser()
    if not local_path.is_dir():
        print(f"Local path is not a directory: {local_path}")
        _pause()
        return

    src = str(local_path) + os.sep
    dest = f"{_ssh_target(server)}:{remote.rstrip('/')}/"
    ssh_e = "ssh " + " ".join(shlex.quote(o) for o in _ssh_opts(server))
    argv = [
        "rsync",
        "-avz",
        "--delete",
        "-e",
        ssh_e,
        src,
        dest,
    ]
    _print_info_panel(
        "Rsync upload + fix permissions",
        [
            f"host:    {server.label}",
            f"local:   {src}",
            f"remote:  {dest}",
            f"after:   chmod dirs {PERM_PRESET_DIRS} / files {PERM_PRESET_FILES}",
            "confirm: y to run · Enter/N cancels · Esc / ← in menu to leave",
        ],
    )
    if not _confirm_yn("Run upload rsync+chmod?", default_yes=False):
        return
    try:
        code = run_argv(argv, server, interactive=True)
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
    print(f"rsync exit: {code}")
    if code == 0:
        print("Applying permission preset…")
        try:
            chmod_code, used_sudo = ssh_remote_sudo_fallback(
                server, chmod_preset_cmd(normalize_remote_path(remote)), interactive=True
            )
        except KeyboardInterrupt:
            print(f"\n{_CANCEL_HINT}")
            return
        if used_sudo:
            print("  chmod used: sudo -S")
        print(f"chmod exit: {chmod_code}")
    _pause()


def action_git_pull(server: core.Server) -> None:
    """Uses git_user/git_token from Edit/yaml when set. No settings prompts here."""
    if not server.web_remote:
        return
    path_q = shlex.quote(normalize_remote_path(server.web_remote))

    if server.git_user and server.git_token:
        u = urllib.parse.quote(server.git_user, safe="")
        t = urllib.parse.quote(server.git_token, safe="")
        remote_script = f"""
set -e
cd {path_q}
ORIGIN=$(git remote get-url origin)
case "$ORIGIN" in
  https://*)
    REST=${{ORIGIN#https://}}
    REST=${{REST#*@}}
    AUTH_URL="https://{u}:{t}@$REST"
    git pull --ff-only "$AUTH_URL"
    ;;
  *)
    git pull --ff-only
    ;;
esac
"""
    else:
        remote_script = f"set -e; cd {path_q}; git pull --ff-only"

    info_lines = [
        f"host:  {server.label}",
        f"path:  {normalize_remote_path(server.web_remote)}",
        f"mode:  git pull --ff-only",
    ]
    if server.git_user:
        info_lines.append(f"auth:  {server.git_user} (token: set)")
    else:
        info_lines.append("auth:  none in Edit (OK for public/SSH remotes)")
    info_lines.append("note:  non-sudo first; sudo -S fallback if password in YAML")
    info_lines.append(_RUN_KEYS_NOTE)
    _print_info_panel("Git pull", info_lines)
    if not _pause("Press Enter to run…"):
        return
    try:
        code, out, used_sudo = ssh_remote_capture_sudo_fallback(
            server, remote_script, timeout=180.0, always_retry=True
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
    _print_git_pull_output(out)
    if used_sudo:
        print("  used: sudo -S")
    print(f"Exit: {code}")
    _pause()


def action_commit_history(server: core.Server, limit: int = 20) -> None:
    if not server.web_remote:
        return
    path_q = shlex.quote(normalize_remote_path(server.web_remote))
    fmt = shlex.quote("%h %ad %an %s")
    cmd = f"git -C {path_q} log -n {int(limit)} --date=short --pretty=format:{fmt}"
    _print_info_panel(
        "Git history",
        [
            f"host:  {server.label}",
            f"path:  {normalize_remote_path(server.web_remote)}",
            f"cmd:   git log -n {limit} --pretty=…",
            "note:  non-sudo first; sudo -S fallback if password in YAML",
            _RUN_KEYS_NOTE,
        ],
    )
    if not _pause("Press Enter to run…"):
        return
    try:
        code, used_sudo = ssh_remote_sudo_fallback(server, cmd, interactive=True)
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
    if used_sudo:
        print("  used: sudo -S")
    print(f"\nExit: {code}")
    _pause()


def action_rsync_download(server: core.Server) -> None:
    """Download remote web_remote → local web_local (simple rsync output, like upload)."""
    local = server.web_local
    remote = server.web_remote
    if not local or not remote:
        return
    local_path = Path(local).expanduser()
    try:
        local_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Cannot create local dir: {exc}")
        _pause()
        return

    remote = normalize_remote_path(remote)
    src = f"{_ssh_target(server)}:{remote}/"
    dest = str(local_path) + os.sep
    ssh_e = "ssh " + " ".join(shlex.quote(o) for o in _ssh_opts(server))
    argv = [
        "rsync",
        "-avz",
        "--delete",
        "--progress",
        "-e",
        ssh_e,
        src,
        dest,
    ]
    _print_info_panel(
        "Rsync download",
        [
            f"host:    {server.label}",
            f"remote:  {src}",
            f"local:   {dest}",
            "confirm: y to run · Enter/N cancels · Esc / ← in menu to leave",
        ],
    )
    if not _confirm_yn("Run download rsync?", default_yes=False):
        return
    try:
        code = run_argv(argv, server, interactive=True)
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
    print(f"rsync exit: {code}")
    _pause()



def host_actions_menu(
    stdscr: "curses._CursesWindow",
    config_path: Path,
    server: core.Server,
) -> core.Server:
    """
    Runtime actions only. Paths/creds are configured via Edit (e).
    ← / q returns to main list.
    """
    focus = 0  # index into selectable action list
    status = ""
    is_git: Optional[bool] = None
    has_vendor: Optional[bool] = None
    detected = False

    while True:
        for s in core.load_servers(config_path):
            if s.name == server.name:
                server = s
                break

        has_remote = bool(server.web_remote)
        has_local = bool(server.web_local)
        can_upload = has_remote and has_local
        can_chmod = has_remote

        if has_remote and not detected:
            status = "Detecting remote git / vendor…"
            stdscr.clear()
            core._safe_add(stdscr, 0, 2, server.name, curses.color_pair(4) | curses.A_BOLD)
            core._safe_add(stdscr, 2, 2, status, curses.color_pair(2))
            stdscr.refresh()
            try:
                is_git, has_vendor = remote_detect_flags(server)
                bits = []
                bits.append("git" if is_git else "no git")
                bits.append("vendor" if has_vendor else "no vendor")
                status = "Remote: " + ", ".join(bits) + "."
            except Exception as exc:  # noqa: BLE001
                is_git = False
                has_vendor = False
                status = f"Detect failed: {exc}"
            detected = True
        elif not has_remote:
            is_git = False
            has_vendor = False
            status = "Set web_remote (and web_local) in Edit (e) to enable actions."

        # Selectable actions only (section labels are visual)
        # (id, label, enabled)
        git_actions: List[Tuple[str, str, bool]] = []
        if is_git:
            git_actions.append(("git_pull", "Pull", True))
            git_actions.append(("git_log", "History", True))
        elif has_remote:
            git_actions.append(("git_pull", "Pull  (not a git repo)", False))
            git_actions.append(("git_log", "History  (not a git repo)", False))
        else:
            git_actions.append(("git_pull", "Pull  (set web_remote in Edit)", False))
            git_actions.append(("git_log", "History  (set web_remote in Edit)", False))

        deploy_actions: List[Tuple[str, str, bool]] = []
        if can_upload:
            deploy_actions.append(("upload", "Rsync upload + fix permissions", True))
            deploy_actions.append(("download", "Rsync download", True))
        else:
            deploy_actions.append(
                ("upload", "Rsync upload + fix permissions  (set web_local + web_remote in Edit)", False)
            )
            deploy_actions.append(
                ("download", "Rsync download  (set web_local + web_remote in Edit)", False)
            )
        if can_chmod:
            deploy_actions.append(("chmod", "Fix permissions only (dirs 755 / files 644)", True))
        else:
            deploy_actions.append(("chmod", "Fix permissions only  (set web_remote in Edit)", False))

        # Vendor group only when remote web_remote/vendor exists
        vendor_actions: List[Tuple[str, str, bool]] = []
        if has_vendor:
            vendor_actions = [
                ("drush_cr", "Rebuild cache", True),
                ("drush_updb", "Update Database", True),
                ("drush_uli", "Admin login", True),
                ("drush_cim", "Config: Import", True),
                ("drush_cex", "Config: Export", True),
            ]

        selectable: List[Tuple[str, str, bool]] = [
            *git_actions,
            *deploy_actions,
            *vendor_actions,
            ("back", "← Back to host list", True),
        ]

        # Display rows: ('section', title) | ('item', selectable_index)
        git_n = len(git_actions)
        deploy_n = len(deploy_actions)
        vendor_n = len(vendor_actions)
        display: List[Tuple[str, Any]] = [
            ("section", "Git"),
            *[("item", i) for i in range(git_n)],
            ("section", "Deploy"),
            *[("item", git_n + i) for i in range(deploy_n)],
        ]
        if vendor_n:
            display.append(("section", "Vendor"))
            display.extend(("item", git_n + deploy_n + i) for i in range(vendor_n))
        display.append(("blank", None))
        display.append(("item", len(selectable) - 1))  # back

        stdscr.clear()
        h, w = stdscr.getmaxyx()
        # Header: host name prominent
        core._safe_add(stdscr, 0, 2, server.name, curses.color_pair(4) | curses.A_BOLD)
        core._safe_add(stdscr, 1, 2, f"{server.user}@{server.host}:{server.port}", curses.A_DIM)
        core._safe_add(
            stdscr,
            2,
            2,
            "Enter run · ← / q back · paths in Edit (e)",
            curses.color_pair(2),
        )
        if status:
            core._safe_add(stdscr, 3, 2, status[: max(0, w - 3)], curses.A_DIM)

        summary = f"remote {server.web_remote or '—'}   local {server.web_local or '—'}"
        core._safe_add(stdscr, 4, 2, summary[: max(0, w - 3)], curses.A_DIM)

        top = 6
        focus = max(0, min(focus, len(selectable) - 1))
        for row_i, (kind, payload) in enumerate(display):
            y = top + row_i
            if y >= h - 1:
                break
            if kind == "blank":
                continue
            if kind == "section":
                # Gray group label
                core._safe_add(
                    stdscr,
                    y,
                    2,
                    str(payload),
                    curses.color_pair(8) | curses.A_BOLD,
                )
                continue

            item_i = int(payload)
            action_id, label, enabled = selectable[item_i]
            selected = item_i == focus
            if selected and enabled:
                try:
                    stdscr.addnstr(y, 0, " " * max(0, w - 1), w - 1, curses.color_pair(1))
                except curses.error:
                    pass
                attr = curses.color_pair(1) | curses.A_BOLD
            elif selected and not enabled:
                try:
                    stdscr.addnstr(y, 0, " " * max(0, w - 1), w - 1, curses.A_DIM)
                except curses.error:
                    pass
                attr = curses.A_DIM | curses.A_BOLD
            elif not enabled:
                attr = curses.A_DIM
            else:
                attr = curses.color_pair(4) if action_id != "back" else curses.A_NORMAL
            prefix = "    " if enabled else "    · "
            core._safe_add(stdscr, y, 0, (prefix + label)[: max(0, w - 1)], attr)

        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            focus = (focus - 1) % len(selectable)
        elif key in (curses.KEY_DOWN, ord("j")):
            focus = (focus + 1) % len(selectable)
        elif key in (curses.KEY_LEFT, 27, ord("q")):
            return server
        elif key in (curses.KEY_ENTER, 10, 13):
            action, _label, enabled = selectable[focus]
            if action == "back":
                return server
            if not enabled:
                status = "Disabled — set paths in Edit (e), then reopen →."
                continue
            if action == "git_pull":
                _suspend_curses(stdscr)
                try:
                    action_git_pull(server)
                finally:
                    _resume_curses(stdscr)
                status = "Git pull finished."
            elif action == "git_log":
                _suspend_curses(stdscr)
                try:
                    action_commit_history(server)
                finally:
                    _resume_curses(stdscr)
                status = "Commit history finished."
            elif action == "upload":
                _suspend_curses(stdscr)
                try:
                    action_rsync_upload(server)
                finally:
                    _resume_curses(stdscr)
                status = "Upload finished."
            elif action == "download":
                _suspend_curses(stdscr)
                try:
                    action_rsync_download(server)
                finally:
                    _resume_curses(stdscr)
                status = "Download finished."
            elif action == "chmod":
                _suspend_curses(stdscr)
                try:
                    action_fix_permissions(server)
                finally:
                    _resume_curses(stdscr)
                status = "Permissions fix finished."
            elif action == "drush_cr":
                _suspend_curses(stdscr)
                try:
                    server = action_drush(
                        server, "Rebuild cache", "cr", config_path=config_path
                    )
                finally:
                    _resume_curses(stdscr)
                status = "Rebuild cache finished."
            elif action == "drush_updb":
                _suspend_curses(stdscr)
                try:
                    server = action_drush(
                        server,
                        "Update Database",
                        "updatedb -y",
                        config_path=config_path,
                    )
                finally:
                    _resume_curses(stdscr)
                status = "Update Database finished."
            elif action == "drush_uli":
                _suspend_curses(stdscr)
                try:
                    server = action_drush(
                        server, "Admin login", "uli", config_path=config_path
                    )
                finally:
                    _resume_curses(stdscr)
                status = "Admin login finished."
            elif action == "drush_cim":
                _suspend_curses(stdscr)
                try:
                    server = action_drush(
                        server, "Config: Import", "cim", config_path=config_path
                    )
                finally:
                    _resume_curses(stdscr)
                status = "Config import finished."
            elif action == "drush_cex":
                _suspend_curses(stdscr)
                try:
                    server = action_drush(
                        server, "Config: Export", "cex", config_path=config_path
                    )
                finally:
                    _resume_curses(stdscr)
                status = "Config export finished."
