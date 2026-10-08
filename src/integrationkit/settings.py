"""Application settings loaded from environment variables and `.env`."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SecretStr keeps the value out of repr(), str() and validation error messages.
    webhook_secret: SecretStr
    # Stripe's docs warn that a tolerance of 0 disables the recency check, so it is refused.
    signature_tolerance_seconds: int = Field(default=300, gt=0)
    max_body_bytes: int = Field(default=262_144, gt=0)
    database_url: str = (
        "postgresql+psycopg://integrationkit:integrationkit@localhost:5432/integrationkit"
    )
