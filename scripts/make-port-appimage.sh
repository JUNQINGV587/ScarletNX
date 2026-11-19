#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright 2026 the ScarletNX contributors
# Makes <GAME>.AppImage from an installed port folder. The AppImage carries the runtime, the game module, the game's
# extracted folder, every add-on's romfs and the game's icon. The first run installs the payload beside
# ~/Applications/ScarletNX/ and starts the game from there; later runs start the installed copy.
#
# The folder must hold <GAME>, <GAME>_code.so, <GAME>_data, one <GAME>_<TITLE ID>_data for each add-on, and
# <GAME>.ini with its addons= line. It must not hold source= lines.
#
# Usage: scripts/make-port-appimage.sh GAME PORTDIR RUNTIME
# RUNTIME is an AppImage type-2 runtime, from https://github.com/AppImage/type2-runtime/releases.
set -euo pipefail

game=${1:-}
port=${2:-}
runtime=${3:-}
if [ -z "$game" ] || [ -z "$port" ] || [ -z "$runtime" ]; then
    echo "usage: $0 GAME PORTDIR RUNTIME" >&2
    exit 1
fi
for tool in mksquashfs cat; do
    command -v "$tool" >/dev/null || { echo "$0: $tool is not on PATH" >&2; exit 1; }
done
[ -e "$port/$game" ] || { echo "$0: $port has no $game" >&2; exit 1; }
[ -e "$port/${game}_code.so" ] || { echo "$0: $port has no ${game}_code.so" >&2; exit 1; }
[ -e "$port/${game}_data" ] || { echo "$0: $port has no ${game}_data" >&2; exit 1; }
if grep -q '^source=' "$port/$game.ini" 2>/dev/null; then
    echo "$0: $port/$game.ini has a source= line; an installed port reads extracted data only" >&2
    exit 1
fi

out=$PWD/$game.AppImage
work=$(mktemp -d "${TMPDIR:-/tmp}/snx-port-XXXXXX")
trap 'rm -rf "$work"' EXIT
appdir=$work/AppDir
mkdir "$appdir"
# The payload keeps its names: the runtime finds its module by its own name.
for entry in "$port"/*; do
    name=$(basename "$entry")
    cp -al "$entry" "$appdir/$name" 2>/dev/null || cp -a --reflink=auto "$entry" "$appdir/$name"
done

cat > "$appdir/play-$game.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
HERE="\$(cd -- "\$(dirname -- "\${BASH_SOURCE[0]}")" && pwd)"
export LD_LIBRARY_PATH="\$HERE/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "\$HERE/$game" --fullscreen=1 --resolution=3 --docked=1 --fps-limit=240 --gamepad-face-labels=1 --screen-fit=0 --emulated-cores=0 --touch-in-tv=1 "\$@"
EOF
chmod +x "$appdir/play-$game.sh"

cat > "$appdir/AppRun" <<EOF
#!/bin/bash
set -euo pipefail
DEST="\$HOME/Applications/ScarletNX/$game"
if [ ! -f "\$DEST/.installed" ]; then
  echo "Installing $game to \$DEST"
  mkdir -p "\$DEST"
  cp -a "\$APPDIR/." "\$DEST/"
  rm -f "\$DEST/AppRun" "\$DEST/$game.desktop"
  touch "\$DEST/.installed"
  mkdir -p "\$HOME/.local/share/applications"
  printf "[Desktop Entry]\\nType=Application\\nName=$game\\nExec=%s/play-$game.sh\\nIcon=%s/$game.png\\nCategories=Game;\\n" "\$DEST" "\$DEST" > "\$HOME/.local/share/applications/$game.desktop"
  echo "Installed."
  if [ "\${SNX_INSTALL_ONLY:-0}" = 1 ]; then exit 0; fi
fi
exec "\$DEST/play-$game.sh" "\$@"
EOF
chmod +x "$appdir/AppRun"

printf "[Desktop Entry]\nType=Application\nName=%s\nExec=%s\nIcon=%s\nCategories=Game;\n" "$game" "$game" "$game" \
    > "$appdir/$game.desktop"
# The icon that the installed desktop entry shows comes from the game's own data.
if [ -e "$port/${game}_data/icon.jpg" ]; then
    cp "$port/${game}_data/icon.jpg" "$appdir/$game.png"
fi

rm -f "$work/$game.squashfs" "$out"
mksquashfs "$appdir" "$work/$game.squashfs" -comp zstd -Xcompression-level 3 -b 1M -noappend -all-root \
    -no-xattrs -no-progress >/dev/null
cat "$runtime" "$work/$game.squashfs" > "$out"
chmod +x "$out"
echo "$out"
