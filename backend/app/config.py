"""Configuration has no model or GPU dependencies."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDIO_", env_file=".env", extra="ignore")

    data_root: Path = Path("data")
    frontend_root: Path = Path("frontend/dist")
    password_hash: str = ""
    passwordless: bool = False
    public_origin: str = "http://localhost:8000"
    secure_cookie: bool = True
    mock_e621: bool = False
    published_neighbors: bool = True
    similarity_worker_token: str = Field(default="", repr=False, min_length=0)
    upstream_interval: float = Field(default=1.0, ge=0.0)
    transient_preview_bytes: int = Field(default=32 * 1024**2, ge=0, le=256 * 1024**2)
    library_root: Path | None = None
    library_max_bytes: int = Field(default=256 * 1024**2, ge=1024, le=2 * 1024**3)
    library_reserve_bytes: int = Field(default=128 * 1024**2, ge=0)
    library_timeout: int = Field(default=300, ge=10, le=1800)
    e621_username: str = ""
    e621_api_key: str = ""
    metadata_interval: float = Field(default=1.0, ge=1.0)
    media_interval: float = Field(default=0.25, ge=0.0)
    media_concurrency: int = Field(default=4, ge=1, le=8)
    download_concurrency: int = Field(default=2, ge=1, le=4)
    queue_limit: int = Field(default=64, ge=4, le=256)
    cache_bytes: int = Field(default=5 * 1024**3, ge=1024 * 1024)
    preview_max_bytes: int = Field(default=8 * 1024**2, ge=1024)
    preview_max_pixels: int = 20_000_000
    session_hours: int = Field(default=168, ge=1, le=720)
    cdn_hosts: str = "static1.e621.net,static2.e621.net"

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.similarity_worker_token and len(self.similarity_worker_token) < 32:
            raise ValueError("STUDIO_SIMILARITY_WORKER_TOKEN must contain at least 32 characters")
        if not self.passwordless and not re.fullmatch(r"scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}", self.password_hash):
            raise ValueError("Set STUDIO_PASSWORD_HASH using python -m backend.app.auth hash-password")
        if not self.mock_e621 and not self.e621_username.strip():
            raise ValueError("STUDIO_E621_USERNAME is required for a truthful User-Agent")
        if self.e621_api_key and not self.e621_username:
            raise ValueError("An e621 API key requires its username")
        if self.secure_cookie and not self.public_origin.startswith("https://"):
            raise ValueError("Secure cookies require an HTTPS STUDIO_PUBLIC_ORIGIN")
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.netloc
            or origin.username
            or origin.password
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("STUDIO_PUBLIC_ORIGIN must contain only scheme, host, and optional port")
        return self

    @property
    def session_cookie_name(self) -> str:
        # HTTP must not overwrite a Secure cookie retained from a previous HTTPS deployment.
        return "studio_session" if self.secure_cookie else "studio_session_http"

    @property
    def user_agent(self) -> str:
        return f"HydraTrainingStudio/0.1 (by {self.e621_username or 'local-mock'} on e621)"

    @property
    def allowed_hosts(self) -> set[str]:
        return {host.strip().lower() for host in self.cdn_hosts.split(",") if host.strip()}
