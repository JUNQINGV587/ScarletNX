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
rm -f "$appdir"/*.log
# The JIT cache a port's play session left beside the program goes in the image: a fresh install then starts with the
# game's code compiled, where a cold one spends its first minutes at 2 frames a second compiling it (96 MB, and the
# runtime drops it by itself when the build, the host or the game's code no longer match). SNX_PORT_KEEP_CACHE=0
# leaves it out, for an image that should not carry a session's cache.
[ "${SNX_PORT_KEEP_CACHE:-1}" = 0 ] && rm -f "$appdir"/*.jitcache
true

# Pin the settings a port needs in the game's .ini. The runtime writes this file back with
# defaults, and a default of English, or of the other face button labels, would undo the
# launcher's arguments. Chinese (Simplified) is language=15.
ini=$appdir/$id.ini
[ -f "$ini" ] || printf '[Game]\n' >"$ini"
# The 3D resolution multiplier (SNX_PORT_RESOLUTION=1.5 renders 1080p out of a 720p game, as
# Frostpunk's does) and the picture keys that cost nothing on a GPU with room to spare
# (SNX_PORT_ANISOTROPY=16, SNX_PORT_ANTI_ALIASING=smaa, SNX_PORT_UPSCALER=fsr1).
resolution=${SNX_PORT_RESOLUTION:-1}
quality_args=""
quality_pins=""
[ -n "${SNX_PORT_ANISOTROPY:-}" ] && { quality_args="$quality_args --anisotropy=$SNX_PORT_ANISOTROPY"; quality_pins="$quality_pins
anisotropy=$SNX_PORT_ANISOTROPY"; }
[ -n "${SNX_PORT_ANTI_ALIASING:-}" ] && { quality_args="$quality_args --anti_aliasing=$SNX_PORT_ANTI_ALIASING"; quality_pins="$quality_pins
anti_aliasing=$SNX_PORT_ANTI_ALIASING"; }
[ -n "${SNX_PORT_UPSCALER:-}" ] && { quality_args="$quality_args --upscaler=$SNX_PORT_UPSCALER"; quality_pins="$quality_pins
upscaler=$SNX_PORT_UPSCALER"; }
pins="language=15
fullscreen=1
gamepad_face_labels=1
gamepad_assign=0
extract=1
emulated_cores=0
touch_in_tv=1
docked=1
resolution=$resolution
screen_fit=0
fps_limit=240$quality_pins"
# A virtual pad that repeats the handheld's own (InputPlumber's, Steam Input's) connects first, and takes player 1
# from it: name the pad player 1 has, as the log names its GUID (SNX_PORT_PAD=0300d628b00d00000119000014030000).
[ -n "${SNX_PORT_PAD:-}" ] && pins="$pins
gamepad_p1_device=$SNX_PORT_PAD"
# A game that holds itself to 30 frames a second sleeps for its own frame: shorten those waits for the port
# (SNX_PORT_FRAME_WAIT=50). Other waits of the game keep that share too, so the port says why.
[ -n "${SNX_PORT_FRAME_WAIT:-}" ] && pins="$pins
frame_wait_percent=$SNX_PORT_FRAME_WAIT"
awk -v pins="$pins" '
BEGIN{n=split(pins,p,"\n"); for(i=1;i<=n;i++){split(p[i],kv,"="); want[kv[1]]=kv[2]; order[i]=kv[1]}}
{ lines[NR]=$0; if ($0=="[Game]" && !game) game=NR }
END{
  for(i=1;i<=NR;i++){ for(j=1;j<=n;j++){ k=order[j]; if (index(lines[i], k "=")==1){ lines[i]=k "=" want[k]; seen[k]=1 } } }
  for(i=1;i<=NR;i++){ print lines[i]; if(i==game) for(j=1;j<=n;j++){ k=order[j]; if(!(k in seen)){ print k "=" want[k]; seen[k]=1 } } }
  for(j=1;j<=n;j++){ k=order[j]; if(!(k in seen)) print k "=" want[k] }
}' "$ini" >"$ini.new" && mv "$ini.new" "$ini"

cat > "$appdir/play-$slug.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
HERE="\$(cd -- "\$(dirname -- "\${BASH_SOURCE[0]}")" && pwd)"
export LD_LIBRARY_PATH="\$HERE/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "\$HERE/$id" --extract-container --fullscreen=1 --resolution=$resolution --docked=1 --fps-limit=240 --gamepad-face-labels=1 --screen-fit=0 --emulated-cores=0 --touch-in-tv=1$quality_args "\$@"
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
