from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, overridable through environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    upstream_base_url: str = "https://hospital-directory.onrender.com"
    max_csv_rows: int = 20
    max_upload_bytes: int = 1_048_576

    # Upper bound on in-flight upstream requests across ALL jobs.
    max_concurrency: int = 20
    max_attempts: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0
    connect_timeout: float = 10.0
    # The upstream runs on Render's free tier; a cold start takes 20-50s.
    read_timeout: float = 60.0

    max_jobs: int = 500
    warm_upstream_on_startup: bool = True
    log_level: str = "INFO"
