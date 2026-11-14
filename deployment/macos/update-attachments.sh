#!/bin/bash
# Run only after reviewing this checkout. Does not read or modify account secrets.
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
umask 077
ROOT=/Library/TelegramReadOnly
BACKUPS="$ROOT/attachment-backups"
CACHE="$ROOT/downloads"
SERVICE=_telegram_read
SOURCE=$(cd -- "$(dirname -- "$0")/../.." && pwd -P)
FILES=(src/telegram_mcp/__init__.py src/telegram_mcp/__main__.py src/telegram_mcp/cli.py
       src/telegram_mcp/client.py src/telegram_mcp/core.py src/telegram_mcp/handlers.py
       src/telegram_mcp/rich.py src/telegram_mcp/server.py src/telegram_mcp/media.py
       src/telegram_mcp/attachments.py launcher.py egress/telegram_egress.py)

fail() { printf '%s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail 'Run this reviewed script with sudo from your local terminal.'
[ -d "$ROOT" ] && [ ! -L "$ROOT" ] || fail 'The existing protected installation is missing.'
[ -x "$ROOT/python/bin/python3.12" ] || fail 'The protected Python installation is missing.'
SERVICE_UID=$(id -u "$SERVICE")
if pgrep -u "$SERVICE_UID" >/dev/null; then
    fail 'Stop the Telegram MCP server before updating or rolling back; its service identity is still running.'
fi
for rel in "${FILES[@]}"; do
    [ ! -L "$ROOT/$rel" ] || fail "Refusing a symlink destination: $rel"
done
[ ! -L "$ROOT/src" ] && [ ! -L "$ROOT/src/telegram_mcp" ] && [ ! -L "$ROOT/egress" ] || fail 'Refusing symlink code directories.'
[ ! -L "$BACKUPS" ] || fail 'Refusing a symlink backup directory.'

case "${1:-}" in
install)
    [ "$#" = 2 ] || fail 'Usage: update-attachments.sh install LOCAL_OWNER'
    OWNER=$2
    [[ "$OWNER" =~ ^[A-Za-z][A-Za-z0-9_-]*$ ]] || fail 'Use a local short account name.'
    OWNER_UID=$(id -u "$OWNER")
    [ "$OWNER_UID" != 0 ] && [ "$OWNER_UID" != "$SERVICE_UID" ] || fail 'Select the ordinary local owner account.'
    [ ! -e "$CACHE" ] || fail 'The downloads directory already exists; review the existing deployment before updating it.'
    [ ! -L "$CACHE" ] || fail 'Refusing a symlink download directory.'
    # Fail closed if another update changed the runtime since the reviewed base.
    "$ROOT/python/bin/python3.12" -I -B - "$ROOT" "$SOURCE/deployment/macos/baseline-sha256.json" "${FILES[@]}" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sys

root, manifest_path = map(Path, sys.argv[1:3])
manifest = json.loads(manifest_path.read_text())
if set(manifest) != set(sys.argv[3:]):
    raise SystemExit('Runtime baseline manifest does not match the reviewed file list.')
for name, expected in manifest.items():
    path = root / name
    if expected is None:
        matches = not os.path.lexists(path)
    else:
        matches = (path.is_file() and not path.is_symlink()
                   and hashlib.sha256(path.read_bytes()).hexdigest() == expected)
    if not matches:
        raise SystemExit('Installed runtime differs from reviewed baseline: ' + name)
PY
    /usr/bin/install -d -o root -g wheel -m 0700 "$BACKUPS"
    LABEL=$(date -u +%Y%m%dT%H%M%SZ)-$$
    BACKUP="$BACKUPS/$LABEL"
    STAGE=$(mktemp -d "$ROOT/.attachment-stage.XXXXXX")
    trap 'rm -rf -- "$STAGE"' EXIT
    mkdir -m 0700 "$BACKUP"
    # Snapshot only known source files. Never stage credentials or a chat policy.
    for rel in "${FILES[@]}"; do
        case "$rel" in
            launcher.py) src="$SOURCE/deployment/macos/launcher.py" ;;
            egress/telegram_egress.py) src="$SOURCE/deployment/macos/telegram_egress.py" ;;
            *) src="$SOURCE/$rel" ;;
        esac
        [ -f "$src" ] && [ ! -L "$src" ] || fail "Missing regular source file: $rel"
        mkdir -p "$STAGE/$(dirname "$rel")" "$BACKUP/$(dirname "$rel")"
        /usr/bin/install -o root -g wheel -m 0600 "$src" "$STAGE/$rel"
        if [ -e "$ROOT/$rel" ]; then
            [ -f "$ROOT/$rel" ] || fail "Installed target is not a regular file: $rel"
            cp -p "$ROOT/$rel" "$BACKUP/$rel"
        else
            touch "$BACKUP/$rel.absent"
        fi
    done
    "$ROOT/python/bin/python3.12" -I -B -c 'import pathlib,sys; [compile(p.read_bytes(), str(p), "exec") for p in pathlib.Path(sys.argv[1]).rglob("*.py")]' "$STAGE"
    touch "$BACKUP/READY"
    printf 'Backup ready; rollback label: %s\n' "$LABEL"
    /usr/bin/install -d -o "$SERVICE" -g "$SERVICE" -m 0700 "$CACHE"
    # The owner can traverse/read downloaded files, but cannot alter service data.
    chmod +a "user:$OWNER allow list,search,readattr,readextattr,readsecurity,directory_inherit" "$CACHE"
    chmod +a "user:$OWNER allow read,readattr,readextattr,readsecurity,file_inherit,only_inherit" "$CACHE"
    for rel in "${FILES[@]}"; do
        /usr/bin/install -o root -g wheel -m 0644 "$STAGE/$rel" "$ROOT/$rel"
    done
    printf 'Installed attachment support. Restart the Telegram MCP server.\nRollback label: %s\n' "$LABEL"
    ;;
rollback)
    [ "$#" = 2 ] || fail 'Usage: update-attachments.sh rollback BACKUP_LABEL'
    LABEL=$2
    [[ "$LABEL" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || fail 'Invalid backup label.'
    BACKUP="$BACKUPS/$LABEL"
    [ -d "$BACKUP" ] && [ ! -L "$BACKUP" ] && [ -f "$BACKUP/READY" ] || fail 'No completed backup with that label.'
    for rel in "${FILES[@]}"; do
        if [ -f "$BACKUP/$rel.absent" ]; then
            rm -f -- "$ROOT/$rel"
        else
            [ -f "$BACKUP/$rel" ] && [ ! -L "$BACKUP/$rel" ] || fail "Invalid backup file: $rel"
            /usr/bin/install -o root -g wheel -m 0644 "$BACKUP/$rel" "$ROOT/$rel"
        fi
    done
    printf 'Restored previous runtime. Restart MCP. Downloaded files were retained in %s.\n' "$CACHE"
    ;;
*) fail 'Usage: update-attachments.sh install LOCAL_OWNER | rollback BACKUP_LABEL' ;;
esac
