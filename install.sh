#!/usr/bin/env bash
# Installe Coup d'œil pour l'utilisateur courant (aucun droit root nécessaire).
#   ./install.sh           → appli + icône + lanceur dans le menu
#   ./install.sh --favori  → et l'ajoute aux favoris du menu KDE
set -euo pipefail

src="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
dest="$data/coup-doeil"

python3 -c "import PySide6" 2>/dev/null || {
    echo "PySide6 est requis (Arch / CachyOS : sudo pacman -S pyside6)." >&2
    exit 1
}

install -Dm755 "$src/coup_doeil.py"    "$dest/coup_doeil.py"
install -Dm644 "$src/coup-doeil.svg"   "$dest/coup-doeil.svg"
install -Dm644 "$src/coup-doeil.svg"   "$data/icons/hicolor/scalable/apps/coup-doeil.svg"
sed "s|@DEST@|$dest|" "$src/coup-doeil.desktop" > "$data/applications/coup-doeil.desktop"
chmod 644 "$data/applications/coup-doeil.desktop"

command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null && update-desktop-database "$data/applications" 2>/dev/null || true

if [[ "${1:-}" == "--favori" ]] && command -v qdbus6 >/dev/null; then
    qdbus6 org.kde.ActivityManager /ActivityManager/Resources/Linking \
        org.kde.ActivityManager.ResourcesLinking.LinkResourceToActivity \
        org.kde.plasma.favorites.applications applications:coup-doeil.desktop :global \
        && echo "Ajouté aux favoris du menu KDE."
fi

echo "Coup d'œil installé dans $dest — lance-le depuis le menu."
