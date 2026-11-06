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
          "$SHARE/icons/hicolor/symbolic/apps/$APPID-symbolic.svg"
    gnome-extensions disable "$EXT_UUID" 2>/dev/null || true
    rm -rf "$PREFIX" "$EXTDIR"
    systemctl --user daemon-reload
    echo "Roomcast removed. Settings are kept in ~/.config/roomcast."
    exit 0
fi

for tool in pipewire pw-dump pw-cli pw-link pw-metadata wpctl; do
    command -v "$tool" >/dev/null || { echo "Missing $tool (PipeWire tools)"; exit 1; }
done
[ -e /usr/lib/x86_64-linux-gnu/pipewire-0.3/libpipewire-module-raop-discover.so ] ||
    ls /usr/lib*/pipewire-0.3/libpipewire-module-raop-discover.so >/dev/null 2>&1 ||
    ls /usr/lib/*/pipewire-0.3/libpipewire-module-raop-discover.so >/dev/null 2>&1 ||
    echo "Warning: PipeWire's RAOP (AirPlay) module wasn't found; speakers won't be discovered."
python3 -c 'import gi; gi.require_version("Adw","1"); from gi.repository import Adw' 2>/dev/null ||
    { echo "Missing GTK 4 / libadwaita Python bindings (python3-gi, gir1.2-adw-1)"; exit 1; }

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
