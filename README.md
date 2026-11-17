# ssh-gui

Terminal TUI to quickly SSH into hosts defined in a local YAML inventory, plus a host **actions** menu for deploy helpers (rsync, git pull, permissions).

## Requirements

- macOS with Python 3 (system Python is fine)
- `ssh` / `rsync` (OpenSSH)
- Preferred: SSH keys via `identity` instead of `pass`
- YAML passwords are auto-typed at the SSH prompt (stdlib PTY; no sshpass required)

## Quick install

```bash
cd ~/ssh-gui
./install.sh
```

This creates the venv, installs PyYAML, and symlinks `~/.local/bin/ssh-gui`.

If `ssh-gui` is not found:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

## Configuration

```yaml
my_server_name:
  host: 10.0.0.5
  user: jan
  pass: "optional"
  port: 22
  identity: ~/.ssh/id_ed25519
  notes: "short menu label"
  web_remote: /var/www/app          # remote path for rsync / git
  web_local: ~/Projects/my-app      # local folder to upload
  git_user: jan                     # optional HTTPS git auth on remote
  git_token: "never-commit-this"
```

**Important:** `servers.yaml` is gitignored. Never push real passwords or `git_token` values. Only commit `servers.example.yaml`.

## Usage

```bash
ssh-gui              # TUI
ssh-gui --list
ssh-gui --dry-run
ssh-gui --connect NAME
```

### Main list

| Key | Action |
|-----|--------|
| ↑↓ | Move |
| Enter | SSH connect (or open Add host) |
| e | **Edit** durable config (SSH + `web_*` + `git_*`) |
| → | Host **runtime actions** (deploy helpers) |
| q | Quit |

### Edit / Add (`e`)

Owns all saved fields: host/user/pass/identity/notes, plus `web_remote`, `web_local` (Enter on **Web local** opens a folder browser), `git_user`, `git_token`.

### Host actions (`→`)

Runtime only — no path settings here. Missing config shows as disabled with a hint to use Edit.

| Action | When enabled |
|--------|----------------|
| Git pull | `web_remote` is a git work tree |
| Commit history | same (`git log` short on remote) |
| Upload (rsync) + fix permissions | both `web_local` and `web_remote` set |
| Download (rsync) | same — remote → local (simple rsync progress like upload) |
| Fix permissions only | `web_remote` set |
| ← / q | Back to host list |


Each enabled run prints the command and waits for Enter before executing.

**Permission preset:** after a successful upload rsync (and via “Fix permissions only”):

```bash
find <web_remote> -type d -exec chmod 755 {} +
find <web_remote> -type f -exec chmod 644 {} +
```

## Auth

| Situation | Behaviour |
|-----------|-----------|
| `identity` set | `ssh -i …` |
| `pass` without identity | Password auto-typed at the prompt (PTY) |
| Neither | Interactive SSH auth |

## Security

- Keep `servers.yaml` mode `600`.
- Prefer keys over plaintext passwords / tokens.
- Never commit `servers.yaml` or real `git_token` values.

## Layout

```
~/ssh-gui/
  ssh_gui.py
  host_actions.py         # → submenu (rsync / git / paths)
  bin/ssh-gui
  servers.example.yaml
  servers.yaml            # LOCAL (gitignored)
  …
```

## Try it

```bash
ssh-gui
# highlight a host → press →
# set web remote + local dir, then Upload or Git pull
# Enter on the host still opens a normal SSH session
```
