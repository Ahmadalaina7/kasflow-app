"""Runtime configuration for Kasflow.

Everything is read from environment variables so the same image runs locally,
on a staging server and in production. ``Settings.validate()`` refuses to start
a production process with insecure defaults (missing signing key, plain HTTP URL
or a SQLite database), which is the most common way a small app leaks sessions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WEB_ROOT = BASE_DIR.parent
DEFAULT_DATABASE_URL = f"sqlite:///{(BASE_DIR / 'kasflow.sqlite3').as_posix()}"

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
        try:
            session_max_age = int(os.getenv("KASFLOW_SESSION_MAX_AGE", "43200"))
        except ValueError:
            session_max_age = 43_200
        try:
            max_upload = int(os.getenv("KASFLOW_MAX_UPLOAD_MB", "10")) * 1024 * 1024
        except ValueError:
            max_upload = 10 * 1024 * 1024
        upload_dir = Path(os.getenv("KASFLOW_UPLOAD_DIR", str(BASE_DIR / "uploads"))).expanduser()
        return cls(
            env=(os.getenv("KASFLOW_ENV", "development") or "development").strip().lower(),
            secret_key=os.getenv("KASFLOW_SECRET_KEY", "").strip(),
            database_url=(
                os.getenv("KASFLOW_DATABASE_URL", DEFAULT_DATABASE_URL) or DEFAULT_DATABASE_URL
            ).strip(),
            public_url=(os.getenv("KASFLOW_PUBLIC_URL", "http://127.0.0.1:8010") or "").rstrip("/"),
            upload_dir=upload_dir,
            allowed_hosts=allowed,
            session_max_age=session_max_age,
            max_upload_bytes=max_upload,
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
        if self.database_url.startswith("sqlite"):
            problems.append("KASFLOW_DATABASE_URL must point at PostgreSQL in production")
        if problems:
            raise RuntimeError("Invalid production configuration: " + "; ".join(problems))
