"""Settings loaded from the project's .env file (one level above backend/)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_DIR / ".env", extra="ignore")

    database_url: str
    ollama_url: str = "http://localhost:11434"
    chat_model: str = "qwen3:8b"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768

    secret_key: str
    access_token_minutes: int = 60

    upload_dir: Path = PROJECT_DIR / "storage" / "uploads"
    max_upload_mb: int = 20


settings = Settings()
