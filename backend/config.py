"""Runtime configuration for Kasflow.

Everything is read from environment variables so the same image runs locally,
on a staging server and in production. ``Settings.validate()`` refuses to start
a production process with insecure defaults (missing signing key, plain HTTP URL
or a SQLite database), which is the most common way a small app leaks sessions.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WEB_ROOT = BASE_DIR.parent
DEFAULT_DATABASE_URL = f"sqlite:///{(BASE_DIR / 'kasflow.sqlite3').as_posix()}"
# Lokaal bewaarde signeersleutel. Zonder deze sleutel zou elke herstart een
# nieuwe sleutel genereren en zou iedereen uitgelogd raken.
DEV_SECRET_FILE = BASE_DIR / ".kasflow-secret"


def development_secret_key() -> str:
    """Return the local signing key, creating it once.

    Sessions are signed with this key, so a stable key means a browser stays
    logged in after the development server or the laptop restarts. The file is
    git-ignored and only used outside production; production must pass its own
    ``KASFLOW_SECRET_KEY``.
    """
    try:
        existing = DEV_SECRET_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        existing = ""
    if len(existing) >= 32:
        return existing
    key = secrets.token_urlsafe(48)
    try:
        DEV_SECRET_FILE.write_text(key + "\n", encoding="utf-8")
        os.chmod(DEV_SECRET_FILE, 0o600)
    except OSError:  # pragma: no cover - read-only checkout
        return key
    return key

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional in the test image
    load_dotenv = None

if load_dotenv is not None:
    # Existing environment variables win, so Docker Compose and systemd stay in charge.
    load_dotenv(WEB_ROOT / ".env")
    load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    secret_key: str = ""
    database_url: str = DEFAULT_DATABASE_URL
    public_url: str = "http://127.0.0.1:8010"
    upload_dir: Path = BASE_DIR / "uploads"
    allowed_hosts: tuple[str, ...] = ()
    session_max_age: int = 43_200
    max_upload_bytes: int = 10 * 1024 * 1024
    mollie_api_key: str = ""
    kvk_api_key: str = ""
    kvk_base_url: str = ""
    allow_sqlite: bool = False

    @property
    def production(self) -> bool:
        return self.env == "production"

    @property
    def cookie_secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def host(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self.public_url).hostname or "localhost"

    @classmethod
    def from_environment(cls) -> "Settings":
        raw_hosts = os.getenv("KASFLOW_ALLOWED_HOSTS", "").strip()
        allowed = tuple(host.strip() for host in raw_hosts.split(",") if host.strip())
        if allowed:
            allowed = tuple(dict.fromkeys(allowed + ("127.0.0.1", "localhost")))
        try:
            session_max_age = int(os.getenv("KASFLOW_SESSION_MAX_AGE", "43200"))
        except ValueError:
            session_max_age = 43_200
        try:
            max_upload = int(os.getenv("KASFLOW_MAX_UPLOAD_MB", "10")) * 1024 * 1024
        except ValueError:
            max_upload = 10 * 1024 * 1024
        upload_dir = Path(os.getenv("KASFLOW_UPLOAD_DIR", str(BASE_DIR / "uploads"))).expanduser()
        environment = (os.getenv("KASFLOW_ENV", "development") or "development").strip().lower()
        secret_key = os.getenv("KASFLOW_SECRET_KEY", "").strip()
        if not secret_key and environment != "production":
            secret_key = development_secret_key()
        return cls(
            env=environment,
            secret_key=secret_key,
            database_url=(
                os.getenv("KASFLOW_DATABASE_URL", DEFAULT_DATABASE_URL) or DEFAULT_DATABASE_URL
            ).strip(),
            public_url=(os.getenv("KASFLOW_PUBLIC_URL", "http://127.0.0.1:8010") or "").rstrip("/"),
            upload_dir=upload_dir,
            allowed_hosts=allowed,
            session_max_age=session_max_age,
            max_upload_bytes=max_upload,
            mollie_api_key=os.getenv("KASFLOW_MOLLIE_API_KEY", "").strip(),
            kvk_api_key=os.getenv("KASFLOW_KVK_API_KEY", "").strip(),
            kvk_base_url=os.getenv("KASFLOW_KVK_BASE_URL", "").strip().rstrip("/"),
            allow_sqlite=os.getenv("KASFLOW_ALLOW_SQLITE", "").strip().lower()
            in {"1", "true", "yes", "on"},
        )

    def validate(self) -> None:
        """Fail fast on configuration that would be unsafe in production."""
        if not self.production:
            return
        problems: list[str] = []
        if len(self.secret_key) < 32:
            problems.append("KASFLOW_SECRET_KEY must be at least 32 characters")
        if not self.public_url.startswith("https://"):
            problems.append("KASFLOW_PUBLIC_URL must use https:// in production")
        if self.database_url.startswith("sqlite") and not self.allow_sqlite:
            problems.append("KASFLOW_DATABASE_URL must point at PostgreSQL in production")
        if problems:
            raise RuntimeError("Invalid production configuration: " + "; ".join(problems))
