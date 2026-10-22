"""CGI-ingang: voert de FastAPI-app uit voor één verzoek (Plesk/LiteSpeed)."""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("KASFLOW_UPLOAD_DIR", str(ROOT / "backend" / "uploads"))

SECRETS = re.compile(r"://[^:/@\s]+:[^@\s]+@")


def mask(text: str) -> str:
    """Haalt wachtwoorden uit URL's voordat een melding naar de browser gaat."""
    return SECRETS.sub("://***:***@", text)


def fail(message: str, status: int = 503) -> None:
    sys.stdout.write(f"Status: {status}\r\n")
    sys.stdout.write("Content-Type: application/json; charset=utf-8\r\n\r\n")
    sys.stdout.write(json.dumps({"detail": mask(message)}, ensure_ascii=False))


def configure_path() -> None:
    """Zet de CGI-variabelen die a2wsgi en wsgiref verwachten.

    LiteSpeed vult lang niet alle CGI-variabelen in; zonder SERVER_NAME stopt
    a2wsgi met een KeyError nog voordat de app iets kan antwoorden.
    """
    uri = os.environ.get("REQUEST_URI") or os.environ.get("REDIRECT_URL") or "/"
    path, _, query = uri.partition("?")
    os.environ["SCRIPT_NAME"] = ""
    os.environ["PATH_INFO"] = path or "/"
    if query and not os.environ.get("QUERY_STRING"):
        os.environ["QUERY_STRING"] = query

    host = (os.environ.get("HTTP_HOST") or "").split(":")[0]
    os.environ.setdefault("SERVER_NAME", host or "localhost")
    https = os.environ.get("HTTPS", "").lower() in {"on", "1", "true"}
    os.environ.setdefault("SERVER_PORT", "443" if https else "80")
    os.environ.setdefault("SERVER_PROTOCOL", "HTTP/1.1")
    os.environ.setdefault("GATEWAY_INTERFACE", "CGI/1.1")
    os.environ.setdefault("REQUEST_METHOD", "GET")
    os.environ.setdefault("CONTENT_TYPE", "application/json")
    if not os.environ.get("CONTENT_LENGTH"):
        os.environ["CONTENT_LENGTH"] = "0"


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        configure_path()
        from a2wsgi import ASGIMiddleware
        from app import app as asgi_app
        from wsgiref.handlers import CGIHandler

        CGIHandler().run(ASGIMiddleware(asgi_app))
    except Exception as exc:  # noqa: BLE001 - de melding moet altijd uitkomen
        detail = f"{type(exc).__name__}: {exc}"
        try:
            log = ROOT / "backend" / "logs"
            log.mkdir(parents=True, exist_ok=True)
            (log / "cgi.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:  # noqa: BLE001 - loggen mag de melding niet blokkeren
            pass
        fail(
            f"Kasflow kon niet starten ({detail}). "
            "Volledige fout in backend/logs/cgi.log."
        )


if __name__ == "__main__":
    main()
