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

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now roomcast.service 2>/dev/null || true
    rm -f "$UNITDIR/roomcast.service" "$BIN/roomcast" "$BIN/roomcastd" \
          "$SHARE/applications/$APPID.desktop" \
          "$SHARE/icons/hicolor/scalable/apps/$APPID.svg"
    rm -rf "$PREFIX"
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

mkdir -p "$SHARE/applications" "$SHARE/icons/hicolor/scalable/apps" "$UNITDIR"
sed "s|@BIN@|$BIN|g" "$SRC/data/$APPID.desktop" > "$SHARE/applications/$APPID.desktop"
cp "$SRC/data/$APPID.svg" "$SHARE/icons/hicolor/scalable/apps/"
sed "s|@BIN@|$BIN|g" "$SRC/data/roomcast.service" > "$UNITDIR/roomcast.service"
update-desktop-database "$SHARE/applications" 2>/dev/null || true
gtk-update-icon-cache -q "$SHARE/icons/hicolor" 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable roomcast.service >/dev/null 2>&1
systemctl --user restart roomcast.service
echo "Installed. The background service is running and starts at login."
echo "Open Roomcast from the app grid, or run: roomcast"
