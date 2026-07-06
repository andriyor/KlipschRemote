#!/usr/bin/env bash
#
# Builds the Linux KlipschRemote release with PyInstaller and packages it as a
# .deb — an installable Debian/Ubuntu package that drops the app into the system
# applications menu (its own icon + launcher), the Linux counterpart of the
# Windows Inno Setup installer (build_app.ps1 + installer.iss).
#
# WHY NOT `flet build linux`?
#   `flet build linux` embeds a standalone CPython (from serious_python) that is
#   compiled in a minimal manylinux image WITHOUT the BlueZ headers, so its
#   `socket` module has no AF_BLUETOOTH / BTPROTO_L2CAP. On Linux the speaker
#   only answers ATT over the Classic (BR/EDR) transport, so the app's ONLY path
#   is the raw-L2CAP-socket backend (klipsch_ble/l2cap_backend.py) — which needs
#   AF_BLUETOOTH. The result: a `flet build` release throws
#       "this Python build has no Linux Bluetooth L2CAP support"
#   and cannot connect at all.
#
#   PyInstaller freezes with the CURRENT interpreter instead. Distro CPython
#   (Debian/Ubuntu, built against libbluetooth-dev) HAS AF_BLUETOOTH, so the
#   frozen app keeps it. The flet PyInstaller hook also ships the desktop client
#   inside the bundle; main.py points FLET_VIEW_PATH at it so the app is fully
#   offline (no first-run download).
#
# Prerequisites on the BUILD machine (must be Debian/Ubuntu-family Linux):
#   - A distro CPython WITH Bluetooth support (the guard below hard-fails otherwise)
#   - pip install -r requirements.txt pyinstaller
#   - dpkg-deb (from the `dpkg` package — preinstalled on Debian/Ubuntu)
#
# Usage (from this folder):
#   ./build_app_linux.sh
#   VERSION=0.2.0 ./build_app_linux.sh      # override the package version
# Output:
#   dist_app/KlipschRemote/                       (the raw PyInstaller bundle)
#   dist_installer/klipsch-remote_<ver>_amd64.deb (installable package, ship this)
#
# Install / remove it with:
#   sudo apt install ./dist_installer/klipsch-remote_<ver>_amd64.deb
#   sudo apt remove klipsch-remote

set -euo pipefail

ScriptDir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ScriptDir"

PYTHON="${PYTHON:-python3}"
ARTIFACT="KlipschRemote"     # frozen bundle / install-dir name
PKG="klipsch-remote"         # Debian package name (lowercase, dpkg convention)
ARCH="amd64"
InstallRoot="/opt/$ARTIFACT" # where the bundle lands on the target system

# Package version: default to the one in packaging/pyproject.toml; CI overrides it
# from the release tag via the VERSION env var.
VERSION="${VERSION:-$(grep -m1 -E '^version' packaging/pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')}"
if [ -z "$VERSION" ]; then
    echo "ERROR: could not determine a version (set VERSION=x.y.z)." >&2
    exit 1
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# --- Guard: the interpreter MUST expose Bluetooth L2CAP, or the release is dead
# on arrival. Fail loudly here instead of shipping a binary that throws at connect.
echo "==> Verifying the build interpreter has Bluetooth L2CAP support..."
"$PYTHON" - <<'PY'
import socket, sys
ok = hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_L2CAP")
if not ok:
    sys.exit(
        "FATAL: this Python has no AF_BLUETOOTH/BTPROTO_L2CAP — the frozen app "
        "would be unable to connect. Use a distro CPython built against "
        "libbluetooth-dev (Debian/Ubuntu apt python3)."
    )
print("    OK:", sys.version.split()[0], "has AF_BLUETOOTH + BTPROTO_L2CAP")
PY

command -v dpkg-deb >/dev/null 2>&1 || {
    echo "ERROR: dpkg-deb not found. Install it with: sudo apt-get install -y dpkg" >&2
    exit 1
}

# --- Stage a clean tree (source stays single-sourced) -------------------------
Stage="$ScriptDir/.build_app_linux"
rm -rf "$Stage"
mkdir -p "$Stage"

# The flet PyInstaller hook ships the desktop client from the local flet cache
# (~/.flet/client/...). Warm it once so the frozen bundle is self-contained; if
# the cache is cold, importing flet_desktop and calling ensure_client_cached()
# downloads it. Do it explicitly so a cold CI runner still bundles the client.
echo "==> Ensuring the flet desktop client is cached (for offline bundling)..."
"$PYTHON" - <<'PY'
from flet_desktop import ensure_client_cached
print("    client:", ensure_client_cached())
PY

# --- Freeze with PyInstaller --------------------------------------------------
echo "==> Running PyInstaller..."
"$PYTHON" -m PyInstaller \
    --noconfirm --clean --log-level WARN \
    --name "$ARTIFACT" \
    --windowed \
    --paths "$ScriptDir" \
    --additional-hooks-dir "$("$PYTHON" -c 'import flet_cli.__pyinstaller as p, os; print(os.path.dirname(p.__file__))')" \
    --add-data "$ScriptDir/klipsch_remote/assets:klipsch_remote/assets" \
    --collect-submodules bleak \
    --collect-submodules flet \
    --collect-all dbus_fast \
    --hidden-import bleak.backends.bluezdbus.client \
    --distpath "$Stage/dist" --workpath "$Stage/build" --specpath "$Stage" \
    packaging/main.py

