"""Application configuration."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application
    app_name: str = "Disaster Response Coordination Platform"
    app_version: str = "1.0.0"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    api_prefix: str = "/api/v1"
    cors_origins: str = "*"

    # Database
    database_url: str = "postgresql+asyncpg://drp:drp_local_password@localhost:5432/disaster_response"
    test_database_url: str = (
        "postgresql+asyncpg://drp:drp_local_password@localhost:5432/disaster_response_test"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # Redis / cache
    redis_url: str = "redis://localhost:6379/0"
    cache_allow_memory_fallback: bool = True
    cache_ttl_geocode: int = 604_800
    cache_ttl_place_extract: int = 86_400
    cache_ttl_reports: int = 300
    cache_ttl_official: int = 900
    cache_stale_grace: int = 3_600
    cache_l1_ttl: int = 30

    # Auth
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 720
    seed_admin_password: str = "admin123"
    seed_contributor_password: str = "contributor123"
    seed_responder_password: str = "responder123"

    # External services
    social_api_base_url: str = "http://localhost:9000"
    official_api_base_url: str = "http://localhost:9000"
    geocoder_enabled: bool = True
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    nominatim_user_agent: str = "disaster-response-platform/1.0 (contact: you@example.com)"
    nominatim_rate_limit_per_sec: float = 1.0
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # Resilience
    http_connect_timeout_s: float = 2.0
    http_read_timeout_s: float = 5.0
    http_max_retries: int = 2
    circuit_fail_threshold: int = 5
    circuit_reset_timeout_s: float = 30.0

    # Rate limiting
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 120
    rate_limit_window_s: int = 60
    auth_rate_limit_requests: int = 10
    auth_rate_limit_window_s: int = 60

    # Geospatial guard rails
    max_radius_km: float = Field(default=500.0, gt=0)
    default_radius_km: float = Field(default=10.0, gt=0)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def llm_enabled(self) -> bool:
        """LLM place-extraction is strictly opt-in: no key means no network call."""
        return bool(self.anthropic_api_key.strip())

    @property
    def effective_database_url(self) -> str:
        return self.test_database_url if self.app_env == "test" else self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. FastAPI dependencies call this; module code uses `settings`."""
    return Settings()


settings = get_settings()
