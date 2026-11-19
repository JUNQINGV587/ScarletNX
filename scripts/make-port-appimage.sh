#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the ScarletNX contributors
# Makes "<NAME>.AppImage" from an installed port folder. The AppImage carries the runtime, the game module, the
# game's extracted folder, every add-on's romfs and the game's icon. The first run installs the payload beside
# ~/Applications/ScarletNX/ and starts the game from there; later runs start the installed copy.
#
# The folder must follow the port rules: the program keeps the game's title ID as its name, and so do <TITLE ID>.ini,
# <TITLE ID>_code.so, <TITLE ID>_data and each <TITLE ID>_<ADD ON>_data. A name that is not a title ID makes the
# runtime read no add-on at all (common/utilities/game_ini.c, snx_addons_parse). The folder must not hold a source=
# line in its .ini: an installed port reads extracted data only.
#
# Usage: scripts/make-port-appimage.sh NAME PORTDIR RUNTIME
# NAME is the game's display name, on the AppImage, the launcher and the desktop entry. RUNTIME is an AppImage type-2
# runtime, from https://github.com/AppImage/type2-runtime/releases.
set -euo pipefail

name=${1:-}
port=${2:-}
runtime=${3:-}
if [ -z "$name" ] || [ -z "$port" ] || [ -z "$runtime" ]; then
    echo "usage: $0 NAME PORTDIR RUNTIME" >&2
    exit 1
fi
for tool in mksquashfs cat; do
    command -v "$tool" >/dev/null || { echo "$0: $tool is not on PATH" >&2; exit 1; }
done

id=
for entry in "$port"/*; do
    base=$(basename "$entry")
    case $base in
    [0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F])
        [ -f "$entry" ] && [ -x "$entry" ] && id=$base ;;
    esac
done
[ -n "$id" ] || { echo "$0: $port has no program named after the game's title ID" >&2; exit 1; }
[ -e "$port/${id}_code.so" ] || { echo "$0: $port has no ${id}_code.so" >&2; exit 1; }
[ -e "$port/${id}_data" ] || { echo "$0: $port has no ${id}_data" >&2; exit 1; }
if grep -q '^source=' "$port/$id.ini" 2>/dev/null; then
    echo "$0: $port/$id.ini has a source= line; an installed port reads extracted data only" >&2
    exit 1
fi

slug=$(printf '%s' "$name" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9\n' '-' | sed 's/-\{1,\}/-/g; s/^-//; s/-$//')
out=$PWD/${name// /.}.AppImage
work=$(mktemp -d "${TMPDIR:-/tmp}/snx-port-XXXXXX")
trap 'rm -rf "$work"' EXIT
appdir=$work/AppDir
mkdir "$appdir"
# A reflink copy: a hard link would make the runtime refuse the folder it indexes (a file of two names).
cp -a --reflink=auto "$port/." "$appdir/"
rm -f "$appdir"/*.log "$appdir"/*.jitcache

cat > "$appdir/play-$slug.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
HERE="\$(cd -- "\$(dirname -- "\${BASH_SOURCE[0]}")" && pwd)"
export LD_LIBRARY_PATH="\$HERE/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "\$HERE/$id" --extract-container --fullscreen=1 --resolution=3 --docked=1 --fps-limit=240 --gamepad-face-labels=1 --screen-fit=0 --emulated-cores=0 --touch-in-tv=1 "\$@"
EOF
chmod +x "$appdir/play-$slug.sh"

# The port's lib/ holds the libraries the host lacks (z3, libedit): they stay in the image and in the install.
cat > "$appdir/AppRun" <<EOF
#!/bin/bash
set -euo pipefail
DEST="\$HOME/Applications/ScarletNX/$name"
if [ ! -f "\$DEST/.installed" ]; then
  echo "Installing $name to \$DEST"
  mkdir -p "\$DEST"
  cp -a "\$APPDIR/." "\$DEST/"
  rm -f "\$DEST/AppRun" "\$DEST/${name// /.}.desktop"
  touch "\$DEST/.installed"
  mkdir -p "\$HOME/.local/share/applications"
  printf "[Desktop Entry]\\nType=Application\\nName=$name\\nExec=%s/play-$slug.sh\\nIcon=%s/%s.png\\nCategories=Game;\\n" "\$DEST" "\$DEST" "${name// /.}" > "\$HOME/.local/share/applications/${name// /.}.desktop"
  echo "Installed."
  if [ "\${SNX_INSTALL_ONLY:-0}" = 1 ]; then exit 0; fi
fi
exec "\$DEST/play-$slug.sh" "\$@"
EOF
chmod +x "$appdir/AppRun"

printf "[Desktop Entry]\nType=Application\nName=%s\nExec=play-%s.sh\nIcon=%s.png\nCategories=Game;\n" \
    "$name" "$slug" "${name// /.}" > "$appdir/${name// /.}.desktop"
# The icon that the installed desktop entry shows comes from the game's own data.
if [ -e "$port/${id}_data/icon.jpg" ]; then
    cp "$port/${id}_data/icon.jpg" "$appdir/${name// /.}.png"
fi

rm -f "$work/$slug.AppImage" "$out"
mksquashfs "$appdir" "$work/$slug.squashfs" -comp zstd -Xcompression-level 3 -b 1M -noappend -all-root \
    -no-xattrs -no-progress >/dev/null
cat "$runtime" "$work/$slug.squashfs" > "$out"
chmod +x "$out"
echo "$out"
