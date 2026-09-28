"""Settings, read from env vars or .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "groq"
    llm_model: str = "openai/gpt-oss-120b"
    llm_temperature: float = 0.0

    github_token: str | None = None
    github_app_id: str | None = None
    github_private_key_path: Path | None = None
    github_webhook_secret: str | None = None
    github_api_url: str = "https://api.github.com"

    max_files: int = 15
    max_comments: int = 12
    min_confidence: float = 0.6
    max_tool_steps: int = 6  # 0 = no tools (used in eval)
    use_critic: bool = True
    max_concurrency: int = 3
    max_retries: int = 10  # groq's free tier rate limits a lot
    max_patch_chars: int = 12_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
