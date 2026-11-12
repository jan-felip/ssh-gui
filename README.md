# ssh-gui

Menú TUI al terminal per connectar ràpidament per SSH a servidors definits en un YAML local.

## Requisits

- macOS amb Python 3 (ve de sèrie)
- `ssh` (OpenSSH)
- Opcional: [sshpass](https://formulae.brew.sh/) per injectar la contrasenya del YAML  
  `brew install hudochenkov/sshpass/sshpass`
- Preferible: claus SSH (`identity`) en lloc de `pass`

## Instal·lació ràpida

```bash
cd ~/ssh-gui
./install.sh
```

Això crea el venv, instal·la PyYAML i enllaça `~/.local/bin/ssh-gui`.

Si `ssh-gui` no es troba:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

## Configuració

1. Copia l'exemple (ja ho fa `install.sh` si no existeix):

```bash
cp servers.example.yaml servers.yaml
chmod 600 servers.yaml
```

2. Edita `servers.yaml` amb els teus servidors:

```yaml
Nom_servidor_que_li_posi_jo:
  host: 10.0.0.5
  user: jan
  pass: "opcional"
  port: 22
  identity: ~/.ssh/id_ed25519   # opcional; si hi és, té prioritat sobre pass
  notes: "text curt al menú"
```

**Important:** `servers.yaml` està al `.gitignore`. No el pujis mai a GitHub. Només `servers.example.yaml` (sense credencials reals).

## Ús

```bash
ssh-gui              # menú TUI (↑↓ + Enter, q surt)
ssh-gui --list       # llista sense menú
ssh-gui --dry-run    # tria un servidor i mostra la comanda sense connectar
ssh-gui --connect Nom_servidor_que_li_posi_jo
ssh-gui -c ~/altre.yaml
```

## Auth

| Situació | Comportament |
|----------|--------------|
| `identity` present | `ssh -i …` |
| `pass` sense identity + `sshpass` instal·lat | `sshpass -p … ssh …` |
| `pass` sense `sshpass` | Avís + SSH demana la contrasenya a mà |
| Cap dels dos | Auth interactiva normal d'SSH |

## Seguretat

- Mantén `servers.yaml` amb `chmod 600`.
- Prefereix claus (`identity`) abans que contrasenyes en clar.
- No commitis credencials; el `.gitignore` ja exclou `servers.yaml` i `.venv/`.
- `sshpass` deixa la contrasenya visible a la línia de comandes del procés: només per xarxes de confiança / ús local.

## Estructura

```
~/ssh-gui/
  ssh_gui.py              # lògica + TUI (curses)
  bin/ssh-gui             # launcher → .venv
  servers.example.yaml    # plantilla segura per al repo
  servers.yaml            # LOCAL, secrets (gitignored)
  requirements.txt
  install.sh
  README.md
  .gitignore
```

## Preparar un repo a GitHub (futur)

```bash
cd ~/ssh-gui
git init
git add .gitignore README.md requirements.txt install.sh ssh_gui.py bin/ssh-gui servers.example.yaml
git status   # comprova que servers.yaml NO surt
# després: remote + push quan vulguis
```
