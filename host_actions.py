#!/usr/bin/env python3
"""Host actions submenu: web paths, local dir picker, git pull, rsync+chmod."""

from __future__ import annotations

import curses
import os
import shlex
import subprocess
import sys
import urllib.parse
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


def _pause(msg: str = "Press Enter to continue…") -> None:
    try:
        input(msg)
    except EOFError:
        pass


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
    if interactive:
        return subprocess.call(argv)
    return subprocess.call(argv)


def ssh_remote(server: core.Server, remote_cmd: str, interactive: bool = False) -> int:
    argv = ["ssh", *_ssh_opts(server), _ssh_target(server), remote_cmd]
    if interactive or core.uses_yaml_password(server):
        return run_argv(argv, server, interactive=True)
    return subprocess.call(argv)


def ssh_remote_capture(server: core.Server, remote_cmd: str, timeout: float = 60.0) -> Tuple[int, str]:
    """
    Run a short remote command and capture stdout+stderr.
    Password hosts: use a one-shot PTY expect (output may include the prompt line).
    """
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

    # Password path: reuse PTY helper but we need capture — run interactively and
    # ask the user to rely on visible output for MVP detect; use a temp approach:
    # Write output via ssh with BatchMode alternative — feed password through PTY
    # and collect bytes without full stdin relay after auth for short commands.
    return _ssh_capture_with_password(argv, server.password or "", timeout)


