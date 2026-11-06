#!/bin/sh
# Install Roomcast for the current user.
#   ./install.sh            install or update, and start the background service
#   ./install.sh --uninstall
set -eu

SRC="$(cd "$(dirname "$0")" && pwd)"
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$SHARE/roomcast"
BIN="$HOME/.local/bin"
UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPID=io.github.jridder.Roomcast
EXT_UUID=roomcast@jridder.local
EXTDIR="$SHARE/gnome-shell/extensions/$EXT_UUID"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now roomcast.service 2>/dev/null || true
    rm -f "$UNITDIR/roomcast.service" "$BIN/roomcast" "$BIN/roomcastd" \
          "$SHARE/applications/$APPID.desktop" \
          "$SHARE/icons/hicolor/scalable/apps/$APPID.svg" \
          "$SHARE/icons/hicolor/symbolic/apps/$APPID-symbolic.svg" \
          "$SHARE/metainfo/$APPID.metainfo.xml"
    gnome-extensions disable "$EXT_UUID" 2>/dev/null || true
    rm -rf "$PREFIX" "$EXTDIR"
    systemctl --user daemon-reload
    echo "Roomcast removed. Settings are kept in ~/.config/roomcast."
    exit 0
fi

# ----- prerequisites ---------------------------------------------------

missing=""
need() { missing="$missing $1"; }

for tool in pipewire pw-dump pw-cli pw-link pw-metadata; do
    command -v "$tool" >/dev/null || { need pipewire-tools; break; }
done
command -v wpctl >/dev/null || need wireplumber
raop=""
for f in /usr/lib/*/pipewire-0.3/libpipewire-module-raop-sink.so \
         /usr/lib*/pipewire-0.3/libpipewire-module-raop-sink.so; do
    [ -e "$f" ] && raop=1
done
[ -n "$raop" ] || need pipewire-airplay-module
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || need python3.10
python3 -c 'import venv, ensurepip' 2>/dev/null || need python3-venv
python3 - <<'PY' 2>/dev/null || need gtk4-libadwaita
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
raise SystemExit(0 if (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6) else 1)
PY

if [ -n "$missing" ]; then
    echo "Roomcast needs some packages first:$missing"
    . /etc/os-release 2>/dev/null || true
    case " ${ID:-} ${ID_LIKE:-} " in
        *" debian "*|*" ubuntu "*)
            echo "Install them with:"
            echo "  sudo apt install pipewire-bin libpipewire-0.3-modules wireplumber python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-venv" ;;
        *" fedora "*)
            echo "Install them with:"
            echo "  sudo dnf install pipewire pipewire-utils wireplumber python3-gobject gtk4 libadwaita" ;;
        *" arch "*)
            echo "Install them with:"
            echo "  sudo pacman -S pipewire wireplumber python-gobject gtk4 libadwaita" ;;
    esac
    echo "GTK 4 with libadwaita 1.6 or newer and Python 3.10 or newer are required."
    exit 1
fi

echo "Setting up Python environment in $PREFIX/venv"
mkdir -p "$PREFIX"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv --system-site-packages "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade -r "$SRC/requirements.txt"

rm -rf "$PREFIX/lib"
mkdir -p "$PREFIX/lib"
cp -r "$SRC/roomcast" "$PREFIX/lib/"
find "$PREFIX/lib" -name __pycache__ -prune -exec rm -rf {} +

mkdir -p "$BIN"
for pair in "roomcast:roomcast" "roomcastd:roomcast.daemon"; do
    name="${pair%%:*}"; module="${pair#*:}"
    cat > "$BIN/$name" <<EOF
#!/bin/sh
export PYTHONPATH="$PREFIX/lib"
exec "$PREFIX/venv/bin/python" -m $module "\$@"
EOF
    chmod +x "$BIN/$name"
done

mkdir -p "$SHARE/applications" "$SHARE/icons/hicolor/scalable/apps" \
         "$SHARE/icons/hicolor/symbolic/apps" "$UNITDIR"
sed "s|@BIN@|$BIN|g" "$SRC/data/$APPID.desktop" > "$SHARE/applications/$APPID.desktop"
cp "$SRC/data/$APPID.svg" "$SHARE/icons/hicolor/scalable/apps/"
cp "$SRC/data/$APPID-symbolic.svg" "$SHARE/icons/hicolor/symbolic/apps/"
mkdir -p "$SHARE/metainfo"
cp "$SRC/data/$APPID.metainfo.xml" "$SHARE/metainfo/"
sed "s|@BIN@|$BIN|g" "$SRC/data/roomcast.service" > "$UNITDIR/roomcast.service"
update-desktop-database "$SHARE/applications" 2>/dev/null || true
gtk-update-icon-cache -q "$SHARE/icons/hicolor" 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable roomcast.service >/dev/null 2>&1
systemctl --user restart roomcast.service
# Top-panel menu (GNOME Shell extension)
if command -v gnome-shell >/dev/null; then
    rm -rf "$EXTDIR"
    mkdir -p "$EXTDIR"
    cp "$SRC/shell-extension/$EXT_UUID/"* "$EXTDIR/"
    # Add it to GNOME's enabled list so it turns on at the next login even if
    # the running shell doesn't know about it yet (new extensions on Wayland).
    python3 - "$EXT_UUID" <<'PY'
import ast, subprocess, sys
uuid = sys.argv[1]
cur = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"],
                     capture_output=True, text=True).stdout.strip()
cur = cur.removeprefix("@as ")
items = ast.literal_eval(cur) if cur.startswith("[") else []
if uuid not in items:
    items.append(uuid)
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(items)])
PY
    if gnome-extensions enable "$EXT_UUID" 2>/dev/null &&
       gnome-extensions info "$EXT_UUID" 2>/dev/null | grep -q "State: ACTIVE"; then
        echo "Top-panel menu is on."
    else
        echo "Top-panel menu installed. It appears after you log out and back in."
    fi
fi

echo "Installed. The background service is running and starts at login."
echo "Open Roomcast from the app grid, or run: roomcast"
