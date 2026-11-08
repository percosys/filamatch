#!/usr/bin/env bash
# Install or update Filamatch in the macOS Fusion add-ins directory.
#
# Usage:
#   scripts/install_addin_macos.sh            # copy/update the add-in
#   scripts/install_addin_macos.sh --dev      # link the add-in to this repository
#   scripts/install_addin_macos.sh --dry-run  # show what would happen
#
# --dev makes Fusion's add-in folder a link to the repository's Filamatch
# folder: after a code change, stop and run the add-in in Fusion (Shift+S);
# no reinstall. Running without --dev turns a link back into a copy.
# FILAMATCH_ADDINS_DIR replaces Fusion's AddIns folder (for tests) and
# FILAMATCH_TRASH_DIR the Trash.
#
# Current Autodesk Fusion releases (2025/2026+, version-numbered builds) load
# Python add-ins from:
#   ~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/
# and expect a JSON .manifest file paired with a .py entry point of the same
# basename. This script validates the manifest and mirrors the add-in folder
# there. Fusion must be restarted (or the add-in re-run from
# Utilities > ADD-INS > Scripts and Add-Ins) before changes are picked up.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ADDIN_SRC="$REPO_ROOT/Filamatch"
ADDINS_DIR="${FILAMATCH_ADDINS_DIR:-$HOME/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns}"
TRASH_DIR="${FILAMATCH_TRASH_DIR:-$HOME/.Trash}"
ADDIN_DST="$ADDINS_DIR/Filamatch"
# The add-in was called FusionFilament Bridge before 0.2.0. Fusion would load
# both copies, so the old one is moved to the Trash.
LEGACY_DST="$ADDINS_DIR/FusionFilamentBridge"
DRY_RUN=""
DEV=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --dev) DEV=1 ;;
    --copy) DEV="" ;;
    -h|--help)
      sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "error: unknown argument: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

echo "Source add-in: $ADDIN_SRC"
echo "Fusion AddIns: $ADDIN_DST"

if [[ ! -d "$ADDIN_SRC" ]]; then
  echo "error: add-in source folder not found: $ADDIN_SRC" >&2
  exit 1
fi
for required in Filamatch.py Filamatch.manifest __init__.py config.py AddInIcon.svg commands/__init__.py commands/open_palette/entry.py commands/open_palette/resources/16x16.png commands/open_palette/resources/32x32.png commands/open_palette/resources/64x64.png lib/__init__.py lib/palette_bridge.py lib/catalog.py lib/threemf_injector.py lib/exporter.py data/filaments.json palette/index.html palette/style.css palette/app.js; do
  if [[ ! -f "$ADDIN_SRC/$required" ]]; then
    echo "error: required add-in file missing: $ADDIN_SRC/$required" >&2
    exit 1
  fi
done

# Validate the JSON manifest before touching the installed copy. Fusion silently
# ignores add-ins whose manifest is missing/malformed (e.g. a legacy XML one).
python3 - "$ADDIN_SRC/Filamatch.manifest" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as handle:
    raw = handle.read()
if raw.lstrip().startswith("<"):
    sys.exit(
        "error: manifest is XML. Current Autodesk Fusion requires a JSON manifest "
        "for Python add-ins: " + path
    )
data = json.loads(raw)
required = {
    "autodeskProduct": str,
    "type": str,
    "description": dict,
    "version": str,
    "runOnStartup": bool,
    "supportedOS": str,
    "editEnabled": bool,
}
missing = [key for key, kind in required.items() if key not in data or not isinstance(data[key], kind)]
if missing:
    sys.exit("error: manifest is missing/wrong-typed fields: " + ", ".join(missing))
if data["type"] != "addin":
    sys.exit("error: manifest type must be 'addin', got " + repr(data["type"]))
if not data["description"].get(""):
    sys.exit("error: manifest description must contain a non-empty \"\" entry")
