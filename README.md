# ssh-gui

[![Last commit](https://img.shields.io/github/last-commit/jan-felip/ssh-gui)](https://github.com/jan-felip/ssh-gui/commits/main)
[![License: GPL-3.0](https://img.shields.io/github/license/jan-felip/ssh-gui)](https://github.com/jan-felip/ssh-gui/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.x-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](https://github.com/jan-felip/ssh-gui)

> **A convenient solution to a problem that nobody has.**  
> *Una solució convenient a un problema que ningú no té.*

Menú TUI al terminal per connectar per SSH a hosts d’un inventari YAML local, amb accions de deploy (rsync, git, Drush…).  
Terminal TUI to SSH into hosts from a local YAML inventory, plus deploy helpers (rsync, git, Drush…).

**Author:** [jan-felip](https://github.com/jan-felip)

---

## Català

### Què és

`ssh-gui` obre un menú amb la llista de servidors que hagis desat localment. `Enter` connecta per SSH; `e` edita la config; `→` obre accions (git, rsync, Vendor/Drush). Les contrasenyes del YAML s’escriuen soles a la terminal al entrar.

### Requisits

- Linux o macOS amb Python 3
- `ssh` i `rsync` (OpenSSH)
- Preferible clau SSH (`identity`) en lloc de `pass` *(també funciona si la tens desada a la configuració d'ssh a l'ordinador, no cal especificar-la aquí)*

### Instal·lació

```bash
git clone https://github.com/jan-felip/ssh-gui.git ~/ssh-gui
cd ~/ssh-gui
./install.sh
```

Crea el venv, instal·la PyYAML i enllaça `~/.local/bin/ssh-gui`.

Si no trobes la comanda:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # o ~/.bashrc
source ~/.zshrc
```

### Inventari YAML

```bash
cp servers.example.yaml servers.yaml
chmod 600 servers.yaml
```

Exemple (placeholders):

```yaml
my_server:
  host: 192.0.2.10
  user: deploy
  # pass: "optional"          # millor identity
  identity: ~/.ssh/id_ed25519
  notes: "etiqueta al menú"
  web_remote: /var/www/app
  web_local: ~/Projects/my-app
  # git_user / git_token      # només si cal pull HTTPS al remot
```

`servers.yaml` està al `.gitignore`. Només es pot publicar `servers.example.yaml`.

### Ús

```bash
ssh-gui                 # TUI
ssh-gui --list
ssh-gui --connect NAME
ssh-gui --dry-run
```

#### Llista principal

| Tecla | Acció |
|-------|--------|
| Escriure | Obre el cercador (filtra nom / host / notes) |
| Esc | Tanca/neteja el cercador (no surt de l’app) |
| ↑↓ / j k | Moure |
| Enter | Connectar SSH (o `[+] Add host`) |
| e | **Edit** (config durable: SSH + `web_*` + `git_*`) |
| → | Accions runtime (deploy / git / Vendor) |
| Ctrl+C | Sortir |

#### Accions (`→`)

Només runtime. Si falta config, surten deshabilitades amb pista d’usar Edit.

- **Git:** pull, historial  
- **Deploy:** rsync upload (+ chmod 755/644), download, fix permissions  
- **Vendor** (si existeix `web_remote/vendor`): Drush `cr`, `Database: Update/Export/Import`, `uli`, `cim`, `cex` (fallback Remi PHP + sudo) *(Això existeix ja que habituo a treballar amb webs basades en Drupal)*  
- **MySQL** (si hi ha `web_remote`): `Database: Export/Import` via `mysqldump` / `mysql` (Drush → YAML → prompt; després d’èxit pots desar `mysql_*` al YAML)

Export desa el dump a `web_local/db-dumps/` (fora del docroot remot). Import és destructiu (`sql:drop` / `DROP DATABASE`) amb confirmació `y/N`.

Enter a un host amb `web_remote` obre l’SSH ja dins aquell directori.

### Seguretat

- `chmod 600 servers.yaml`
- Preferir claus a contrasenyes / tokens en text pla
- No commitjar mai `servers.yaml`, `git_token` reals ni dumps `*.sql` / `*.sql.gz`

---

## English

### What it is

`ssh-gui` opens a menu with the list of servers you have saved locally. `Enter` connects over SSH; `e` edits the config; `→` opens actions (git, rsync, Vendor/Drush). Passwords from the YAML are typed into the terminal automatically on login.

### Requirements

- Linux or macOS with Python 3
- `ssh` and `rsync` (OpenSSH)
- Prefer an SSH key (`identity`) over `pass` *(also works if the key is already in your machine’s SSH config — you don’t need to set it here)*

### Install

```bash
git clone https://github.com/jan-felip/ssh-gui.git ~/ssh-gui
cd ~/ssh-gui
./install.sh
```

Creates the venv, installs PyYAML, and symlinks `~/.local/bin/ssh-gui`.

If you can’t find the command:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc   # or ~/.bashrc
source ~/.zshrc
```

### YAML inventory

```bash
cp servers.example.yaml servers.yaml
chmod 600 servers.yaml
```

Example (placeholders):

```yaml
my_server:
  host: 192.0.2.10
  user: deploy
  # pass: "optional"          # prefer identity
  identity: ~/.ssh/id_ed25519
  notes: "menu label"
  web_remote: /var/www/app
  web_local: ~/Projects/my-app
  # git_user / git_token      # only if remote HTTPS pull is needed
```

`servers.yaml` is in `.gitignore`. Only `servers.example.yaml` may be published.

### Usage

```bash
ssh-gui                 # TUI
ssh-gui --list
ssh-gui --connect NAME
ssh-gui --dry-run
```

#### Main list

| Key | Action |
|-----|--------|
| Type | Opens search (filters name / host / notes) |
| Esc | Closes/clears search (does not quit the app) |
| ↑↓ / j k | Move |
| Enter | Connect SSH (or `[+] Add host`) |
| e | **Edit** (durable config: SSH + `web_*` + `git_*`) |
| → | Runtime actions (deploy / git / Vendor) |
| Ctrl+C | Quit |

#### Actions (`→`)

Runtime only. If config is missing, items show disabled with a hint to use Edit.

- **Git:** pull, history  
- **Deploy:** rsync upload (+ chmod 755/644), download, fix permissions  
- **Vendor** (if `web_remote/vendor` exists): Drush `cr`, `Database: Update/Export/Import`, `uli`, `cim`, `cex` (Remi PHP + sudo fallback) *(This exists because I usually work on Drupal-based sites)*  
- **MySQL** (when `web_remote` is set): `Database: Export/Import` via `mysqldump` / `mysql` (Drush → YAML → prompt; after success you can save `mysql_*` to YAML)

Export writes the dump under `web_local/db-dumps/` (remote dump stays out of the docroot). Import is destructive (`sql:drop` / `DROP DATABASE`) with a `y/N` confirm.

Enter on a host with `web_remote` opens SSH already inside that directory.

### Security

- `chmod 600 servers.yaml`
- Prefer keys over plaintext passwords / tokens
- Never commit `servers.yaml`, real `git_token` values, or `*.sql` / `*.sql.gz` dumps

---

## License

[GPL-3.0](LICENSE) · [jan-felip](https://github.com/jan-felip)
