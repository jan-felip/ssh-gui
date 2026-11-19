#!/usr/bin/env python3
"""Host actions submenu: runtime ops (git pull/log, rsync, chmod). Config lives in Edit."""

from __future__ import annotations

import curses
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
import termios
import tty
import urllib.parse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, TextIO, Tuple

import ssh_gui as core

# Preset permission mode applied after upload (and available alone)
PERM_PRESET_DIRS = "755"
PERM_PRESET_FILES = "644"

# Local dump folder under web_local (keep *.sql* out of git — see .gitignore)
DB_DUMPS_SUBDIR = "db-dumps"
_DUMP_NAME_RE = re.compile(r"^[\w.-]+\.sql(\.gz)?$", re.IGNORECASE)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _suspend_curses(stdscr: "curses._CursesWindow") -> None:
    curses.def_prog_mode()
    curses.endwin()


def _resume_curses(stdscr: "curses._CursesWindow") -> None:
    """Restore TUI after endwin; always re-enable blocking keypad input."""
    curses.reset_prog_mode()
    core._curses_blocking_input(stdscr)
    stdscr.clear()
    stdscr.refresh()


def _getch_blocking(stdscr: "curses._CursesWindow") -> int:
    """Blocking getch shared by actions/pickers; ignores timeout/-1/no-input."""
    return core._curses_getch(stdscr)


_CANCEL_HINT = "Cancelled. Press Esc or ← to leave the actions menu."
_RUN_KEYS_NOTE = "keys:  Enter to run · Esc / ← in menu to cancel"


def _pause(msg: str = "Press Enter to continue…") -> bool:
    """Wait for Enter; swallow arrow escapes (no ^[[D dump). Ctrl+C cancels."""
    try:
        sys.stdout.write(msg)
        sys.stdout.flush()
        if not sys.stdin.isatty():
            sys.stdin.readline()
            return True
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("\n", "\r"):
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return True
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch == "\x1b":
                    # Drain CSI / SS3 (arrows) without echoing
                    nxt = sys.stdin.read(1)
                    if nxt == "[":
                        while True:
                            c = sys.stdin.read(1)
                            if not c or c.isalpha() or c == "~":
                                break
                    elif nxt == "O":
                        sys.stdin.read(1)
                # ignore other keys (no echo)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
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
    """
    Diff colors: + / +++ always green; - / --- always red (never yellow).
    New-file paths/text green; modified paths/text yellow.
    """
    s = line.rstrip("\n")
    stripped = s.lstrip()
    low = stripped.lower()

    # +++ / --- headers and hunk +/- lines — symbol color wins over “modified”
    if stripped.startswith("+++"):
        return _c(_ANSI_GREEN, s)
    if stripped.startswith("---"):
        return _c(_ANSI_RED, s)
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
        # Pure adds → new file (green path); pure deletes → red; both → modified yellow
        if has_plus and not has_minus:
            return _c(_ANSI_GREEN, s)
        if has_minus and not has_plus:
            return _c(_ANSI_RED, s)
        return _c(_ANSI_YELLOW, s)

    if "insertion" in low and "deletion" not in low:
        return _c(_ANSI_GREEN, s)
    if "deletion" in low and "insertion" not in low:
        return _c(_ANSI_RED, s)

    # Modified / other status text (paths, "Updating …", etc.)
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


_remote_sudo_cache: Dict[str, bool] = {}


def _server_cache_key(server: core.Server) -> str:
    return f"{server.user}@{server.host}:{server.port}"


def remote_has_sudo(server: core.Server) -> bool:
    """True when remote `sudo` binary exists (cached per host)."""
    key = _server_cache_key(server)
    if key in _remote_sudo_cache:
        return _remote_sudo_cache[key]
    code, _out = ssh_remote_capture(
        server, "command -v sudo >/dev/null 2>&1", timeout=20.0
    )
    _remote_sudo_cache[key] = code == 0
    return _remote_sudo_cache[key]


def _mark_sudo_missing(server: core.Server) -> None:
    _remote_sudo_cache[_server_cache_key(server)] = False


def _sudo_missing_output(out: str) -> bool:
    low = (out or "").lower()
    return (
        "sudo: not found" in low
        or "sudo: command not found" in low
        or ("command not found" in low and "sudo" in low)
    )


def _can_sudo(server: core.Server) -> bool:
    """sudo -S needs YAML password and a remote sudo binary."""
    return bool(server.password) and remote_has_sudo(server)


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
    if _sudo_missing_output(out):
        return False
    # Missing binaries (php/drush) are not privilege errors
    if "command not found" in low or "no such file" in low:
        return False
    needles = (
        "permission denied",
        "operation not permitted",
        "not permitted",
        "access denied",
        "must be root",
        "password is required",
        "a password is required",
        "sudo: ",
    )
    return any(n in low for n in needles)


def ssh_remote_capture_sudo_fallback(
    server: core.Server,
    remote_cmd: str,
    timeout: float = 60.0,
    *,
    always_retry: bool = False,
) -> Tuple[int, str, bool]:
    """
    Try remote_cmd without sudo; on failure retry with sudo -S when available
    (privilege-like errors, or always_retry=True for detect).
    Returns (code, output, used_sudo). Skips sudo when binary is missing.
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
    if _sudo_missing_output(out2) or (
        code2 == 127 and "sudo" in (out2 or "").lower()
    ):
        _mark_sudo_missing(server)
        # Keep the original non-sudo failure as the useful result
        return code, out, False
    return code2, out2, True


def ssh_remote_sudo_fallback(
    server: core.Server,
    remote_cmd: str,
    interactive: bool = True,
) -> Tuple[int, bool]:
    """
    Run remote_cmd; if it fails and sudo is available, retry with sudo -S.
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


def remi_shell_cmd(
    path_q: str,
    shell_after_cd: str,
    *,
    preferred: Optional[str] = None,
    quiet: bool = False,
) -> str:
    """
    Activate Remi PHP as the SSH user (no sudo), then run shell_after_cd.
    Tries: source enable → scl enable → module → direct
    /opt/remi/php8x/root/usr/bin/php on PATH.
    preferred=php83 → that version only; else first existing php81→php84.
    """
    checks = " ".join(shlex.quote(p) for p in REMI_PHP_ENABLE_CANDIDATES)
    preferred_q = shlex.quote(preferred) if preferred and _REMI_PHP_RE.match(preferred) else "''"
    quiet_flag = "1" if quiet else "0"
    # shell_after_cd is embedded as a quoted script for scl/module subshells
    body_q = shlex.quote(shell_after_cd)
    return f"""
set -e
cd {path_q}
QUIET={quiet_flag}
PREFERRED={preferred_q}
announce() {{
  if [ "$QUIET" = "0" ]; then
    echo "Using Remi: $*" >&2
  fi
}}
run_body() {{
  eval {body_q}
}}
pick_label() {{
  if [ -n "$PREFERRED" ] && [ -d "/opt/remi/$PREFERRED" ]; then
    echo "$PREFERRED"
    return 0
  fi
  for f in {checks}; do
    if [ -f "$f" ] || [ -x "$(dirname "$f")/root/usr/bin/php" ]; then
      basename "$(dirname "$f")"
      return 0
    fi
  done
  return 1
}}
label=$(pick_label) || {{
  echo "No Remi PHP found (tried php81–php84 under /opt/remi/)." >&2
  exit 127
}}
enable="/opt/remi/$label/enable"
php_bin="/opt/remi/$label/root/usr/bin/php"
# 1) source enable script (user-level)
if [ -f "$enable" ]; then
  announce "source $enable"
  set +e
  # shellcheck disable=SC1090
  source "$enable"
  src_rc=$?
  set -e
  if [ "$src_rc" -eq 0 ]; then
    run_body
    exit $?
  fi
  echo "Remi source failed (exit $src_rc); trying other methods…" >&2
fi
# 2) scl enable
if command -v scl >/dev/null 2>&1; then
  announce "scl enable $label"
  if scl enable "$label" -- bash -lc {body_q}; then
    exit 0
  fi
fi
# 3) environment module
if command -v module >/dev/null 2>&1; then
  announce "module load $label"
  set +e
  module load "$label" >/dev/null 2>&1
  mod_rc=$?
  set -e
  if [ "$mod_rc" -eq 0 ]; then
    run_body
    exit $?
  fi
fi
# 4) direct Remi php binary on PATH (vendor/bin/drush shebang uses env php)
if [ -x "$php_bin" ]; then
  announce "direct $php_bin"
  export PATH="$(dirname "$php_bin"):$PATH"
  run_body
  exit $?
fi
echo "Remi $label found but could not activate (source/scl/module/direct php)." >&2
exit 127
"""


def remi_drush_cmd(
    path_q: str,
    drush_args: str,
    *,
    preferred: Optional[str] = None,
    quiet: bool = False,
) -> str:
    """Source Remi enable then run vendor/bin/drush <args>."""
    return remi_shell_cmd(
        path_q,
        f"vendor/bin/drush {drush_args}",
        preferred=preferred,
        quiet=quiet,
    )


def _safe_dump_stem(server_name: str) -> str:
    return _SAFE_NAME_RE.sub("_", server_name).strip("._") or "host"


def _dump_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _local_dumps_dir(server: core.Server) -> Optional[Path]:
    if not server.web_local:
        return None
    return Path(server.web_local).expanduser().resolve() / DB_DUMPS_SUBDIR


