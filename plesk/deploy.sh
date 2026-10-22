#!/bin/bash
# Optioneel bij Plesk Git extra acties. CGI (kasflow.cgi) heeft dit niet
# per se nodig: de eerste /api-aanroep draait bootstrap.sh zelf.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bash "$ROOT/plesk/bootstrap.sh"
bash "$ROOT/plesk/start.sh" || true