print("Manifest OK: {} {} ({})".format(data["autodeskProduct"], data["version"], data["type"]))
PY

if [[ -n "$DRY_RUN" && -n "$DEV" ]]; then
  echo "Dry run: $ADDIN_DST would become a link to $ADDIN_SRC"
  exit 0
fi
if [[ -n "$DRY_RUN" ]]; then
  echo "Dry run: files that would be installed:"
  find "$ADDIN_SRC" -type f ! -name ".DS_Store" ! -path "*/__pycache__/*" ! -name "*.pyc" -print | sed "s#^$ADDIN_SRC/#  #"
  if [[ -d "$LEGACY_DST" ]]; then
    echo "Dry run: the old add-in would move to the Trash: $LEGACY_DST"
  fi
  exit 0
fi

if [[ -d "$LEGACY_DST" ]]; then
  TRASHED="$HOME/.Trash/FusionFilamentBridge-$(date +%Y%m%d-%H%M%S)"
  mv "$LEGACY_DST" "$TRASHED"
  echo "Moved the old FusionFilament Bridge add-in to the Trash: $TRASHED"
fi

if [[ -n "$DEV" ]]; then
  mkdir -p "$ADDINS_DIR"
  if [[ -L "$ADDIN_DST" ]]; then
    rm "$ADDIN_DST"
  elif [[ -e "$ADDIN_DST" ]]; then
    mkdir -p "$TRASH_DIR"
    TRASHED="$TRASH_DIR/Filamatch-installed-$(date +%Y%m%d-%H%M%S)"
    mv "$ADDIN_DST" "$TRASHED"
    echo "Moved the installed copy to the Trash: $TRASHED"
  fi
  ln -s "$ADDIN_SRC" "$ADDIN_DST"
  if [[ "$(cd "$ADDIN_DST" && pwd -P)" != "$(cd "$ADDIN_SRC" && pwd -P)" || ! -f "$ADDIN_DST/Filamatch.manifest" ]]; then
    echo "error: the link does not reach the add-in: $ADDIN_DST" >&2
    exit 1
  fi
  echo
  echo "Development install: $ADDIN_DST -> $ADDIN_SRC"
  echo "After a code change, stop and run Filamatch in Scripts and Add-Ins (Shift+S)."
  echo "Run this script without --dev to go back to a normal copy."
  exit 0
fi

# A development link is removed first, so the copy below never writes into the repository.
if [[ -L "$ADDIN_DST" ]]; then
  rm "$ADDIN_DST"
  echo "Removed the development link; installing a copy."
fi
mkdir -p "$ADDIN_DST"
# Mirror the add-in folder, dropping caches and macOS metadata only.
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude 'result.json' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  "$ADDIN_SRC/" "$ADDIN_DST/"

# Fusion runs as the same user; make sure the files are readable regardless.
chmod -R u+rwX,go+rX "$ADDIN_DST"

# Verify the installed copy is byte-identical to the source.
if ! diff -r -q \
  --exclude '__pycache__' \
  --exclude 'result.json' \
  --exclude '*.pyc' \
  --exclude '.DS_Store' \
  "$ADDIN_SRC" "$ADDIN_DST" >/dev/null; then
  echo "error: installed copy differs from source after sync" >&2
  exit 1
fi

echo
echo "Installed successfully:"
echo "  $ADDIN_DST"
echo
echo "Next steps:"
echo "  1. Quit Autodesk Fusion completely and reopen it (add-ins are discovered at load)."
echo "  2. The add-in is configured to run on startup: the 'Filamatch'"
echo "     button appears on the ADD-INS panel and opens the HTML palette."
echo "  3. If it is not there yet, open Utilities > ADD-INS > Scripts and Add-Ins,"
echo "     select the Add-Ins tab, choose 'Filamatch' and click Run."
echo "  4. Diagnostics are printed to the TEXT COMMANDS palette and written to"
echo "     ~/Library/Logs/Filamatch/filamatch.log"
