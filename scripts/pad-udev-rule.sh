#!/usr/bin/env bash
# Hide a handheld's built-in pad from InputPlumber.
#
# InputPlumber matches the built-in pad on its evdev joystick property and then
# grabs the node for itself. Some versions (0.84.0 on Bazzite, seen on a Claw A8)
# never write an event to the virtual pad they publish, so a game that reads the
# pad through SDL gets a controller that reports no button at all. Clearing
# ID_INPUT_JOYSTICK for that one node keeps InputPlumber's matching away from it
# while SDL, which does not need the property, reads the node directly.
#
# Run as root on the handheld:  ./pad-udev-rule.sh [VENDOR PRODUCT PHYS]
# Without arguments the script picks the built-in pad itself: an evdev joystick
# on a USB path whose name is not one of InputPlumber's own devices.

set -euo pipefail

rule=/etc/udev/rules.d/99-scarletnx-pad.rules

if [ "$(id -u)" != 0 ]; then
    echo "pad-udev-rule: run me as root" >&2
    exit 1
fi

if [ $# -eq 3 ]; then
    vendor=$1
    product=$2
    phys=$3
else
    fields=$(python3 - <<'PY'
import evdev

for path in sorted(evdev.list_devices()):
    try:
        dev = evdev.InputDevice(path)
    except OSError:
        continue
    if "InputPlumber" in dev.name or not dev.phys:
        continue
    caps = dev.capabilities()
    if evdev.ecodes.EV_KEY not in caps or evdev.ecodes.EV_ABS not in caps:
        continue
    if len(caps[evdev.ecodes.EV_KEY]) < 8:
        continue
    info = dev.info
    print("%04x %04x %s" % (info.vendor, info.product, dev.phys))
    break
PY
)
    if [ -z "$fields" ]; then
        echo "pad-udev-rule: found no built-in pad; give VENDOR PRODUCT PHYS" >&2
        exit 1
    fi
    read -r vendor product phys <<<"$fields"
fi

echo "pad-udev-rule: hiding $vendor:$product at $phys from InputPlumber"
cat >"$rule" <<EOF
# Written by scripts/pad-udev-rule.sh: keep InputPlumber's hands off the built-in
# pad, which it grabs and, on some versions, never forwards.
KERNEL=="event*", SUBSYSTEM=="input", ATTRS{id/vendor}=="$vendor", ATTRS{id/product}=="$product", ATTRS{phys}=="$phys", ENV{ID_INPUT_JOYSTICK}=""
EOF
udevadm control --reload
udevadm trigger --subsystem-match=input
echo "pad-udev-rule: installed $rule"
