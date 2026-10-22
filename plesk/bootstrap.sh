#!/bin/bash
# Zet venv, packages en .env klaar. Wordt aangeroepen door proxy.php (eerste
# /api-verzoek) en kan met de hand: bash plesk/bootstrap.sh
#
# Waarom dit script zo omslachtig is: Plesk-hosting heeft vaak meerdere Python-
# versies naast elkaar, en een venv die ooit met een oude versie is gemaakt
# blijft anders voor altijd kapot. Daarom kiest dit script de nieuwste Python,
# maakt de venv opnieuw als die van een andere versie is, en schrijft het pip-
# logboek naar backend/logs/bootstrap.log.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/backend/uploads" "$ROOT/backend/logs"
LOG="$ROOT/backend/logs/bootstrap.log"

say() { echo "$*" >&2; echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >>"$LOG"; }
fail() { say "FOUT: $*"; exit 1; }

: >"$LOG"

# -- Python zoeken ---------------------------------------------------------
candidates=()
for c in /opt/plesk/python/*/bin/python3 /opt/plesk/python/*/bin/python \
         /usr/local/bin/python3.* /usr/bin/python3.* \
         /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] && candidates+=("$c")
done
if command -v python3 >/dev/null 2>&1; then
  candidates+=("$(command -v python3)")
fi

best=""
best_score=0
best_version=""
for c in "${candidates[@]}"; do
  version="$("$c" -c 'import sys;print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" || continue
  score="$("$c" -c 'import sys;print(sys.version_info[0]*10000+sys.version_info[1]*100+sys.version_info[2])' 2>/dev/null)" || continue
  if [ "${score:-0}" -gt "$best_score" ]; then
    best="$c"
    best_score="$score"
    best_version="$version"
  fi
done

[ -n "$best" ] || fail "Geen Python 3 gevonden op deze hosting. Vraag de host om Python 3.10 of nieuwer."
say "Nieuwste Python: $best_version ($best)"

mm="${best_version%.*}"
case "$mm" in
  3.8|3.9) REQUIREMENTS="$ROOT/backend/requirements-legacy.txt" ;;
  *) REQUIREMENTS="$ROOT/backend/requirements.txt" ;;
esac
say "Packages: $(basename "$REQUIREMENTS")"

# -- venv ------------------------------------------------------------------
lock="$ROOT/backend/logs/bootstrap.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$lock"
  flock -w 170 9 || fail "Een andere installatie loopt nog. Probeer het zo opnieuw."
fi

stamp="$ROOT/venv/.kasflow-python"
vpy="$ROOT/venv/bin/python"
recreate=0
[ -x "$vpy" ] || recreate=1
if [ "$recreate" = "0" ]; then
  current="$(cat "$stamp" 2>/dev/null || true)"
  # Geen stempel betekent een venv van vóór deze versie van het script.
  [ "$current" = "$mm" ] || recreate=1
fi
if [ "$recreate" = "0" ]; then
  "$vpy" -c 'import sys' >/dev/null 2>&1 || recreate=1
fi
if [ "$recreate" = "1" ]; then
  say "Nieuwe venv maken met $best_version"
  rm -rf "$ROOT/venv"
  "$best" -m venv "$ROOT/venv" >>"$LOG" 2>&1 \
    || fail "De venv kon niet worden gemaakt met $best_version. Mist de hosting de module venv (python3-venv)?"
  echo "$mm" >"$stamp"
fi

# -- pip en packages -------------------------------------------------------
if ! "$vpy" -m pip --version >>"$LOG" 2>&1; then
  say "pip ontbreekt in de venv; ensurepip proberen"
  "$vpy" -m ensurepip --upgrade >>"$LOG" 2>&1 || true
fi
"$vpy" -m pip --version >>"$LOG" 2>&1 \
  || fail "pip ontbreekt in de venv ($best_version) en kon niet worden geïnstalleerd."

"$vpy" -m pip install -q -U pip >>"$LOG" 2>&1 || true
say "Packages installeren (kan een minuut duren)"
if ! "$vpy" -m pip install -q -r "$REQUIREMENTS" >>"$LOG" 2>&1; then
  fail "pip install is mislukt met Python $best_version: $(tail -n 3 "$LOG" | tr '\n' ' ')"
fi

missing="$("$vpy" - <<'PY'
import importlib.util
modules = ["fastapi", "uvicorn", "sqlalchemy", "dotenv", "a2wsgi", "reportlab", "httpx", "itsdangerous"]
print(", ".join(m for m in modules if importlib.util.find_spec(m) is None))
PY
)"
[ -z "$missing" ] || fail "Na het installeren ontbreken nog: $missing."

# -- .env ------------------------------------------------------------------
if [ ! -f "$ROOT/.env" ]; then
  key="$("$vpy" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  host="$(basename "$ROOT")"
  case "$host" in
    httpdocs|httpsdocs|kasflow) host="kasflow-app.webnestiq.nl" ;;
  esac
  cat >"$ROOT/.env" <<EOF
KASFLOW_ENV=production
KASFLOW_SECRET_KEY=${key}
KASFLOW_PUBLIC_URL=https://${host}
KASFLOW_ALLOWED_HOSTS=${host},127.0.0.1,localhost
KASFLOW_ALLOW_SQLITE=1
KASFLOW_DATABASE_URL=sqlite:///${ROOT}/backend/kasflow.sqlite3
KASFLOW_UPLOAD_DIR=${ROOT}/backend/uploads
KASFLOW_KVK_API_KEY=
KASFLOW_KVK_BASE_URL=
KASFLOW_MOLLIE_API_KEY=
EOF
  chmod 600 "$ROOT/.env"
  say ".env aangemaakt voor ${host}"
fi

say "Klaar: Python $best_version, venv in $ROOT/venv"
