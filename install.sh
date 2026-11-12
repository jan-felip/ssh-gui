#!/usr/bin/env bash
# Instal·la/actualitza el venv i enllaça ssh-gui a ~/.local/bin
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
mkdir -p "$BIN_DIR"

if [[ ! -d "$ROOT/.venv" ]]; then
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"

chmod +x "$ROOT/bin/ssh-gui" "$ROOT/ssh_gui.py" "$ROOT/install.sh"
ln -sfn "$ROOT/bin/ssh-gui" "$BIN_DIR/ssh-gui"

if [[ ! -f "$ROOT/servers.yaml" ]]; then
  cp "$ROOT/servers.example.yaml" "$ROOT/servers.yaml"
  chmod 600 "$ROOT/servers.yaml"
  echo "Creat $ROOT/servers.yaml (des de l'exemple). Edita'l amb les teves dades."
else
  chmod 600 "$ROOT/servers.yaml" 2>/dev/null || true
fi

echo "OK: $BIN_DIR/ssh-gui → $ROOT/bin/ssh-gui"
if ! command -v ssh-gui >/dev/null 2>&1; then
  echo
  echo "PATH: afegeix això al ~/.zshrc si cal (ja sol estar-hi):"
  echo '  export PATH="$HOME/.local/bin:$PATH"'
  echo "Després: source ~/.zshrc"
else
  echo "Comanda disponible: $(command -v ssh-gui)"
fi

if ! command -v sshpass >/dev/null 2>&1; then
  echo
  echo "Opcional (contrasenyes YAML): brew install hudochenkov/sshpass/sshpass"
fi
