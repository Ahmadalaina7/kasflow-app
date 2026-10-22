#!/bin/bash
# Plesk > Git > Extra implementatie-acties:
#   bash plesk/deploy.sh
# Dit maakt venv/.env aan indien nodig en start gunicorn op 127.0.0.1:8010.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/backend/uploads" "$ROOT/backend/logs"

python_bin() {
  if command -v python3.11 >/dev/null 2>&1; then
    echo python3.11
  elif command -v python3 >/dev/null 2>&1; then
    echo python3
  else
    echo "Python 3 ontbreekt op deze hosting." >&2
    exit 1
  fi
}

PY="$(python_bin)"
if [ ! -x "$ROOT/venv/bin/python" ]; then
  "$PY" -m venv "$ROOT/venv"
fi
"$ROOT/venv/bin/pip" install -q -U pip
"$ROOT/venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"

if [ ! -f "$ROOT/.env" ]; then
  KEY="$("$ROOT/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  HOST="$(basename "$ROOT")"
  case "$HOST" in
    httpdocs|httpsdocs|kasflow) HOST="kasflow-app.webnestiq.nl" ;;
  esac
  DB_PATH="$ROOT/backend/kasflow.sqlite3"
  cat > "$ROOT/.env" <<EOF
KASFLOW_ENV=production
KASFLOW_SECRET_KEY=${KEY}
KASFLOW_PUBLIC_URL=https://${HOST}
KASFLOW_ALLOWED_HOSTS=${HOST},127.0.0.1,localhost
KASFLOW_ALLOW_SQLITE=1
KASFLOW_DATABASE_URL=sqlite:///${DB_PATH}
KASFLOW_UPLOAD_DIR=${ROOT}/backend/uploads
KASFLOW_KVK_API_KEY=
KASFLOW_KVK_BASE_URL=
KASFLOW_MOLLIE_API_KEY=
EOF
  chmod 600 "$ROOT/.env"
fi

PIDFILE="$ROOT/backend/kasflow.pid"
if [ -f "$PIDFILE" ]; then
  old="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "${old:-}" ]; then
    kill "$old" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PIDFILE"
fi

bash "$ROOT/plesk/start.sh"