def _is_dump_filename(name: str) -> bool:
    return bool(_DUMP_NAME_RE.match(name))


_RSYNC_PROGRESS_RE = re.compile(
    r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+([\d:]+)(?:\s+\((.+)\))?\s*$"
)
_RSYNC_PROGRESS2_RE = re.compile(
    r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+([\d:]+)\s*$"
)
_RSYNC_TO_CHECK_RE = re.compile(r"to-check=(\d+)/(\d+)")

_rsync_progress2_cached: Optional[bool] = None


def _rsync_supports_progress2() -> bool:
    global _rsync_progress2_cached
    if _rsync_progress2_cached is not None:
        return _rsync_progress2_cached
    try:
        proc = subprocess.run(
            ["rsync", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        _rsync_progress2_cached = "progress2" in text
    except (OSError, subprocess.TimeoutExpired):
        _rsync_progress2_cached = False
    return _rsync_progress2_cached


def _progress_bar(pct: int, width: int = 26) -> str:
    pct = max(0, min(100, int(pct)))
    filled = int(round(width * pct / 100))
    return "█" * filled + "░" * (width - filled)


def _rsync_progress_flags() -> List[str]:
    """Prefer overall progress2 on GNU rsync; classic --progress on openrsync."""
    if _rsync_supports_progress2():
        return ["--info=progress2", "--human-readable"]
    return ["--progress", "-h"]


def _colorize_rsync_name_line(line: str) -> str:
    s = line.rstrip("\n")
    stripped = s.lstrip()
    low = stripped.lower()
    if not stripped:
        return s
    if stripped.startswith("sending ") or stripped.startswith("receiving "):
        return _c(_ANSI_CYAN_DIM, s)
    if stripped.startswith("sent ") or stripped.startswith("total size"):
        return _c(_ANSI_CYAN, s)
    if "speedup" in low:
        return _c(_ANSI_CYAN, s)
    if stripped.startswith("deleting ") or low.startswith("*deleting"):
        return _c(_ANSI_RED, s)
    if stripped.startswith(">f+++++++++") or stripped.startswith("<f+++++++++"):
        return _c(_ANSI_GREEN, s)
    if stripped.startswith(">f") or stripped.startswith("<f") or stripped.startswith("c"):
        return _c(_ANSI_YELLOW, s)
    if stripped.startswith("./") or stripped.endswith("/"):
        return _c(_ANSI_CYAN_DIM, s)
    return _c(_ANSI_GREEN, s)


def _format_rsync_progress_line(
    *,
    name: str,
    pct: int,
    rate: str,
    eta: str,
    extra: str = "",
) -> str:
    bar = _progress_bar(pct)
    name_w = 34
    shown = name if len(name) <= name_w else ("…" + name[-(name_w - 1) :])
    pct_s = f"{pct:3d}%"
    rate_s = f"{rate:>9}"
    eta_s = f"{eta:>8}"
    tail = f"  {extra}" if extra else ""
    body = f"  {shown:<{name_w}}  [{bar}]  {pct_s}  {rate_s}  {eta_s}{tail}"
    return _c(_ANSI_CYAN, body)


def _iter_rsync_output_chunks(stream: TextIO) -> Iterator[str]:
    """Yield text chunks split on CR/LF (rsync progress rewrites with \\r)."""
    buf = ""
    while True:
        block = stream.read(1024)
        if not block:
            if buf:
                yield buf
            break
        buf += block
        while True:
            m = re.search(r"[\r\n]", buf)
            if not m:
                break
            part, buf = buf[: m.start()], buf[m.end() :]
            if part:
                yield part


def _rsync_is_meta_line(line: str) -> bool:
    low = line.lstrip().lower()
    return (
        low.startswith("sending ")
        or low.startswith("receiving ")
        or low.startswith("sent ")
        or low.startswith("total ")
        or "speedup" in low
        or low.startswith("building file list")
        or low.startswith("created directory")
    )


def run_rsync_pretty(argv: List[str], server: core.Server) -> int:
    """
    Run rsync with colored file lines + aligned percent progress bar.
    Password hosts keep PTY auto-auth (raw progress); key hosts get live polish.
    """
    if core.uses_yaml_password(server):
        print(_c(_ANSI_CYAN_DIM, "  progress: live (PTY) — password host"))
        return run_argv(argv, server, interactive=True)

    print()
    print(_c(_ANSI_CYAN, "-" * 56))
    print(
        _c(
            _ANSI_CYAN_DIM,
            "  path                               progress                   %     rate      eta",
        )
    )
    print(_c(_ANSI_CYAN, "-" * 56))

    current_name = ""
    on_progress_row = False
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=0,
        )
    except OSError as exc:
        print(_c(_ANSI_RED, f"  failed to start rsync: {exc}"))
        return 127

    assert proc.stdout is not None
    try:
        for chunk in _iter_rsync_output_chunks(proc.stdout):
            line = chunk.rstrip()
            if not line.strip():
                continue
            m = _RSYNC_PROGRESS_RE.match(line) or _RSYNC_PROGRESS2_RE.match(line)
            if m:
                pct = int(m.group(2))
                rate = m.group(3)
                eta = m.group(4)
                extra = ""
                if m.lastindex and m.lastindex >= 5 and m.group(5):
                    extra = m.group(5)
                    tc = _RSYNC_TO_CHECK_RE.search(extra)
                    if tc:
                        done = int(tc.group(2)) - int(tc.group(1))
                        total = int(tc.group(2))
                        extra = f"{done}/{total}"
                label = current_name or "transfer"
                rendered = _format_rsync_progress_line(
                    name=label, pct=pct, rate=rate, eta=eta, extra=extra
                )
                sys.stdout.write("\r\033[K" + rendered)
                sys.stdout.flush()
                on_progress_row = True
                if pct >= 100:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    on_progress_row = False
                    current_name = ""
                continue

            if on_progress_row:
                sys.stdout.write("\n")
                sys.stdout.flush()
                on_progress_row = False
            stripped = line.strip()
            if not _rsync_is_meta_line(stripped):
                current_name = stripped
            print(_colorize_rsync_name_line(line))
        code = proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except OSError:
                pass
        raise

    if on_progress_row:
        sys.stdout.write("\n")
        sys.stdout.flush()
    print(_c(_ANSI_CYAN, "-" * 56))
    if code == 0:
        _print_success_line("[success] rsync finished")
    else:
        print(_c(_ANSI_RED, f"  rsync exit: {code}"))
    print()
    return code


def _rsync_file(
    server: core.Server,
    *,
    src: str,
    dest: str,
) -> int:
    """rsync a single file (src/dest may be local paths or user@host:path)."""
    ssh_e = "ssh " + " ".join(shlex.quote(o) for o in _ssh_opts(server))
    argv = ["rsync", "-avz", *_rsync_progress_flags(), "-e", ssh_e, src, dest]
    return run_rsync_pretty(argv, server)


def _exec_vendor_shell(
    server: core.Server,
    path: str,
    shell_after_cd: str,
    *,
    config_path: Optional[Path] = None,
    timeout: float = 600.0,
) -> Tuple[core.Server, int, str, bool]:
    """
    Run shell_after_cd in web_remote.

    Chain (sudo skipped when missing):
      cached Remi (user) → plain → Remi probe (user: source/scl/module/direct php)
      → same steps with sudo -S only if sudo exists + YAML password.

    Returns (server, code, output, used_remi).
    """
    path_q = shlex.quote(path)
    plain_cmd = f"cd {path_q} && {shell_after_cd}"
    cached = server.remi_php if (server.remi_php and _REMI_PHP_RE.match(server.remi_php)) else None
    used_remi = False
    remi_enable_used = ""
    out = ""
    code = 1

    def _run(cmd: str, *, try_sudo: bool) -> Tuple[int, str]:
        if try_sudo:
            c, o, _s = ssh_remote_capture_sudo_fallback(
                server, cmd, timeout=timeout, always_retry=True
            )
            return c, o
        return ssh_remote_capture(server, cmd, timeout=timeout)

    def _try_remi(*, preferred: Optional[str], quiet: bool, try_sudo: bool) -> Tuple[int, str]:
        cmd = remi_shell_cmd(
            path_q, shell_after_cd, preferred=preferred, quiet=quiet
        )
        return _run(cmd, try_sudo=try_sudo)

    # Pass 1: user-level only (no sudo)
    if cached:
        code, out = _try_remi(preferred=cached, quiet=True, try_sudo=False)
        used_remi = True
        remi_enable_used = remi_enable_for(cached)
        if code != 0:
            _print_red_block(
                [
                    f"Cached Remi {cached} failed (exit {code}).",
                    "Retrying: plain → Remi php81–84 (user, no sudo)…",
                ]
            )
            code, out1 = _run(plain_cmd, try_sudo=False)
            if code == 0:
                out = out1
                used_remi = False
                remi_enable_used = ""
            else:
                fail_lines = [ln for ln in (out1 or "").splitlines() if ln.strip()]
                fail_lines.append(
                    "retrying Remi php81–84 (source / scl / module / direct php)…"
                )
                _print_red_block(fail_lines)
                code, out = _try_remi(preferred=None, quiet=False, try_sudo=False)
                used_remi = True
                remi_enable_used = _parse_remi_enable_used(out) or remi_enable_used
    else:
        code, out1 = _run(plain_cmd, try_sudo=False)
        if code == 0:
            out = out1
        else:
            fail_lines = [ln for ln in (out1 or "").splitlines() if ln.strip()]
            fail_lines.append(
                "retrying Remi php81–84 (source / scl / module / direct php)…"
            )
            _print_red_block(fail_lines)
            code, out = _try_remi(preferred=None, quiet=False, try_sudo=False)
            used_remi = True
            remi_enable_used = _parse_remi_enable_used(out)

    # Pass 2: sudo only when still failing and sudo is actually available
    if code != 0 and _can_sudo(server):
        _print_red_block(
            ["User-level attempts failed; retrying with sudo -S …"]
        )
        if cached:
            code, out = _try_remi(preferred=cached, quiet=True, try_sudo=True)
            used_remi = True
            remi_enable_used = remi_enable_for(cached)
        if code != 0:
            code, out1 = _run(plain_cmd, try_sudo=True)
            if code == 0:
                out = out1
                used_remi = False
                remi_enable_used = ""
            else:
                code, out = _try_remi(preferred=None, quiet=False, try_sudo=True)
                used_remi = True
                remi_enable_used = _parse_remi_enable_used(out) or remi_enable_used

    if code == 0 and used_remi and remi_enable_used:
        label = remi_php_label(remi_enable_used)
        if label:
            server = _persist_remi_php(config_path, server, label)
    return server, code, out, used_remi


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
    """Return enable path or empty; also accepts scl/direct announce lines."""
    for line in (out or "").splitlines():
        m = _REMI_USING_RE.search(line)
        if m:
            token = m.group(1).strip()
            if token.startswith("/opt/remi/") and token.endswith("/enable"):
                return token
            # "direct /opt/remi/php83/root/usr/bin/php"
            m_php = re.search(r"/opt/remi/(php8[1-4])/", token)
            if m_php:
                return remi_enable_for(m_php.group(1))
        # "Using Remi: scl enable php83" / "module load php83" / "source /opt/..."
        m_lab = re.search(
            r"Using Remi:.*\b(php8[1-4])\b", line, re.IGNORECASE
        )
        if m_lab:
            return remi_enable_for(m_lab.group(1).lower())
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
    """Run vendor/bin/drush <args> in web_remote (Remi user-level, sudo only if present)."""
    if not server.web_remote:
        return server
    path = normalize_remote_path(server.web_remote)
    cached = server.remi_php if (server.remi_php and _REMI_PHP_RE.match(server.remi_php)) else None
    quiet = bool(cached)
    has_sudo = _can_sudo(server)

    if cached:
        note = f"note:  Remi {cached} (cached) → plain → Remi probe"
    else:
        note = "note:  plain → Remi php81–84 (source/scl/module/direct php)"
    if has_sudo:
        note += " → sudo -S last"
    else:
        note += " · no sudo on host (skipped)"

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

    try:
        server, code, out, used_remi = _exec_vendor_shell(
            server,
            path,
            f"vendor/bin/drush {drush_args}",
            config_path=config_path,
            timeout=180.0,
        )
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
            if not any(_DRUSH_SUCCESS_RE.search(ln) for ln in (out or "").splitlines()):
                _print_success_line(f"[success] {title} finished.")
        print()  # blank before final Press Enter
        _pause()
        return server

    for line in (out or "").splitlines():
        _print_drush_output_line(line, quiet=False)
    _print_error_panel(
        f"{title} failed",
        [
            f"host: {server.label}",
            f"path: {path}",
            f"exit: {code}",
            f"cmd:  vendor/bin/drush {drush_args}",
            "Tried: plain → Remi (source/scl/module/direct php)"
            + (" → sudo -S" if has_sudo else " (sudo skipped — not installed)"),
            "Copy the output below for debugging.",
        ],
        out,
    )
    print()
    _pause("Press Enter to return…")
    return server


def pick_dump_source(stdscr: "curses._CursesWindow") -> Optional[str]:
    """Pick Local / Remote dump. Returns 'local', 'remote', or None."""
    options = [
        ("local", "Local dump (.sql / .sql.gz)"),
        ("remote", "Remote dump on host"),
        ("cancel", "← Cancel"),
    ]
    focus = 0
    curses.curs_set(0)
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        core._safe_add(stdscr, 0, 2, "Database import — pick dump source", curses.A_BOLD)
        core._safe_add(
            stdscr,
            1,
            2,
            "↑↓ move · Enter select · Esc cancel",
            curses.color_pair(2),
        )
        for i, (_id, label) in enumerate(options):
            y = 3 + i
            selected = i == focus
            if selected:
                try:
                    stdscr.addnstr(y, 0, " " * max(0, w - 1), w - 1, curses.color_pair(1))
                except curses.error:
                    pass
                attr = curses.color_pair(1) | curses.A_BOLD
            else:
                attr = curses.A_NORMAL
            core._safe_add(stdscr, y, 2, label[: max(0, w - 3)], attr)
        stdscr.refresh()
        key = _getch_blocking(stdscr)
        if key in (curses.KEY_UP, ord("k")):
            focus = (focus - 1) % len(options)
        elif key in (curses.KEY_DOWN, ord("j")):
            focus = (focus + 1) % len(options)
        elif key in (27,):
            return None
        elif key in (curses.KEY_ENTER, 10, 13):
            chosen = options[focus][0]
            return None if chosen == "cancel" else chosen


def pick_local_dump_file(
    stdscr: "curses._CursesWindow",
    start: Optional[str] = None,
) -> Optional[str]:
    """Browse local dirs; Enter on .sql/.sql.gz selects the file."""
    cwd = Path(start or Path.home()).expanduser().resolve()
    if not cwd.is_dir():
        cwd = Path.home()
    idx = 0
    curses.curs_set(0)

    while True:
        try:
            dirs = sorted(
                [p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")],
                key=lambda p: p.name.lower(),
            )
            dumps = sorted(
                [
                    p
                    for p in cwd.iterdir()
                    if p.is_file() and _is_dump_filename(p.name)
                ],
                key=lambda p: p.name.lower(),
            )
        except PermissionError:
            dirs, dumps = [], []

        rows: List[Tuple[str, Optional[Path], str]] = []
        if cwd.parent != cwd:
            rows.append(("[..]", cwd.parent, "dir"))
        for p in dirs:
            rows.append((p.name + "/", p, "dir"))
        for p in dumps:
            rows.append((p.name, p, "file"))

        stdscr.clear()
        h, w = stdscr.getmaxyx()
        core._safe_add(stdscr, 0, 0, "Pick local dump (.sql / .sql.gz)", curses.A_BOLD)
        core._safe_add(
            stdscr,
            1,
            0,
            "↑↓ move · Enter open/select · ← up · Esc cancel",
            curses.color_pair(2),
        )
        core._safe_add(stdscr, 2, 0, str(cwd)[: max(0, w - 1)], curses.A_DIM)

        if not rows:
            core._safe_add(stdscr, 4, 2, "(no folders or dumps here)", curses.A_DIM)

        top = 4
        visible = max(1, h - top - 1)
        idx = max(0, min(idx, max(0, len(rows) - 1)))
        start_i = max(0, min(idx - visible + 1, max(0, len(rows) - visible)))
        for row_i, (label, _path, _kind) in enumerate(rows[start_i : start_i + visible]):
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
        key = _getch_blocking(stdscr)
        if not rows:
            if key in (27, ord("q"), curses.KEY_LEFT):
                if cwd.parent != cwd:
                    cwd = cwd.parent
                    idx = 0
                else:
                    return None
            continue
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
        elif key in (curses.KEY_ENTER, 10, 13):
            _label, path, kind = rows[idx]
            if path is None:
                continue
            if kind == "dir":
                cwd = path
                idx = 0
            else:
                return str(path)


def _remote_list_entries(
    server: core.Server, remote_dir: str
) -> Tuple[int, List[Tuple[str, str]]]:
    """List remote dir → (code, [(name, 'dir'|'file'), ...]). Dumps + dirs only."""
    path_q = shlex.quote(remote_dir)
    # Portable listing (no GNU find -printf). Emits name<TAB>d|f
    cmd = f"""
d={path_q}
if [ ! -d "$d" ]; then
  echo "NOTDIR" >&2
  exit 2
fi
python3 -c 'import os,sys
root=sys.argv[1]
for name in sorted(os.listdir(root), key=str.lower):
  if name.startswith("."): continue
  p=os.path.join(root,name)
  print(name+"\\t"+("d" if os.path.isdir(p) else "f"))
' "$d" 2>/dev/null || {{
  for p in "$d"/* "$d"/.[!.]* "$d"/..?*; do
    [ -e "$p" ] || continue
    n=$(basename "$p")
    case "$n" in .|..) continue ;; esac
    if [ -d "$p" ]; then printf '%s\\td\\n' "$n"
    elif [ -f "$p" ]; then printf '%s\\tf\\n' "$n"
    fi
  done
}}
"""
    code, out, _sudo = ssh_remote_capture_sudo_fallback(
        server, cmd, timeout=60.0, always_retry=True
    )
    if code != 0:
        return code, []
    entries: List[Tuple[str, str]] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        name, kind = line.split("\t", 1)
        name = name.strip()
        if not name or name.startswith("."):
            continue
        if kind.startswith("d"):
            entries.append((name, "dir"))
        elif kind.startswith("f") and _is_dump_filename(name):
            entries.append((name, "file"))
    entries.sort(key=lambda t: (0 if t[1] == "dir" else 1, t[0].lower()))
    return 0, entries


def pick_remote_dump_file(
    stdscr: "curses._CursesWindow",
    server: core.Server,
    start: Optional[str] = None,
) -> Optional[str]:
    """Browse remote dirs over SSH; Enter selects a .sql/.sql.gz file."""
    cwd = normalize_remote_path(start or "/tmp") or "/tmp"
    idx = 0
    status = ""
    curses.curs_set(0)

    while True:
        code, entries = _remote_list_entries(server, cwd)
        if code != 0:
            status = f"List failed (exit {code}). ← up or Esc."
            entries = []
        else:
            status = ""

        rows: List[Tuple[str, Optional[str], str]] = []
        parent = str(Path(cwd).parent) if cwd not in ("/", "") else None
        if parent and parent != cwd:
            rows.append(("[..]", parent, "dir"))
        for name, kind in entries:
            label = name + ("/" if kind == "dir" else "")
            path = cwd.rstrip("/") + "/" + name if cwd != "/" else "/" + name
            rows.append((label, path, kind))

        stdscr.clear()
        h, w = stdscr.getmaxyx()
        core._safe_add(stdscr, 0, 0, "Pick remote dump (.sql / .sql.gz)", curses.A_BOLD)
        core._safe_add(
            stdscr,
            1,
            0,
            "↑↓ move · Enter open/select · ← up · Esc cancel",
            curses.color_pair(2),
        )
        core._safe_add(stdscr, 2, 0, f"{server.label}:{cwd}"[: max(0, w - 1)], curses.A_DIM)
        if status:
            core._safe_add(stdscr, 3, 0, status[: max(0, w - 1)], curses.color_pair(7))

        top = 5
        visible = max(1, h - top - 1)
        idx = max(0, min(idx, max(0, len(rows) - 1)))
        start_i = max(0, min(idx - visible + 1, max(0, len(rows) - visible)))
        if not rows:
            core._safe_add(stdscr, top, 2, "(empty — no dumps/dirs)", curses.A_DIM)
        for row_i, (label, _path, _kind) in enumerate(rows[start_i : start_i + visible]):
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
        key = _getch_blocking(stdscr)
        if not rows:
            if key in (27, ord("q")):
                return None
            if key in (curses.KEY_LEFT,) and parent:
                cwd = parent
                idx = 0
            continue
        if key in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(rows)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(rows)
        elif key in (curses.KEY_LEFT,):
            if parent:
                cwd = parent
                idx = 0
        elif key in (27, ord("q")):
            return None
        elif key in (curses.KEY_ENTER, 10, 13):
            _label, path, kind = rows[idx]
            if not path:
                continue
            if kind == "dir":
                cwd = path
                idx = 0
            else:
                return path


def _mysql_creds_from_server(server: core.Server) -> Optional[Dict[str, str]]:
    """Build runtime creds from YAML fields when user+db are set."""
    if not server.mysql_user or not server.mysql_db:
        return None
    return {
        "host": (server.mysql_host or "127.0.0.1").strip() or "127.0.0.1",
        "port": "3306",
        "username": server.mysql_user,
        "database": server.mysql_db,
        "password": server.mysql_pass or "",
    }


def _prompt_mysql_creds_manual(
    defaults: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, str]]:
    """Interactive MySQL connection. Prefills from defaults/YAML when present."""
    d = defaults or {}
    def_host = (d.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    def_port = (d.get("port") or "3306").strip() or "3306"
    def_user = (d.get("username") or "").strip()
    def_db = (d.get("database") or "").strip()
    has_pass = bool(d.get("password"))

    print("MySQL credentials:")
    print(_c(_ANSI_CYAN_DIM, "  (prefer Vendor/Drush when available; save to YAML only if you choose)"))
    try:
        host_in = input(f"  host [{def_host}]: ").strip()
        host = host_in or def_host
        port_in = input(f"  port [{def_port}]: ").strip()
        port = port_in or def_port
        user_hint = f" [{def_user}]" if def_user else ""
        user = input(f"  user{user_hint}: ").strip() or def_user
        db_hint = f" [{def_db}]" if def_db else ""
        database = input(f"  database{db_hint}: ").strip() or def_db
        if has_pass:
            password = getpass.getpass("  password [saved — Enter to keep]: ")
            if not password:
                password = d.get("password") or ""
        else:
            password = getpass.getpass("  password: ")
    except (EOFError, KeyboardInterrupt):
        print(f"\n{_CANCEL_HINT}")
        return None
    if not user or not database:
        print("user and database are required.")
        return None
    return {
        "host": host,
        "port": port,
        "username": user,
        "database": database,
        "password": password,
    }


def _mysql_creds_match_server(server: core.Server, creds: Dict[str, str]) -> bool:
    saved = _mysql_creds_from_server(server)
    if not saved:
        return False
    return (
        saved["host"] == (creds.get("host") or "127.0.0.1")
        and saved["username"] == creds.get("username")
        and saved["database"] == creds.get("database")
        and (saved.get("password") or "") == (creds.get("password") or "")
    )


def _maybe_persist_mysql_creds(
    server: core.Server,
    creds: Dict[str, str],
    config_path: Optional[Path],
) -> core.Server:
    """After successful MySQL auth, offer y/N to save into servers.yaml."""
    if not config_path:
        return server
    if _mysql_creds_match_server(server, creds):
        return server
    print()
    print(
        _c(
            _ANSI_YELLOW,
            "Save MySQL creds to servers.yaml? (plaintext — prefer keys/Drush when you can)",
        )
    )
    if not _confirm_yn("Save mysql_host / mysql_db / mysql_user / mysql_pass?", default_yes=False):
        return server
    updated = replace(
        server,
        mysql_host=(creds.get("host") or None),
        mysql_db=(creds.get("database") or None),
        mysql_user=(creds.get("username") or None),
        mysql_pass=(creds.get("password") or None),
    )
    try:
        core.update_server(config_path, server.name, updated)
    except Exception as exc:  # noqa: BLE001
        print(_c(_ANSI_RED, f"  Could not save MySQL creds: {exc}"))
        return server
    _print_success_line("[success] MySQL creds saved to servers.yaml")
    print()
    return updated


def _parse_drush_sql_conf(out: str) -> Optional[Dict[str, str]]:
    text = (out or "").strip()
    if not text:
        return None
    # Drush may print warnings before JSON — take last {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Flatten possible nested default.default
    if "database" not in data and isinstance(data.get("default"), dict):
        inner = data["default"]
        if isinstance(inner.get("default"), dict):
            data = inner["default"]
        else:
            data = inner
    db = str(data.get("database") or data.get("databaseName") or "").strip()
    user = str(data.get("username") or data.get("user") or "").strip()
    if not db or not user:
        return None
    return {
        "database": db,
        "username": user,
        "password": str(data.get("password") or ""),
        "host": str(data.get("host") or "127.0.0.1"),
        "port": str(data.get("port") or "3306"),
    }


def resolve_mysql_creds(
    server: core.Server,
    *,
    config_path: Optional[Path] = None,
    has_vendor: bool = False,
) -> Tuple[core.Server, Optional[Dict[str, str]]]:
    """
    Resolve DB creds for mysqldump/mysql.
    Prefer Drush (no plaintext) → YAML saved → interactive prompt (prefilled).
    """
    saved = _mysql_creds_from_server(server)

    if has_vendor and server.web_remote:
        path = normalize_remote_path(server.web_remote)
        server, code, out, _remi = _exec_vendor_shell(
            server,
            path,
            "vendor/bin/drush sql:conf --show-passwords --format=json",
            config_path=config_path,
            timeout=60.0,
        )
        if code == 0:
            creds = _parse_drush_sql_conf(out)
            if creds and creds.get("password") is not None and creds.get("database"):
                print(_c(_ANSI_CYAN_DIM, "  creds: Drush sql:conf"))
                return server, creds
        print(_c(_ANSI_YELLOW, "Could not read DB password via Drush — trying YAML / prompt."))

    if saved and saved.get("username") and saved.get("database"):
        print(
            _c(
                _ANSI_CYAN_DIM,
                f"  creds: YAML ({saved['username']}@{saved['host']}/{saved['database']})",
            )
        )
        return server, saved

    defaults = saved or {}
    if server.mysql_host and "host" not in defaults:
        defaults = {**defaults, "host": server.mysql_host}
    if server.mysql_user and "username" not in defaults:
        defaults = {**defaults, "username": server.mysql_user}
    if server.mysql_db and "database" not in defaults:
        defaults = {**defaults, "database": server.mysql_db}
    if server.mysql_pass and "password" not in defaults:
        defaults = {**defaults, "password": server.mysql_pass}
    return server, _prompt_mysql_creds_manual(defaults or None)


def _mysql_defaults_file_remote_script(creds: Dict[str, str], conf_path: str) -> str:
    """Write a chmod 600 mysql defaults file on the remote (then caller deletes)."""
    # Avoid echoing password in process list / shell history via quoted heredoc
    user_q = shlex.quote(creds["username"])
    host_q = shlex.quote(creds["host"])
    port_q = shlex.quote(str(creds["port"]))
    pass_q = shlex.quote(creds.get("password") or "")
    conf_q = shlex.quote(conf_path)
    return f"""
umask 077
printf '%s\\n' '[client]' > {conf_q}
printf 'user=%s\\n' {user_q} >> {conf_q}
printf 'password=%s\\n' {pass_q} >> {conf_q}
printf 'host=%s\\n' {host_q} >> {conf_q}
printf 'port=%s\\n' {port_q} >> {conf_q}
chmod 600 {conf_q}
"""


# Walker list — Drupal bin caches (TRUNCATE, not DROP).
_MYSQL_CACHE_TRUNCATE_TABLES: Tuple[str, ...] = (
    "cachetags",
    "cache_access_policy",
    "cache_bootstrap",
    "cache_config",
    "cache_container",
    "cache_data",
    "cache_default",
    "cache_discovery",
    "cache_dynamic_page_cache",
    "cache_entity",
    "cache_file_parsing",
    "cache_menu",
    "cache_page",
    "cache_render",
    "cache_routes",
    "cache_toolbar",
)


def action_db_export_drush(
    server: core.Server,
    *,
    config_path: Optional[Path] = None,
) -> core.Server:
    """Drush sql:dump on remote → rsync into web_local/db-dumps/."""
    if not server.web_remote or not server.web_local:
        return server
    path = normalize_remote_path(server.web_remote)
    local_dir = _local_dumps_dir(server)
    assert local_dir is not None
    stamp = _dump_stamp()
    fname = f"{_safe_dump_stem(server.name)}_{stamp}.sql.gz"
    remote_dump = f"/tmp/{fname}"
    local_path = local_dir / fname

    _print_info_panel(
        "Database: Export (Vendor / Drush)",
        [
            f"host:    {server.label}",
            f"remote:  {path}",
            f"dump:    {remote_dump}",
            f"local:   {local_path}",
            "cmd:     vendor/bin/drush sql:dump --gzip --result-file=…",
            "note:    dump written under /tmp then rsynced; remote file removed",
            "confirm: y to run · Enter/N cancels",
        ],
    )
    if not _confirm_yn("Export database via Drush + rsync to local?", default_yes=False):
        return server

    try:
        local_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Cannot create {local_dir}: {exc}")
        _pause()
        return server

    remote_q = shlex.quote(remote_dump)
    shell = (
        f"vendor/bin/drush sql:dump --gzip --result-file={remote_q} "
        f"&& test -f {remote_q}"
    )
    print()
    try:
        server, code, out, _remi = _exec_vendor_shell(
            server, path, shell, config_path=config_path, timeout=900.0
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return server

    if code != 0:
        _print_error_panel(
            "Drush dump failed",
            [f"host: {server.label}", f"path: {path}", f"exit: {code}"],
            out,
        )
        _pause("Press Enter to return…")
        return server

    for line in (out or "").splitlines():
        _print_drush_output_line(line, quiet=False)
    print(_c(_ANSI_CYAN_DIM, f"  rsync → {local_path}"))
    try:
        rc = _rsync_file(
            server,
            src=f"{_ssh_target(server)}:{remote_dump}",
            dest=str(local_path),
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return server

    # Best-effort remote cleanup (never leave dump in /tmp longer than needed)
    ssh_remote_capture_sudo_fallback(
        server, f"rm -f {remote_q}", timeout=30.0, always_retry=True
    )

    if rc != 0:
        _print_error_panel(
            "rsync of dump failed",
            [f"src: {remote_dump}", f"dest: {local_path}", f"exit: {rc}"],
            "",
        )
        _pause("Press Enter to return…")
        return server

    _print_success_line(f"[success] Exported → {local_path}")
    print()
    _pause()
    return server


def action_db_import_drush(
    server: core.Server,
    dump_path: str,
    *,
    source: str,
    config_path: Optional[Path] = None,
) -> core.Server:
    """Import dump via Drush sql:drop + sql:cli (destructive)."""
    if not server.web_remote:
        return server
    path = normalize_remote_path(server.web_remote)
    is_local = source == "local"
    stamp = _dump_stamp()
    base = Path(dump_path).name
    if not _is_dump_filename(base):
        print(f"Not a dump file: {base}")
        _pause()
        return server
    remote_dump = f"/tmp/ssh-gui-import-{_safe_dump_stem(server.name)}-{stamp}-{base}"

    _print_info_panel(
        "Database: Import (Vendor / Drush)",
        [
            f"host:    {server.label}",
            f"path:    {path}",
            f"source:  {source} → {dump_path}",
            f"remote:  {remote_dump}",
            "cmd:     drush sql:drop -y && drush sql:cli < dump",
            "warn:    DESTRUCTIVE — replaces the remote database",
            "confirm: y to run · Enter/N cancels",
        ],
    )
    if not _confirm_yn("DROP remote DB and import this dump?", default_yes=False):
        return server

    remote_q = shlex.quote(remote_dump)
    try:
        if is_local:
            local = Path(dump_path).expanduser().resolve()
            if not local.is_file():
                print(f"Missing local file: {local}")
                _pause()
                return server
            print(_c(_ANSI_CYAN_DIM, f"  rsync {local} → {remote_dump}"))
            rc = _rsync_file(
                server,
                src=str(local),
                dest=f"{_ssh_target(server)}:{remote_dump}",
            )
            if rc != 0:
                _print_error_panel("rsync upload failed", [f"exit: {rc}"], "")
                _pause()
                return server
        else:
            src_q = shlex.quote(dump_path)
            code, out, _s = ssh_remote_capture_sudo_fallback(
                server,
                f"cp -f {src_q} {remote_q} && chmod 600 {remote_q}",
                timeout=120.0,
                always_retry=True,
            )
            if code != 0:
                _print_error_panel("Copy remote dump failed", [f"exit: {code}"], out)
                _pause()
                return server

        if base.lower().endswith(".gz"):
            ingest = f"gunzip -c {remote_q} | vendor/bin/drush sql:cli"
        else:
            ingest = f"vendor/bin/drush sql:cli < {remote_q}"
        shell = f"vendor/bin/drush sql:drop -y && {ingest}"
        server, code, out, _remi = _exec_vendor_shell(
            server, path, shell, config_path=config_path, timeout=900.0
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        ssh_remote_capture_sudo_fallback(
            server, f"rm -f {remote_q}", timeout=30.0, always_retry=True
        )
        return server

    ssh_remote_capture_sudo_fallback(
        server, f"rm -f {remote_q}", timeout=30.0, always_retry=True
    )

    if code != 0:
        _print_error_panel(
            "Drush import failed",
            [f"host: {server.label}", f"path: {path}", f"exit: {code}"],
            out,
        )
        _pause("Press Enter to return…")
        return server

    for line in (out or "").splitlines():
        _print_drush_output_line(line, quiet=False)
    _print_success_line("[success] Database import finished.")
    print()
    _pause()
    return server


def action_db_export_mysql(
    server: core.Server,
    *,
    config_path: Optional[Path] = None,
    has_vendor: bool = False,
) -> core.Server:
    """mysqldump on remote (Walker flags) → rsync to web_local/db-dumps/."""
    if not server.web_remote or not server.web_local:
        return server
    local_dir = _local_dumps_dir(server)
    assert local_dir is not None
    stamp = _dump_stamp()
    fname = f"{_safe_dump_stem(server.name)}_{stamp}.sql.gz"
    remote_dump = f"/tmp/{fname}"
    conf_path = f"/tmp/ssh-gui-my-{_safe_dump_stem(server.name)}-{stamp}.cnf"
    local_path = local_dir / fname

    _print_info_panel(
        "Database: Export (MySQL)",
        [
            f"host:    {server.label}",
            f"dump:    {remote_dump}",
            f"local:   {local_path}",
            "cmd:     mysqldump --single-transaction --quick --lock-tables=false | gzip",
            "creds:   Drush → YAML → prompt (optional save after success)",
            "confirm: y to run · Enter/N cancels",
        ],
    )
    if not _confirm_yn("Export database via mysqldump + rsync?", default_yes=False):
        return server

    server, creds = resolve_mysql_creds(
        server, config_path=config_path, has_vendor=has_vendor
    )
    if not creds:
        _pause()
        return server

    try:
        local_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Cannot create {local_dir}: {exc}")
        _pause()
        return server

    db_q = shlex.quote(creds["database"])
    remote_q = shlex.quote(remote_dump)
    conf_q = shlex.quote(conf_path)
    script = _mysql_defaults_file_remote_script(creds, conf_path) + f"""
set -e
mysqldump --defaults-extra-file={conf_q} --single-transaction --quick --lock-tables=false {db_q} \
  | gzip > {remote_q}
test -s {remote_q}
rm -f {conf_q}
"""
    print()
    try:
        code, out, used_sudo = ssh_remote_capture_sudo_fallback(
            server, script, timeout=900.0, always_retry=True
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        ssh_remote_capture(
            server, f"rm -f {shlex.quote(conf_path)} {remote_q}", timeout=30.0
        )
        return server

    # Always scrub defaults file
    ssh_remote_capture(server, f"rm -f {conf_q}", timeout=15.0)

    if code != 0:
        # Redact password-looking lines just in case
        safe_out = re.sub(r"(?i)password\s*=\s*.*", "password=***", out or "")
        _print_error_panel(
            "mysqldump failed",
            [f"host: {server.label}", f"db: {creds['database']}", f"exit: {code}",
             f"sudo: {used_sudo}"],
            safe_out,
        )
        _pause()
        return server

    print(_c(_ANSI_CYAN_DIM, f"  rsync → {local_path}"))
    try:
        rc = _rsync_file(
            server,
            src=f"{_ssh_target(server)}:{remote_dump}",
            dest=str(local_path),
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return server

    ssh_remote_capture_sudo_fallback(
        server, f"rm -f {remote_q}", timeout=30.0, always_retry=True
    )
    if rc != 0:
        _print_error_panel("rsync of dump failed", [f"exit: {rc}"], "")
        _pause()
        return server

    _print_success_line(f"[success] Exported → {local_path}")
    server = _maybe_persist_mysql_creds(server, creds, config_path)
    print()
    _pause()
    return server


def action_db_import_mysql(
    server: core.Server,
    dump_path: str,
    *,
    source: str,
    config_path: Optional[Path] = None,
    has_vendor: bool = False,
) -> core.Server:
    """Import dump with mysql client (DROP/CREATE database first)."""
    if not server.web_remote:
        return server
    is_local = source == "local"
    stamp = _dump_stamp()
    base = Path(dump_path).name
    if not _is_dump_filename(base):
        print(f"Not a dump file: {base}")
        _pause()
        return server
    remote_dump = f"/tmp/ssh-gui-import-{_safe_dump_stem(server.name)}-{stamp}-{base}"
    conf_path = f"/tmp/ssh-gui-my-{_safe_dump_stem(server.name)}-{stamp}.cnf"

    _print_info_panel(
        "Database: Import (MySQL)",
        [
            f"host:    {server.label}",
            f"source:  {source} → {dump_path}",
            f"remote:  {remote_dump}",
            "cmd:     DROP/CREATE database · mysql < dump",
            "warn:    DESTRUCTIVE — replaces the remote database",
            "confirm: y to run · Enter/N cancels",
        ],
    )
    if not _confirm_yn("DROP remote DB and import this dump via mysql?", default_yes=False):
        return server

    server, creds = resolve_mysql_creds(
        server, config_path=config_path, has_vendor=has_vendor
    )
    if not creds:
        _pause()
        return server

    remote_q = shlex.quote(remote_dump)
    conf_q = shlex.quote(conf_path)
    db_q = shlex.quote(creds["database"])
    db_ident = creds["database"].replace("`", "``")
    drop_sql_q = shlex.quote(
        f"DROP DATABASE IF EXISTS `{db_ident}`; CREATE DATABASE `{db_ident}`;"
    )
    try:
        if is_local:
            local = Path(dump_path).expanduser().resolve()
            if not local.is_file():
                print(f"Missing local file: {local}")
                _pause()
                return server
            print(_c(_ANSI_CYAN_DIM, f"  rsync {local} → {remote_dump}"))
            rc = _rsync_file(
                server,
                src=str(local),
                dest=f"{_ssh_target(server)}:{remote_dump}",
            )
            if rc != 0:
                _print_error_panel("rsync upload failed", [f"exit: {rc}"], "")
                _pause()
                return server
        else:
            src_q = shlex.quote(dump_path)
            code, out, _s = ssh_remote_capture_sudo_fallback(
                server,
                f"cp -f {src_q} {remote_q} && chmod 600 {remote_q}",
                timeout=120.0,
                always_retry=True,
            )
            if code != 0:
                _print_error_panel("Copy remote dump failed", [f"exit: {code}"], out)
                _pause()
                return server

        if base.lower().endswith(".gz"):
            load = f"gunzip -c {remote_q} | mysql --defaults-extra-file={conf_q} {db_q}"
        else:
            load = f"mysql --defaults-extra-file={conf_q} {db_q} < {remote_q}"
        script = _mysql_defaults_file_remote_script(creds, conf_path) + f"""
set -e
mysql --defaults-extra-file={conf_q} -e {drop_sql_q}
{load}
rm -f {conf_q} {remote_q}
"""
        code, out, used_sudo = ssh_remote_capture_sudo_fallback(
            server, script, timeout=900.0, always_retry=True
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        ssh_remote_capture(
            server, f"rm -f {conf_q} {remote_q}", timeout=30.0
        )
        return server

    ssh_remote_capture(server, f"rm -f {conf_q} {remote_q}", timeout=15.0)

    if code != 0:
        safe_out = re.sub(r"(?i)password\s*=\s*.*", "password=***", out or "")
        _print_error_panel(
            "mysql import failed",
            [f"host: {server.label}", f"db: {creds['database']}", f"exit: {code}",
             f"sudo: {used_sudo}"],
            safe_out,
        )
        _pause()
        return server

    _print_success_line("[success] MySQL import finished.")
    server = _maybe_persist_mysql_creds(server, creds, config_path)
    print()
    _pause()
    return server


def action_mysql_clear_cache(
    server: core.Server,
    *,
    config_path: Optional[Path] = None,
    has_vendor: bool = False,
) -> core.Server:
    """
    TRUNCATE Walker’s Drupal cache table list via non-interactive mysql
    (defaults-extra-file). Creds: Drush → YAML → prompt (same as export/import).
    """
    tables = _MYSQL_CACHE_TRUNCATE_TABLES
    _print_info_panel(
        "MySQL: Clear cache",
        [
            f"host:    {server.label}",
            f"tables:  {len(tables)} (cachetags + cache_*)",
            "cmd:     mysql --defaults-extra-file … -e 'TRUNCATE …'",
            "creds:   Drush → YAML → prompt (optional save after success)",
            "confirm: y to run · Enter/N cancels",
        ],
    )
    print(_c(_ANSI_CYAN_DIM, "  Tables:"))
    for t in tables:
        print(_c(_ANSI_CYAN_DIM, f"    · {t}"))
    print()
    if not _confirm_yn("TRUNCATE listed cache tables on remote MySQL?", default_yes=False):
        return server

    server, creds = resolve_mysql_creds(
        server, config_path=config_path, has_vendor=has_vendor
    )
    if not creds:
        _pause()
        return server

    stamp = _dump_stamp()
    conf_path = f"/tmp/ssh-gui-my-cc-{_safe_dump_stem(server.name)}-{stamp}.cnf"
    conf_q = shlex.quote(conf_path)
    db_q = shlex.quote(creds["database"])
    # One statement list; identifiers backtick-escaped (names are fixed constants).
    sql = "; ".join(f"TRUNCATE `{t}`" for t in tables) + ";"
    sql_q = shlex.quote(sql)
    script = _mysql_defaults_file_remote_script(creds, conf_path) + f"""
set -e
mysql --defaults-extra-file={conf_q} --batch --raw {db_q} -e {sql_q}
rm -f {conf_q}
"""
    print()
    try:
        code, out, used_sudo = ssh_remote_capture_sudo_fallback(
            server, script, timeout=120.0, always_retry=True
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        ssh_remote_capture(server, f"rm -f {conf_q}", timeout=15.0)
        return server

    ssh_remote_capture(server, f"rm -f {conf_q}", timeout=15.0)

    if code != 0:
        safe_out = re.sub(r"(?i)password\s*=\s*.*", "password=***", out or "")
        _print_error_panel(
            "MySQL clear cache failed",
            [
                f"host: {server.label}",
                f"db: {creds['database']}",
                f"exit: {code}",
                f"sudo: {used_sudo}",
            ],
            safe_out,
        )
        _pause()
        return server

    _print_success_line(
        f"[success] Truncated {len(tables)} cache table(s) on {creds['database']}"
    )
    server = _maybe_persist_mysql_creds(server, creds, config_path)
    print()
    _pause()
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
        key = _getch_blocking(stdscr)
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
        *_rsync_progress_flags(),
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
        code = run_rsync_pretty(argv, server)
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
    if code == 0:
        print(_c(_ANSI_CYAN_DIM, "  Applying permission preset…"))
        try:
            chmod_code, used_sudo = ssh_remote_sudo_fallback(
                server, chmod_preset_cmd(normalize_remote_path(remote)), interactive=True
            )
        except KeyboardInterrupt:
            print(f"\n{_CANCEL_HINT}")
            return
        if used_sudo:
            print(_c(_ANSI_CYAN_DIM, "  chmod used: sudo -S"))
        if chmod_code == 0:
            _print_success_line("[success] permissions applied")
        else:
            print(_c(_ANSI_RED, f"  chmod exit: {chmod_code}"))
        print()
    _pause()


# Backup dir for Keep-local-changes pull (listed in .git/info/exclude).
_PULL_KEEP_DIR = ".ssh-gui-pull-keep"


def _ssh_out_payload(out: str) -> str:
    """Drop ssh password prompts / CR noise from captured remote output."""
    lines: List[str] = []
    for ln in (out or "").splitlines():
        low = ln.lower()
        if "password:" in low and ("'s password" in low or "password: " in low):
            continue
        lines.append(ln.replace("\r", ""))
    return "\n".join(lines)


def _porcelain_paths(porcelain: str) -> List[str]:
    """Parse `git status --porcelain` into relative paths (handles renames)."""
    paths: List[str] = []
    for raw in (porcelain or "").splitlines():
        line = raw.rstrip("\r")
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if not path or path.startswith(_PULL_KEEP_DIR):
            continue
        paths.append(path)
    # stable unique
    seen = set()
    out: List[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _dirty_path_kind(path: str) -> str:
    """Bucket dirty paths for the pull UX summary."""
    p = path.replace("\\", "/")
    if p == "core" or p.startswith("core/"):
        return "core"
    if (
        p.startswith("vendor/")
        or p in ("composer.lock", "composer.json", "composer.phar")
    ):
        return "vendor"
    return "site"


def _prompt_dirty_pull_choice(prompt: str = "Choice") -> Optional[str]:
    """Return 'discard' | 'keep' | 'select' | None (cancel)."""
    try:
        ans = input(
            f"{prompt} [D]iscard / [K]eep all / [S]elect / [C]ancel: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(f"\n{_CANCEL_HINT}")
        return None
    if not ans or ans in ("c", "cancel", "q"):
        return None
    if ans in ("d", "discard"):
        return "discard"
    if ans in ("k", "keep"):
        return "keep"
    if ans in ("s", "select"):
        return "select"
    print(_c(_ANSI_YELLOW, "  Unknown choice — cancelled."))
    return None


def _kind_label(kind: str) -> str:
    if kind == "core":
        return "core"
    if kind == "vendor":
        return "vendor"
    return "site"


def pick_dirty_keep_paths(
    stdscr: "curses._CursesWindow",
    paths: List[str],
) -> Optional[List[str]]:
    """
    Multi-select dirty paths to KEEP after pull.
    Space toggles, Enter confirms, Esc cancels.
    Returns selected paths (may be empty), or None if cancelled.
    """
    if not paths:
        return []
    # Default: all selected (same as Keep all until user untoggles)
    selected = [True] * len(paths)
    focus = 0
    curses.curs_set(0)
    core._curses_blocking_input(stdscr)

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        core._safe_add(
            stdscr,
            0,
            0,
            "Select files to KEEP (unchecked → discarded by reset)",
            curses.A_BOLD,
        )
        core._safe_add(
            stdscr,
            1,
            0,
            "↑↓ move · Space toggle · a all · n none · Enter confirm · Esc cancel",
            curses.color_pair(2),
        )
        n_keep = sum(1 for s in selected if s)
        core._safe_add(
            stdscr,
            2,
            0,
            f"Keeping {n_keep}/{len(paths)}",
            curses.A_DIM,
        )
        top = 4
        visible = max(1, h - top - 1)
        focus = max(0, min(focus, len(paths) - 1))
        start_i = max(0, min(focus - visible + 1, max(0, len(paths) - visible)))
        for row_i, path in enumerate(paths[start_i : start_i + visible]):
            y = top + row_i
            i = start_i + row_i
            mark = "[x]" if selected[i] else "[ ]"
            kind = _kind_label(_dirty_path_kind(path))
            label = f"{mark} {path}  ({kind})"
            is_focus = i == focus
            if is_focus:
                try:
                    stdscr.addnstr(
                        y, 0, " " * max(0, w - 1), w - 1, curses.color_pair(1)
                    )
                except curses.error:
                    pass
                attr = curses.color_pair(1) | curses.A_BOLD
            else:
                attr = curses.A_NORMAL
            core._safe_add(stdscr, y, 2, label[: max(0, w - 3)], attr)
        stdscr.refresh()

        key = _getch_blocking(stdscr)
        if key in (curses.KEY_UP, ord("k")):
            focus = (focus - 1) % len(paths)
        elif key in (curses.KEY_DOWN, ord("j")):
            focus = (focus + 1) % len(paths)
        elif key == ord(" "):
            selected[focus] = not selected[focus]
        elif key in (ord("a"), ord("A")):
            selected = [True] * len(paths)
        elif key in (ord("n"), ord("N")):
            selected = [False] * len(paths)
        elif key in (27, ord("q")):
            return None
        elif key in (curses.KEY_ENTER, 10, 13):
            return [p for p, on in zip(paths, selected) if on]


def _git_pull_keep_script(
    path_q: str,
    pull_snip: str,
    keep_paths: List[str],
) -> str:
    """
    Backup only keep_paths → exclude dir → reset --hard → pull → restore.
    Unlisted dirty files are discarded by the reset.
    """
    keep_root = shlex.quote(_PULL_KEEP_DIR)
    copy_lines: List[str] = []
    for p in keep_paths:
        pq = shlex.quote(p)
        copy_lines.append(
            f"""
if [ -e {pq} ] || [ -L {pq} ]; then
  mkdir -p "$KEEP/$(dirname -- {pq})"
  cp -a -- {pq} "$KEEP"/{pq}
  printf '%s\\n' {pq} >> "$KEEP/MANIFEST"
fi
"""
        )
    copies = "\n".join(copy_lines) if copy_lines else "true\n"
    return f"""
set -e
cd {path_q}
KEEP_ROOT={keep_root}
STAMP=$(date +%Y%m%d-%H%M%S)
KEEP="$KEEP_ROOT/$STAMP"
mkdir -p "$KEEP"
mkdir -p .git/info
EXCLUDE=.git/info/exclude
touch "$EXCLUDE"
grep -qxF "$KEEP_ROOT/" "$EXCLUDE" 2>/dev/null || echo "$KEEP_ROOT/" >> "$EXCLUDE"
: > "$KEEP/MANIFEST"
git status --porcelain > "$KEEP/status.before" || true
{copies}
echo "Keep: backed up $(wc -l < "$KEEP/MANIFEST" | tr -d ' ') path(s) -> $KEEP"
git reset --hard HEAD
{pull_snip}
if [ -f "$KEEP/MANIFEST" ]; then
  while IFS= read -r path || [ -n "$path" ]; do
    [ -z "$path" ] && continue
    if [ -e "$KEEP/$path" ] || [ -L "$KEEP/$path" ]; then
      mkdir -p "$(dirname -- "$path")"
      cp -a -- "$KEEP/$path" "$path"
    fi
  done < "$KEEP/MANIFEST"
  echo "Keep: restored overwrite from $KEEP"
fi
"""


def _git_pull_ff_snippet(server: core.Server) -> str:
    """Shell fragment: ff-only pull (auth URL when git_user/token set)."""
    if server.git_user and server.git_token:
        u = urllib.parse.quote(server.git_user, safe="")
        t = urllib.parse.quote(server.git_token, safe="")
        return f"""
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
    return "git pull --ff-only\n"


def remote_git_dirty_list(
    server: core.Server,
) -> Tuple[int, List[str], str]:
    """
    Return (code, porcelain paths, raw payload). code!=0 → probe failed.
    Does not modify the remote.
    """
    if not server.web_remote:
        return 1, [], "no web_remote"
    path_q = shlex.quote(normalize_remote_path(server.web_remote))
    cmd = f"cd {path_q} && git status --porcelain"
    code, out, _sudo = ssh_remote_capture_sudo_fallback(
        server, cmd, timeout=60.0, always_retry=True
    )
    payload = _ssh_out_payload(out)
    if code != 0:
        return code, [], payload
    return 0, _porcelain_paths(payload), payload


def action_git_pull(
    server: core.Server,
    stdscr: Optional["curses._CursesWindow"] = None,
) -> None:
    """
    git pull --ff-only on web_remote.
    If dirty: Discard (reset --hard + pull, y/N), Keep all, or Select
    (curses multi-select: kept → same exclude/backup/restore; unkept →
    discarded by reset). Never pushes.
    """
    if not server.web_remote:
        return
    path = normalize_remote_path(server.web_remote)
    path_q = shlex.quote(path)
    pull_snip = _git_pull_ff_snippet(server)

    info_lines = [
        f"host:  {server.label}",
        f"path:  {path}",
        f"mode:  git pull --ff-only",
    ]
    if server.git_user:
        info_lines.append(f"auth:  {server.git_user} (token: set)")
    else:
        info_lines.append("auth:  none in Edit (OK for public/SSH remotes)")
    info_lines.append("note:  non-sudo first; sudo -S fallback if password in YAML")
    info_lines.append(_RUN_KEYS_NOTE)
    _print_info_panel("Git pull", info_lines)

    print(_c(_ANSI_CYAN_DIM, "  Checking remote working tree…"))
    try:
        probe_code, dirty_paths, probe_out = remote_git_dirty_list(server)
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
    if probe_code != 0:
        _print_error_panel(
            "Could not read git status",
            [f"host: {server.label}", f"path: {path}"],
            probe_out,
        )
        _pause()
        return

    mode = "clean"  # clean | discard | keep
    keep_paths: List[str] = []
    if dirty_paths:
        by_kind: Dict[str, List[str]] = {"core": [], "vendor": [], "site": []}
        for p in dirty_paths:
            by_kind[_dirty_path_kind(p)].append(p)
        lines = [
            f"{len(dirty_paths)} dirty path(s) on remote — pull needs a clean tree.",
            "Discard = git reset --hard HEAD, then pull (loses all local edits).",
            f"Keep    = backup all to {_PULL_KEEP_DIR}/, reset, pull, restore.",
            "Select  = choose which files to keep (Space); rest discarded by reset.",
            "",
        ]
        for kind, label in (
            ("core", "Drupal core"),
            ("vendor", "Composer / vendor"),
            ("site", "Site-specific"),
        ):
            group = by_kind[kind]
            if not group:
                continue
            lines.append(f"{label}:")
            for p in group:
                lines.append(f"  · {p}")
            lines.append("")
        _print_info_panel("Local changes on remote", lines)
        choice = _prompt_dirty_pull_choice()
        if choice is None:
            return
        if choice == "discard":
            if not _confirm_yn(
                "Discard ALL listed local changes on the remote?",
                default_yes=False,
            ):
                print(_c(_ANSI_YELLOW, "  Cancelled — nothing discarded."))
                _pause()
                return
            mode = "discard"
        elif choice == "keep":
            mode = "keep"
            keep_paths = list(dirty_paths)
        else:  # select
            if stdscr is None:
                print(
                    _c(
                        _ANSI_RED,
                        "  Select needs the TUI window — use Keep/Discard, or reopen →.",
                    )
                )
                _pause()
                return
            # Picker needs curses; we are suspended (endwin) for stdout panels
            curses.reset_prog_mode()
            core._curses_blocking_input(stdscr)
            try:
                picked = pick_dirty_keep_paths(stdscr, dirty_paths)
            finally:
                curses.def_prog_mode()
                curses.endwin()
            if picked is None:
                print(_c(_ANSI_YELLOW, "  Select cancelled."))
                _pause()
                return
            if not picked:
                if not _confirm_yn(
                    "No files selected — discard ALL local changes?",
                    default_yes=False,
                ):
                    print(_c(_ANSI_YELLOW, "  Cancelled."))
                    _pause()
                    return
                mode = "discard"
            else:
                mode = "keep"
                keep_paths = picked
                print(
                    _c(
                        _ANSI_CYAN_DIM,
                        f"  Keeping {len(keep_paths)}/{len(dirty_paths)} path(s); "
                        "rest discarded by reset.",
                    )
                )
    else:
        print(_c(_ANSI_GREEN, "  Working tree clean."))

    if mode == "discard":
        remote_script = f"""
set -e
cd {path_q}
echo "Discard: git reset --hard HEAD"
git reset --hard HEAD
{pull_snip}
"""
    elif mode == "keep":
        remote_script = _git_pull_keep_script(path_q, pull_snip, keep_paths)
    else:
        remote_script = f"""
set -e
cd {path_q}
{pull_snip}
"""

    # Blank after clean/dirty status before confirm wait
    print()
    if not _pause("Press Enter to run…"):
        return
    try:
        code, out, used_sudo = ssh_remote_capture_sudo_fallback(
            server, remote_script, timeout=300.0, always_retry=True
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
    _print_git_pull_output(_ssh_out_payload(out))
    if used_sudo:
        print("  used: sudo -S")
    print(f"Exit: {code}")
    _pause()


def _colorize_git_log_line(line: str) -> str:
    """Light colorization: yellow hash, rest default (else whole line yellow)."""
    s = line.rstrip("\n")
    m = re.match(r"^([0-9a-f]{7,40})(\s.*)?$", s, re.IGNORECASE)
    if m:
        rest = m.group(2) or ""
        return _c(_ANSI_YELLOW, m.group(1)) + rest
    return _c(_ANSI_YELLOW, s)


def action_commit_history(server: core.Server, limit: int = 20) -> None:
    if not server.web_remote:
        return
    path = normalize_remote_path(server.web_remote)
    path_q = shlex.quote(path)
    fmt = shlex.quote("%h %ad %an %s")
    cmd = f"git -C {path_q} log -n {int(limit)} --date=short --pretty=format:{fmt}"
    _print_info_panel(
        "Git history",
        [
            f"host:  {server.label}",
            f"path:  {path}",
            f"cmd:   git log -n {limit} --pretty=…",
            "note:  non-sudo first; sudo -S fallback if password in YAML",
            _RUN_KEYS_NOTE,
        ],
    )
    if not _pause("Press Enter to run…"):
        return
    print()  # blank after top Enter
    try:
        code, out, used_sudo = ssh_remote_capture_sudo_fallback(
            server, cmd, timeout=60.0, always_retry=True
        )
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
    text = (out or "").rstrip()
    if text:
        for line in text.splitlines():
            print(_colorize_git_log_line(line))
    elif code == 0:
        print(_c(_ANSI_YELLOW, "(no commits)"))
    if code != 0:
        _print_error_panel(
            "Git history failed",
            [
                f"host: {server.label}",
                f"path: {path}",
                f"exit: {code}",
            ],
            out,
        )
    elif used_sudo:
        print(_c(_ANSI_CYAN_DIM, "  used: sudo -S"))
    print()  # blank before bottom Enter
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
        *_rsync_progress_flags(),
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
        run_rsync_pretty(argv, server)
    except KeyboardInterrupt:
        print(f"\n{_CANCEL_HINT}")
        return
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
    core._curses_blocking_input(stdscr)

    while True:
        core._curses_blocking_input(stdscr)
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
            can_db_xfer = bool(has_local)
            vendor_actions = [
                ("drush_cr", "Rebuild cache", True),
                ("drush_updb", "Database: Update", True),
                (
                    "drush_db_export",
                    "Database: Export"
                    if can_db_xfer
                    else "Database: Export  (set web_local in Edit)",
                    can_db_xfer,
                ),
                ("drush_db_import", "Database: Import", True),
                ("drush_uli", "Admin login", True),
                ("drush_cim", "Config: Import", True),
                ("drush_cex", "Config: Export", True),
            ]

        # MySQL group when web_remote is set (raw mysqldump/mysql)
        mysql_actions: List[Tuple[str, str, bool]] = []
        if has_remote:
            can_db_xfer = bool(has_local)
            mysql_actions = [
                ("mysql_clear_cache", "Clear cache", True),
                (
                    "mysql_db_export",
                    "Database: Export"
                    if can_db_xfer
                    else "Database: Export  (set web_local in Edit)",
                    can_db_xfer,
                ),
                ("mysql_db_import", "Database: Import", True),
            ]

        selectable: List[Tuple[str, str, bool]] = [
            *git_actions,
            *deploy_actions,
            *vendor_actions,
            *mysql_actions,
            ("back", "← Back to host list", True),
        ]

        # Display rows: ('section', title) | ('item', selectable_index)
        git_n = len(git_actions)
        deploy_n = len(deploy_actions)
        vendor_n = len(vendor_actions)
        mysql_n = len(mysql_actions)
        display: List[Tuple[str, Any]] = [
            ("section", "Git"),
            *[("item", i) for i in range(git_n)],
            ("section", "Deploy"),
            *[("item", git_n + i) for i in range(deploy_n)],
        ]
        if vendor_n:
            display.append(("section", "Vendor"))
            display.extend(("item", git_n + deploy_n + i) for i in range(vendor_n))
        if mysql_n:
            display.append(("section", "MySQL"))
            display.extend(
                ("item", git_n + deploy_n + vendor_n + i) for i in range(mysql_n)
            )
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
        key = _getch_blocking(stdscr)
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
                    action_git_pull(server, stdscr=stdscr)
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
                        "Database: Update",
                        "updatedb -y",
                        config_path=config_path,
                    )
                finally:
                    _resume_curses(stdscr)
                status = "Database: Update finished."
            elif action == "drush_db_export":
                _suspend_curses(stdscr)
                try:
                    server = action_db_export_drush(server, config_path=config_path)
                finally:
                    _resume_curses(stdscr)
                status = "Database: Export (Vendor) finished."
            elif action == "drush_db_import":
                source = pick_dump_source(stdscr)
                dump: Optional[str] = None
                if source == "local":
                    start = None
                    dumps = _local_dumps_dir(server)
                    if dumps and dumps.is_dir():
                        start = str(dumps)
                    elif server.web_local:
                        start = server.web_local
                    dump = pick_local_dump_file(stdscr, start)
                elif source == "remote":
                    start_r = "/tmp"
                    if server.web_remote:
                        parent = str(Path(normalize_remote_path(server.web_remote)).parent)
                        if parent:
                            start_r = parent
                    dump = pick_remote_dump_file(stdscr, server, start_r)
                if source and dump:
                    _suspend_curses(stdscr)
                    try:
                        server = action_db_import_drush(
                            server,
                            dump,
                            source=source,
                            config_path=config_path,
                        )
                    finally:
                        _resume_curses(stdscr)
                    status = "Database: Import (Vendor) finished."
                else:
                    status = "Import cancelled."
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
            elif action == "mysql_clear_cache":
                _suspend_curses(stdscr)
                try:
                    server = action_mysql_clear_cache(
                        server,
                        config_path=config_path,
                        has_vendor=bool(has_vendor),
                    )
                finally:
                    _resume_curses(stdscr)
                status = "MySQL: Clear cache finished."
            elif action == "mysql_db_export":
                _suspend_curses(stdscr)
                try:
                    server = action_db_export_mysql(
                        server,
                        config_path=config_path,
                        has_vendor=bool(has_vendor),
                    )
                finally:
                    _resume_curses(stdscr)
                status = "Database: Export (MySQL) finished."
            elif action == "mysql_db_import":
                source = pick_dump_source(stdscr)
                dump = None
                if source == "local":
                    start = None
                    dumps = _local_dumps_dir(server)
                    if dumps and dumps.is_dir():
                        start = str(dumps)
                    elif server.web_local:
                        start = server.web_local
                    dump = pick_local_dump_file(stdscr, start)
                elif source == "remote":
                    start_r = "/tmp"
                    if server.web_remote:
                        parent = str(Path(normalize_remote_path(server.web_remote)).parent)
                        if parent:
                            start_r = parent
                    dump = pick_remote_dump_file(stdscr, server, start_r)
                if source and dump:
                    _suspend_curses(stdscr)
                    try:
                        server = action_db_import_mysql(
                            server,
                            dump,
                            source=source,
                            config_path=config_path,
                            has_vendor=bool(has_vendor),
                        )
                    finally:
                        _resume_curses(stdscr)
                    status = "Database: Import (MySQL) finished."
                else:
                    status = "Import cancelled."
