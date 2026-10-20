from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CAMPUSPAY_", env_file=".env", extra="ignore")
    database_url: str = "postgresql://campuspay:campuspay-local-only@127.0.0.1:55432/campuspay"
    redis_url: str = "redis://127.0.0.1:56379/0"
    rabbit_url: str = "amqp://campuspay:campuspay-local-only@127.0.0.1:5673/%2F"
    demo_password: str = Field(default="campuspay-demo-only", min_length=12)
    webhook_url: str = "http://127.0.0.1:8090/events"
    webhook_secret: str = Field(default="campuspay-webhook-demo-only-32chars", min_length=24)
    rate_limit: int = Field(default=240, ge=1)
    auth_rate_limit: int = Field(default=60, ge=1)

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("webhook URL must be an HTTP(S) endpoint without embedded credentials")
        _ = parsed.port  # Reject invalid/out-of-range ports at startup.
        return value
