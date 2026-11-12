# ssh-gui

Terminal TUI to quickly SSH into hosts defined in a local YAML inventory.

## Requirements

- macOS with Python 3 (system Python is fine)
- `ssh` (OpenSSH)
- Optional: [sshpass](https://formulae.brew.sh/) to inject a YAML password  
  `brew install hudochenkov/sshpass/sshpass`
- Preferred: SSH keys via `identity` instead of `pass`

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

1. Copy the example (also done by `install.sh` when missing):

```bash
cp servers.example.yaml servers.yaml
chmod 600 servers.yaml
```

2. Edit `servers.yaml`, **or** add hosts from the TUI (`[+] Add host`):

```yaml
my_server_name:
  host: 10.0.0.5
  user: jan
  pass: "optional"
  port: 22
  identity: ~/.ssh/id_ed25519   # optional; wins over pass when both set
  notes: "short menu label"
```

**Important:** `servers.yaml` is gitignored. Never push real credentials. Only commit `servers.example.yaml`.

## Usage

```bash
ssh-gui              # TUI (↑↓ + Enter, q quit; [+] Add host at the bottom)
ssh-gui --list       # list without menu
ssh-gui --dry-run    # pick a host and print the command
ssh-gui --connect my_server_name
ssh-gui --add        # add a host via plain prompts
ssh-gui -c ~/other.yaml
```

In the menu, hosts are listed first; then a blank line; then **`[+] Add host`** (`[+]` green, rest white). Selecting it opens a form and appends the host to `servers.yaml`.

## Auth

| Situation | Behaviour |
|-----------|-----------|
| `identity` set | `ssh -i …` |
| `pass` without identity + `sshpass` installed | `sshpass -p … ssh …` |
| `pass` without `sshpass` | Warning + SSH prompts interactively |
| Neither | Normal interactive SSH auth |

## Security

- Keep `servers.yaml` mode `600`.
- Prefer keys (`identity`) over plaintext passwords.
- Do not commit secrets; `.gitignore` excludes `servers.yaml` and `.venv/`.
- `sshpass` exposes the password in the process argv — local/trusted use only.

## Layout

```
~/ssh-gui/
  ssh_gui.py              # logic + TUI (curses)
  bin/ssh-gui             # launcher → .venv
  servers.example.yaml    # safe template for the repo
  servers.yaml            # LOCAL secrets (gitignored)
  requirements.txt
  install.sh
  README.md
  .gitignore
```

## Future GitHub repo

```bash
cd ~/ssh-gui
git add .gitignore README.md requirements.txt install.sh ssh_gui.py bin/ssh-gui servers.example.yaml
git status   # confirm servers.yaml is NOT listed
# then: remote + push when ready
```
