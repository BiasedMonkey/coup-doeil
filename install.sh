#!/usr/bin/env bash
# Installe Coup d'œil pour l'utilisateur courant (aucun droit root nécessaire).
#   ./install.sh           → appli + icône + lanceur dans le menu
#   ./install.sh --favori  → et l'ajoute aux favoris du menu KDE
#   ./install.sh --coin    → les coins supérieurs gauche et droit de l'écran affichent / masquent Coup d'œil
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

for opt in "$@"; do
    case "$opt" in
        --favori)
            command -v qdbus6 >/dev/null && qdbus6 org.kde.ActivityManager /ActivityManager/Resources/Linking \
                org.kde.ActivityManager.ResourcesLinking.LinkResourceToActivity \
                org.kde.plasma.favorites.applications applications:coup-doeil.desktop :global \
                && echo "Ajouté aux favoris du menu KDE." ;;
        --demarrage)
            # lancement à l'ouverture de session (KDE / GNOME / XFCE…), une fois le bureau prêt
            mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
            cat > "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/coup-doeil.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Coup d'œil
Comment=Moniteur système (lancé à l'ouverture de session)
Exec=$(command -v python3) "$dest/coup_doeil.py"
Icon=coup-doeil
Terminal=false
X-KDE-autostart-phase=2
X-GNOME-Autostart-enabled=true
EOF
            echo "Lancé à l'ouverture de session." ;;
        --coin)
            # script KWin : les coins supérieurs gauche et droit affichent / masquent Coup d'œil
            rm -rf "$data/kwin/scripts/coup-doeil-coin"
            mkdir -p "$data/kwin/scripts"
            cp -r "$src/kwin/coup-doeil-coin" "$data/kwin/scripts/"
            kwriteconfig6 --file kwinrc --group Plugins --key coup-doeil-coinEnabled true
            # décharger l'ancienne version, sinon KWin garde celle déjà en mémoire
            qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript coup-doeil-coin >/dev/null 2>&1 || true
            # charger seulement ce script : un « reconfigure » complet de KWin peut
            # laisser un fond gris sur les fenêtres X11 non gérées déjà ouvertes
            qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript \
                "$data/kwin/scripts/coup-doeil-coin/contents/code/main.js" coup-doeil-coin >/dev/null 2>&1 || true
            qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.start >/dev/null 2>&1 || true
            echo "Coins supérieurs gauche et droit : affichent / masquent Coup d'œil (script KWin activé)." ;;
    esac
done

echo "Coup d'œil installé dans $dest — lance-le depuis le menu."
