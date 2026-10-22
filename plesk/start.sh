#!/bin/bash
# Start Kasflow op 127.0.0.1:8010 zonder root/systemd.
# Op Plesk: SSH als abonnement-gebruiker, of een geplande taak elke minuut.
#   bash plesk/start.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PIDFILE="$ROOT/backend/kasflow.pid"
LOGDIR="$ROOT/backend/logs"
mkdir -p "$ROOT/backend/uploads" "$LOGDIR"

if [ ! -x "$ROOT/venv/bin/gunicorn" ]; then
  echo "Geen venv. Eenmalig:"
  echo "  python3 -m venv venv"
  echo "  ./venv/bin/pip install -U pip"
  echo "  ./venv/bin/pip install -r backend/requirements.txt"
  exit 1
fi

if [ ! -f "$ROOT/.env" ]; then
  echo "Geen .env. Eenmalig:"
  echo "  cp plesk/env.example .env"
  echo "  python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
  echo "Vul in .env minstens KASFLOW_SECRET_KEY, KASFLOW_PUBLIC_URL,"
  echo "KASFLOW_ALLOWED_HOSTS en KASFLOW_DATABASE_URL (PostgreSQL)."
  echo "Voor dit domein bijvoorbeeld:"
  echo "  KASFLOW_PUBLIC_URL=https://kasflow-app.webnestiq.nl"
  echo "  KASFLOW_ALLOWED_HOSTS=kasflow-app.webnestiq.nl"
  exit 1
fi

LOCKFILE="$ROOT/backend/kasflow.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCKFILE"
  if ! flock -n 9; then
    echo "Een andere start loopt al"
    exit 0
  fi
fi

if [ -f "$PIDFILE" ]; then
  old="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "${old:-}" ] && kill -0 "$old" 2>/dev/null; then
    echo "Kasflow draait al (pid $old)"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

set -a
# shellcheck disable=SC1091
. "$ROOT/.env"
set +a

"$ROOT/venv/bin/gunicorn" app:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --chdir "$ROOT/backend" \
  --bind 127.0.0.1:8010 \
  --workers 1 \
  --pid "$PIDFILE" \
  --daemon \
  --proxy-headers \
  --forwarded-allow-ips=127.0.0.1 \
  --access-logfile "$LOGDIR/access.log" \
  --error-logfile "$LOGDIR/error.log"

sleep 2
if curl -fsS http://127.0.0.1:8010/api/health >/dev/null; then
  echo "Kasflow luistert op 127.0.0.1:8010"
  curl -sS http://127.0.0.1:8010/api/health
  echo
else
  echo "Start mislukt. Laatste regels van $LOGDIR/error.log:"
  tail -n 40 "$LOGDIR/error.log" || true
  exit 1
fi
