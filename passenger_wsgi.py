"""WSGI-ingang voor Plesk (Phusion Passenger).

FastAPI is een ASGI-app; Passenger verwacht WSGI. a2wsgi vertaalt dat. Websockets
gebruikt Kasflow niet, dus deze brug is voldoende. Liever gunicorn achter nginx
(zie PLESK.md); gebruik dit bestand als Plesk alleen Passenger aanbiedt.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
os.chdir(ROOT)
sys.path.insert(0, str(BACKEND))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv(ROOT / ".env")

os.environ.setdefault("KASFLOW_UPLOAD_DIR", str(BACKEND / "uploads"))

from a2wsgi import ASGIMiddleware
from app import app as asgi_app

application = ASGIMiddleware(asgi_app)
