#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HOME/aithercheck-venv"
ENV_FILE="$HOME/.aithercheck.env"

for tool in python3 unrar 7z; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "Hiányzó rendszereszköz: $tool" >&2
        exit 1
    }
done

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

read -r -p "qBittorrent WebUI URL, pl. http://127.0.0.1:10946: " QB_URL
read -r -p "qBittorrent username: " QB_USER
read -r -s -p "qBittorrent password: " QB_PASS
echo

read -r -p "Aither base URL [https://aither.cc]: " AITHER_BASE
AITHER_BASE=${AITHER_BASE:-https://aither.cc}

read -r -s -p "Aither API token: " AITHER_TOKEN
echo

read -r -s -p "TMDb API key: " TMDB_API_KEY
echo

read -r -p "Log file [$HOME/aither_missing.log]: " LOG_FILE
LOG_FILE=${LOG_FILE:-$HOME/aither_missing.log}

read -r -p "State file [$HOME/aither_checked_seen.json]: " STATE_FILE
STATE_FILE=${STATE_FILE:-$HOME/aither_checked_seen.json}

tmp_env="$(mktemp "$HOME/.aithercheck.env.XXXXXX")"
trap 'rm -f -- "$tmp_env"' EXIT
export QB_URL QB_USER QB_PASS AITHER_BASE AITHER_TOKEN TMDB_API_KEY LOG_FILE STATE_FILE
ENV_TARGET="$tmp_env" "$VENV/bin/python" - <<'PY'
import os
from dotenv import set_key

path = os.environ["ENV_TARGET"]
for key in (
    "QB_URL", "QB_USER", "QB_PASS", "AITHER_BASE", "AITHER_TOKEN",
    "TMDB_API_KEY", "LOG_FILE", "STATE_FILE",
):
    set_key(path, key, os.environ[key], quote_mode="always")
PY
chmod 600 "$tmp_env"
mv -f -- "$tmp_env" "$ENV_FILE"
trap - EXIT

mkdir -p "$HOME/.local/bin"
install -m 0755 "$SCRIPT_DIR/aither" "$HOME/.local/bin/aither"

echo
echo "Telepítés kész. Indítás: aither"
echo "Ha a parancs nem található azonnal, jelentkezz ki és vissza."
