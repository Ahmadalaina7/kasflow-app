#!/bin/bash
# venv + .env, zonder gunicorn. Gebruikt door CGI en door deploy.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/backend/uploads" "$ROOT/backend/logs"

python_bin() {
  for c in \
    /opt/plesk/python/3.12/bin/python3 \
    /opt/plesk/python/3.11/bin/python3 \
    /opt/plesk/python/3.10/bin/python3 \
    python3.12 python3.11 python3.10 python3
  do
    if command -v "$c" >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
    if [ -x "$c" ]; then
      echo "$c"
      return 0
    fi
  done
  echo "Python 3 ontbreekt op deze hosting." >&2
  exit 1
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
