from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Reads the repo-root .env (when run from backend/) or a local .env.
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    database_url: str
    redis_url: str
    hold_seconds: int = 300
    payment_seconds: int = 300
    max_payment_attempts: int = 3


settings = Settings()