def _ssh_capture_with_password(argv: List[str], password: str, timeout: float) -> Tuple[int, str]:
    import errno
    import pty
    import select
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
    try:
        while time.time() < deadline:
            try:
                exited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if exited == pid:
                while True:
                    r, _, _ = select.select([master], [], [], 0)
                    if not r:
                        break
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    collected += chunk
                code = (
                    os.waitstatus_to_exitcode(status)
                    if hasattr(os, "waitstatus_to_exitcode")
                    else (os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1)
                )
                return code, collected.decode("utf-8", errors="replace")

            r, _, _ = select.select([master], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            collected += data
            buf += data
            if len(buf) > 8192:
                buf = buf[-4096:]
            if not password_sent and core._HOSTKEY_RE.search(buf):
                os.write(master, b"yes\n")
                buf = b""
            elif not password_sent and core._PASSWORD_RE.search(buf):
                os.write(master, password.encode("utf-8", errors="surrogateescape") + b"\n")
                password_sent = True
                buf = b""
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        return 124, collected.decode("utf-8", errors="replace")
    finally:
        try:
            os.close(master)
        except OSError:
            pass


def remote_is_git(server: core.Server) -> bool:
    if not server.web_remote:
        return False
    path = shlex.quote(server.web_remote)
    cmd = f"git -C {path} rev-parse --is-inside-work-tree >/dev/null 2>&1"
    code, _ = ssh_remote_capture(server, cmd)
    return code == 0


def prompt_line(stdscr: "curses._CursesWindow", title: str, initial: str = "") -> Optional[str]:
    """Simple single-field prompt. Esc cancels."""
    buf = list(initial)
    pos = len(buf)
    curses.curs_set(1)
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        core._safe_add(stdscr, 0, 0, title, curses.A_BOLD)
        core._safe_add(stdscr, 1, 0, "Enter confirm · Esc cancel", curses.color_pair(2))
        shown = "".join(buf)
        core._safe_add(stdscr, 3, 2, shown[: max(0, w - 3)], curses.color_pair(1) | curses.A_BOLD)
        try:
            stdscr.move(3, min(2 + pos, w - 2))
        except curses.error:
            pass
        stdscr.refresh()
        key = stdscr.get_wch()
        if key in ("\n", "\r", curses.KEY_ENTER):
            curses.curs_set(0)
            return "".join(buf).strip()
        if key == "\x1b":
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
        elif key == curses.KEY_DC and pos < len(buf):
            del buf[pos]
        elif isinstance(key, str) and key.isprintable():
            buf.insert(pos, key)
            pos += 1


def pick_local_directory(
    stdscr: "curses._CursesWindow",
    start: Optional[str] = None,
) -> Optional[str]:
    """
    Terminal folder browser.
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

        # Synthetic rows: select current, then dirs
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
            label, path = rows[idx]
            if path is None:
                # Select this folder
                return str(cwd)
            cwd = path
            idx = 0


def patch_server(config_path: Path, server: core.Server, **fields: Any) -> core.Server:
    """Update selected fields on a server and persist."""
    data = server.to_yaml_dict()
    # Rebuild Server via merge
    for k, v in fields.items():
        setattr(server, k, v)
    core.update_server(config_path, server.name, server)
    # Reload to normalize paths
    for s in core.load_servers(config_path):
        if s.name == server.name:
            return s
    return server


def ensure_web_paths(
    stdscr: "curses._CursesWindow",
    config_path: Path,
    server: core.Server,
) -> Optional[core.Server]:
    """Prompt for missing web_remote / web_local. Returns updated server or None if cancelled."""
    if not server.web_remote:
        val = prompt_line(stdscr, "Remote web path (e.g. /var/www/app)", server.web_remote or "")
        if val is None:
            return None
        if not val:
            return None
        server = patch_server(config_path, server, web_remote=val)
    if not server.web_local:
        picked = pick_local_directory(stdscr, server.web_local)
        if picked is None:
            return None
        server = patch_server(config_path, server, web_local=picked)
    return server


def chmod_preset_cmd(web_remote: str) -> str:
    path = shlex.quote(web_remote)
    return (
        f"find {path} -type d -exec chmod {PERM_PRESET_DIRS} {{}} + && "
        f"find {path} -type f -exec chmod {PERM_PRESET_FILES} {{}} +"
    )


def action_fix_permissions(stdscr: "curses._CursesWindow", server: core.Server) -> None:
    if not server.web_remote:
        return
    cmd = chmod_preset_cmd(server.web_remote)
    print(f"→ Fix permissions on {server.label}")
    print(f"  preset: dirs {PERM_PRESET_DIRS}, files {PERM_PRESET_FILES}")
    print(f"  remote: {server.web_remote}")
    print(f"  cmd: ssh … {cmd}")
    _pause("Press Enter to run (Ctrl+C abort)…")
    try:
        code = ssh_remote(server, cmd, interactive=True)
    except KeyboardInterrupt:
        print("\nCancelled.")
        code = 130
    print(f"Exit: {code}")
    _pause()


def action_rsync_upload(stdscr: "curses._CursesWindow", server: core.Server) -> None:
    local = server.web_local
    remote = server.web_remote
    if not local or not remote:
        return
    local_path = Path(local).expanduser()
    if not local_path.is_dir():
        print(f"Local path is not a directory: {local_path}")
        _pause()
        return

    # Trailing slashes: sync contents into remote dir
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
    print(f"→ Upload (rsync) to {server.label}")
    print(f"  local:  {src}")
    print(f"  remote: {dest}")
    print(f"  cmd:    {' '.join(shlex.quote(a) for a in argv)}")
    print(f"  after:  chmod dirs {PERM_PRESET_DIRS} / files {PERM_PRESET_FILES}")
    _pause("Press Enter to run rsync+chmod (Ctrl+C abort)…")
    try:
        code = run_argv(argv, server, interactive=True)
    except KeyboardInterrupt:
        print("\nCancelled.")
        code = 130
    print(f"rsync exit: {code}")
    if code == 0:
        print("Applying permission preset…")
        chmod_code = ssh_remote(server, chmod_preset_cmd(remote), interactive=True)
        print(f"chmod exit: {chmod_code}")
    _pause()


def action_git_pull(
    stdscr: "curses._CursesWindow",
    config_path: Path,
    server: core.Server,
) -> core.Server:
    """Run after curses is suspended (plain terminal I/O)."""
    if not server.web_remote:
        return server
    path_q = shlex.quote(server.web_remote)

    if not server.git_user or not server.git_token:
        try:
            ans = input("Set git_user / git_token for private HTTPS? [y/N]: ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("y", "yes"):
            try:
                gu = input(f"Git username [{server.git_user or ''}]: ").strip() or (server.git_user or "")
                gt = input("Git token / password: ").strip() or (server.git_token or "")
            except EOFError:
                return server
            server = patch_server(
                config_path,
                server,
                git_user=gu or None,
                git_token=gt or None,
            )

    # Build remote pull script
    if server.git_user and server.git_token:
        # Embed URL-encoded creds (avoid shell metacharacters in the script)
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

    print(f"→ Git pull on {server.label}")
    print(f"  path: {server.web_remote}")
    if server.git_user:
        print(f"  git user: {server.git_user} (token: set)")
    else:
        print("  git auth: none stored (public/SSH remotes OK)")
    print("  mode: git pull --ff-only")
    _pause("Press Enter to run (Ctrl+C abort)…")
    try:
        code = ssh_remote(server, remote_script, interactive=True)
    except KeyboardInterrupt:
        print("\nCancelled.")
        code = 130
    print(f"Exit: {code}")
    _pause()
    return server


def host_actions_menu(
    stdscr: "curses._CursesWindow",
    config_path: Path,
    server: core.Server,
) -> core.Server:
    """
    Actions for one host. ← / q returns to main list.
    Reloads/patches server as fields change.
    """
    idx = 0
    status = ""
    detecting = True
    is_git: Optional[bool] = None

    while True:
        # Refresh server from disk each loop so yaml edits stick
        for s in core.load_servers(config_path):
            if s.name == server.name:
                server = s
                break

        if detecting:
            status = "Detecting remote git…"
            stdscr.clear()
            core._safe_add(stdscr, 0, 0, f"Actions — {server.name}", curses.A_BOLD)
            core._safe_add(stdscr, 2, 0, status, curses.color_pair(2))
            stdscr.refresh()
            try:
                is_git = remote_is_git(server) if server.web_remote else False
            except Exception as exc:  # noqa: BLE001 — show in UI
                is_git = False
                status = f"Git detect failed: {exc}"
            else:
                status = "Remote is a git repo." if is_git else "Remote is not a git repo (or path unset)."
            detecting = False

        items: List[Tuple[str, str]] = [
            ("set_remote", f"Set / change web remote path    {server.web_remote or '(not set)'}"),
            ("set_local", f"Set / change local project dir  {server.web_local or '(not set)'}"),
            ("git_creds", f"Git user/token                 {server.git_user or '(not set)'}"),
        ]
        if is_git:
            items.append(("git_pull", "Git pull"))
        items.extend(
            [
                ("upload", "Upload (rsync) + fix permissions"),
                ("chmod", "Fix permissions only (dirs 755 / files 644)"),
                ("redetect", "Re-detect remote git"),
                ("back", "← Back to host list"),
            ]
        )

        stdscr.clear()
        h, w = stdscr.getmaxyx()
        core._safe_add(stdscr, 0, 0, f"Actions — {server.name} ({server.host})", curses.A_BOLD)
        core._safe_add(stdscr, 1, 0, "Enter run · ← / q back", curses.color_pair(2))
        if status:
            core._safe_add(stdscr, 2, 0, status[: max(0, w - 1)], curses.A_DIM)

        top = 4
        idx = max(0, min(idx, len(items) - 1))
        for i, (_key, label) in enumerate(items):
            y = top + i
            if y >= h - 1:
                break
            selected = i == idx
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
            idx = (idx - 1) % len(items)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(items)
        elif key in (curses.KEY_LEFT, 27, ord("q")):
            return server
        elif key in (curses.KEY_ENTER, 10, 13):
            action = items[idx][0]
            if action == "back":
                return server
            if action == "set_remote":
                val = prompt_line(
                    stdscr,
                    "Remote web path (absolute path on server)",
                    server.web_remote or "",
                )
                if val is not None:
                    server = patch_server(config_path, server, web_remote=val or None)
                    detecting = True
                    status = "Saved web_remote."
            elif action == "set_local":
                picked = pick_local_directory(stdscr, server.web_local)
                if picked is not None:
                    server = patch_server(config_path, server, web_local=picked)
                    status = f"Saved web_local: {picked}"
            elif action == "git_creds":
                gu = prompt_line(stdscr, "Git username", server.git_user or "")
                if gu is None:
                    continue
                gt = prompt_line(stdscr, "Git token / password", server.git_token or "")
                if gt is None:
                    continue
                server = patch_server(
                    config_path,
                    server,
                    git_user=gu or None,
                    git_token=gt or None,
                )
                status = "Saved git credentials (local yaml only)."
            elif action == "redetect":
                detecting = True
            elif action == "git_pull":
                _suspend_curses(stdscr)
                try:
                    server = action_git_pull(stdscr, config_path, server)
                finally:
                    _resume_curses(stdscr)
                status = "Git pull finished."
            elif action == "upload":
                updated = ensure_web_paths(stdscr, config_path, server)
                if updated is None:
                    status = "Upload cancelled (need web_local + web_remote)."
                    continue
                server = updated
                _suspend_curses(stdscr)
                try:
                    action_rsync_upload(stdscr, server)
                finally:
                    _resume_curses(stdscr)
                status = "Upload finished."
            elif action == "chmod":
                if not server.web_remote:
                    val = prompt_line(stdscr, "Remote web path required", "")
                    if not val:
                        status = "web_remote required."
                        continue
                    server = patch_server(config_path, server, web_remote=val)
                _suspend_curses(stdscr)
                try:
                    action_fix_permissions(stdscr, server)
                finally:
                    _resume_curses(stdscr)
                status = "Permissions fix finished."
