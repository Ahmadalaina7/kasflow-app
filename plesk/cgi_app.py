"""WSGI/CGI-ingang zodat /api werkt zonder gunicorn op poort 8010."""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("KASFLOW_UPLOAD_DIR", str(ROOT / "backend" / "uploads"))


def fail(message: str, status: int = 503) -> None:
    sys.stdout.write(f"Status: {status}\r\n")
    sys.stdout.write("Content-Type: application/json; charset=utf-8\r\n\r\n")
    sys.stdout.write(json.dumps({"detail": message}, ensure_ascii=False))


def configure_path() -> None:
    uri = os.environ.get("REQUEST_URI") or os.environ.get("REDIRECT_URL") or "/"
    path, _, query = uri.partition("?")
    os.environ["SCRIPT_NAME"] = ""
    os.environ["PATH_INFO"] = path or "/"
    if query and not os.environ.get("QUERY_STRING"):
        os.environ["QUERY_STRING"] = query


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        configure_path()
        from a2wsgi import ASGIMiddleware
        from app import app as asgi_app
        from wsgiref.handlers import CGIHandler

        CGIHandler().run(ASGIMiddleware(asgi_app))
    except Exception:
        log = ROOT / "backend" / "logs"
        log.mkdir(parents=True, exist_ok=True)
        (log / "cgi.log").write_text(traceback.format_exc(), encoding="utf-8")
        fail(
            "Kasflow kon niet starten via CGI. Controleer backend/logs/cgi.log "
            "of voer in de documentroot uit: bash plesk/bootstrap.sh"
        )


if __name__ == "__main__":
    main()
