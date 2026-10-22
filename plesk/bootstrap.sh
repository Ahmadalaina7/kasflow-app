#!/bin/bash
# Zet venv, packages en .env klaar. Wordt aangeroepen door proxy.php (eerste
# /api-verzoek) en kan met de hand: bash plesk/bootstrap.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/backend/uploads" "$ROOT/backend/logs"

python_bin() {
  for c in \
    /opt/plesk/python/3.13/bin/python3 \
    /opt/plesk/python/3.12/bin/python3 \
    /opt/plesk/python/3.11/bin/python3 \
    /opt/plesk/python/3.10/bin/python3 \
    python3.13 python3.12 python3.11 python3.10 python3
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

# Twee gelijktijdige verzoeken mogen niet allebei pip installeren.
LOCK="$ROOT/backend/logs/bootstrap.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -w 170 9 || { echo "Een andere bootstrap loopt nog." >&2; exit 1; }
fi

PY="$(python_bin)"
if [ ! -x "$ROOT/venv/bin/python" ] || ! "$ROOT/venv/bin/python" -V >/dev/null 2>&1; then
  rm -rf "$ROOT/venv"
  "$PY" -m venv "$ROOT/venv" || {
    echo "venv maken mislukt met $PY. Heeft de hosting de module venv?" >&2
    exit 1
  }
fi

"$ROOT/venv/bin/python" -m pip install -q -U pip >/dev/null 2>&1 || true
if ! "$ROOT/venv/bin/python" -m pip install -q -r "$ROOT/backend/requirements.txt"; then
  echo "pip install is mislukt. Meestal blokkeert de hosting uitgaand verkeer naar PyPI." >&2
  exit 1
fi

if ! "$ROOT/venv/bin/python" -c 'import fastapi, a2wsgi, dotenv, sqlalchemy, reportlab, httpx' >/dev/null 2>&1; then
  echo "De packages staan in de venv, maar zijn niet importeerbaar." >&2
  exit 1
fi

if [ ! -f "$ROOT/.env" ]; then
  KEY="$("$ROOT/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  HOST="$(basename "$ROOT")"
  case "$HOST" in
    httpdocs|httpsdocs|kasflow) HOST="kasflow-app.webnestiq.nl" ;;
  esac
  cat > "$ROOT/.env" <<EOF
KASFLOW_ENV=production
KASFLOW_SECRET_KEY=${KEY}
KASFLOW_PUBLIC_URL=https://${HOST}
KASFLOW_ALLOWED_HOSTS=${HOST},127.0.0.1,localhost
KASFLOW_ALLOW_SQLITE=1
KASFLOW_DATABASE_URL=sqlite:///${ROOT}/backend/kasflow.sqlite3
KASFLOW_UPLOAD_DIR=${ROOT}/backend/uploads
KASFLOW_KVK_API_KEY=
KASFLOW_KVK_BASE_URL=
KASFLOW_MOLLIE_API_KEY=
EOF
  chmod 600 "$ROOT/.env"
  echo ".env aangemaakt voor ${HOST}" >&2
fi

echo "Klaar: $("$ROOT/venv/bin/python" -V) in $ROOT/venv" >&2
