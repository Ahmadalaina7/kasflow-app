#!/usr/bin/env bash
# Dagelijkse back-up op een Plesk-server. Zet in crontab van de abonnementsgebruiker:
#   15 3 * * * /var/www/vhosts/app.example.nl/kasflow/plesk/backup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
set -a
source "$ROOT/.env"
set +a

STAMP="$(date +%Y-%m-%d)"
DEST="${KASFLOW_BACKUP_DIR:-$HOME/backups}"
mkdir -p "$DEST"

python3 - <<'PY' "$KASFLOW_DATABASE_URL" "$DEST/db-$STAMP.sql.gz"
import gzip, os, subprocess, sys, urllib.parse
url = urllib.parse.urlparse(sys.argv[1].replace("postgresql+psycopg://", "postgres://", 1))
env = os.environ.copy()
env["PGPASSWORD"] = urllib.parse.unquote(url.password or "")
cmd = [
    "pg_dump",
    "-h", url.hostname or "localhost",
    "-p", str(url.port or 5432),
    "-U", urllib.parse.unquote(url.username or "kasflow"),
    "-d", url.path.lstrip("/"),
    "--no-owner",
]
with gzip.open(sys.argv[2], "wb") as archive:
    proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, check=True)
    archive.write(proc.stdout)
PY

UPLOADS="${KASFLOW_UPLOAD_DIR:-$ROOT/backend/uploads}"
tar czf "$DEST/uploads-$STAMP.tar.gz" -C "$UPLOADS" .
find "$DEST" -type f -mtime +30 -delete
ls -lh "$DEST/db-$STAMP.sql.gz" "$DEST/uploads-$STAMP.tar.gz"
