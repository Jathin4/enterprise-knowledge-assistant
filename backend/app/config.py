"""Settings loaded from the project's .env file (one level above backend/)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_DIR / ".env", extra="ignore")

    database_url: str
    # 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 first and waits ~2 s per call
    # before falling back, because Ollama only listens on IPv4.
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:8b"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768

    secret_key: str
    access_token_minutes: int = 60

    upload_dir: Path = PROJECT_DIR / "storage" / "uploads"
    max_upload_mb: int = 20

    # Tuned in S3·7 on the benchmark (see the experiment log): 400/60 + title beat 3000/400.
    chunk_max_chars: int = 400      # ≈ 100 tokens (1 token ≈ 4 characters of English)
    chunk_overlap_chars: int = 60   # ≈ 15 tokens repeated from the previous chunk
    embed_with_title: bool = True   # prefix the document title to the text that is embedded (not stored)

    top_k: int = 5                # chunks retrieved per question
    max_distance: float = 0.38    # refuse without calling the LLM above this (S3·7: 0 false refusals, 0.02 margin)
    num_ctx: int = 8192           # LLM context window: room for 5 chunks + prompt + answer


settings = Settings()