Bundle="$Stage/dist/$ARTIFACT"
ExePath="$Bundle/$ARTIFACT"
if [ ! -x "$ExePath" ]; then
    echo "ERROR: PyInstaller reported success but $ExePath is missing." >&2
    exit 1
fi

# Sanity: the desktop client must be inside the bundle (offline requirement).
if [ ! -f "$Bundle/_internal/flet_desktop/app/flet/flet" ]; then
    echo "ERROR: flet desktop client not bundled (expected _internal/flet_desktop/app/flet/flet)." >&2
    echo "       The flet cache was probably cold — check the ensure_client_cached step." >&2
    exit 1
fi
echo "==> Bundle: $Bundle"

DistApp="$ScriptDir/dist_app/$ARTIFACT"
rm -rf "$DistApp"; mkdir -p "$DistApp"
cp -a "$Bundle/." "$DistApp/"
echo "==> Raw bundle: $DistApp"

# --- Package the .deb (installs into the applications menu) --------------------
echo "==> Building .deb package..."
DebRoot="$Stage/deb"
rm -rf "$DebRoot"
mkdir -p "$DebRoot$InstallRoot" \
         "$DebRoot/usr/bin" \
         "$DebRoot/usr/share/applications" \
         "$DebRoot/usr/share/icons/hicolor/512x512/apps" \
         "$DebRoot/usr/share/pixmaps" \
         "$DebRoot/DEBIAN"

# The whole frozen bundle goes under /opt (self-contained, incl. the flet client).
cp -a "$Bundle/." "$DebRoot$InstallRoot/"

# Launcher on PATH. FLET_HIDE_WINDOW_ON_START keeps the window hidden during the
# ~2 s Python boot; the app reveals it centred once ready (no empty-window flash).
# FLET_VIEW_PATH is set by packaging/main.py from the bundle, so no host download.
cat > "$DebRoot/usr/bin/$PKG" <<EOF
#!/bin/sh
export FLET_HIDE_WINDOW_ON_START=true
exec "$InstallRoot/$ARTIFACT" "\$@"
EOF
chmod 755 "$DebRoot/usr/bin/$PKG"

# Icon (ship OUR art, not Klipsch's trademarked logo). The 1024px source is fine
# in the 512x512 bucket — the icon theme scales it; also drop a /usr/share/pixmaps
# copy as a universal fallback for desktops that don't read the hicolor theme.
cp "$ScriptDir/klipsch_remote/assets/icon.png" "$DebRoot/usr/share/icons/hicolor/512x512/apps/$PKG.png"
cp "$ScriptDir/klipsch_remote/assets/icon.png" "$DebRoot/usr/share/pixmaps/$PKG.png"

# Desktop entry — this is what makes the app appear in the applications menu.
# StartupWMClass MUST match the window's WM_CLASS so the shell groups the running
# window under THIS launcher (its icon), instead of a second, generic entry. The
# window is the prebuilt flet desktop client, whose WM_CLASS is "flet"/"Flet"
# (not the package name) — verified with xprop. GNOME matches StartupWMClass
# against both the instance ("flet") and class ("Flet"), so the instance is enough.
cat > "$DebRoot/usr/share/applications/$PKG.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Klipsch Remote
Comment=Unofficial desktop remote for Klipsch powered speakers
Exec=$PKG
Icon=$PKG
StartupWMClass=flet
Categories=AudioVideo;Audio;
Keywords=Klipsch;Speaker;Bluetooth;Remote;Volume;
Terminal=false
EOF

InstalledKb=$(du -sk "$DebRoot$InstallRoot" | cut -f1)
cat > "$DebRoot/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: sound
Priority: optional
Architecture: $ARCH
Depends: libc6, bluez, libgtk-3-0
Installed-Size: $InstalledKb
Maintainer: Unofficial <noreply@users.noreply.github.com>
Description: Unofficial desktop remote for Klipsch powered speakers
 Control Klipsch The Fives / Sevens / Nines powered speakers over Bluetooth:
 volume, input source, EQ and more, from a native desktop app.
 .
 Not affiliated with or endorsed by Klipsch.
EOF

# Refresh the menu + icon cache on install/remove so the entry appears (and goes)
# immediately. Both tools are best-effort — never fail the package over them.
cat > "$DebRoot/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
EOF
cat > "$DebRoot/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
fi
exit 0
EOF
chmod 755 "$DebRoot/DEBIAN/postinst" "$DebRoot/DEBIAN/postrm"

DistInstaller="$ScriptDir/dist_installer"
mkdir -p "$DistInstaller"
Out="$DistInstaller/${PKG}_${VERSION}_${ARCH}.deb"
rm -f "$Out"

# --root-owner-group: files land as root:root without needing fakeroot/root.
dpkg-deb --root-owner-group --build "$DebRoot" "$Out"

mb=$(du -sm "$Out" | cut -f1)
echo ""
echo "==> Done. $Out (~${mb} MB)"
echo "    Install with: sudo apt install $Out"
echo "    Then launch \"Klipsch Remote\" from the applications menu, or run \`$PKG\`."
echo "    Raw PyInstaller bundle also in: $DistApp"
