#!/bin/sh
# LiteSpeed/Apache CGI: /api zonder poort 8010.
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
export HOME="$ROOT"
LOG="$ROOT/backend/logs"
mkdir -p "$LOG"

if [ ! -x "$ROOT/venv/bin/python" ] || [ ! -f "$ROOT/.env" ]; then
  bash "$ROOT/plesk/bootstrap.sh" >>"$LOG/autostart.log" 2>&1 || true
fi

if [ ! -x "$ROOT/venv/bin/python" ]; then
  printf 'Status: 503\r\nContent-Type: application/json; charset=utf-8\r\n\r\n'
  printf '%s' '{"detail":"Python-venv ontbreekt. Open SSH in de documentroot en run: bash plesk/bootstrap.sh"}'
  exit 0
fi

exec "$ROOT/venv/bin/python" "$ROOT/plesk/cgi_app.py"
