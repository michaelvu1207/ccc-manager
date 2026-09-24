#!/bin/sh
set -eu

configuration=${1:-Release}
action=${2:-build}
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

cd "$root/macapp"
command -v xcodegen >/dev/null 2>&1 || {
  echo "xcodegen is required (brew install xcodegen)" >&2
  exit 127
}
xcodegen generate

# Xcode's PNG copier rejects the app runner's C.UTF-8 locale on some Macs.
env -u LC_ALL -u LC_CTYPE LANG=en_US.UTF-8 \
  xcodebuild \
  -project CCC.xcodeproj \
  -scheme CCC \
  -configuration "$configuration" \
  -derivedDataPath build \
  CODE_SIGNING_ALLOWED=NO \
  "$action"

# The build above is unsigned (CODE_SIGNING_ALLOWED=NO). Ad-hoc sign the
# product so `codesign --verify --deep --strict` passes and Gatekeeper-less
# local launches work.
if [ "$action" = "build" ]; then
  app="$root/macapp/build/Build/Products/$configuration/CCC.app"
  codesign --force --deep --sign - "$app"
  codesign --verify --deep --strict --verbose=1 "$app"
fi
