"""Configuration from environment variables."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://sync:sync@localhost:5432/sync"
)
# Provider: "fake" (deterministic, no network) or "openai_compat" (/embeddings).
EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "fake")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "fake-embed")
EMBED_API_KEY_FILE = os.environ.get("EMBED_API_KEY_FILE", "")
EMBED_DIM = _int("EMBED_DIM", 1024)
EMBED_INTERVAL_SEC = _int("EMBED_INTERVAL_SEC", 5)
EMBED_BATCH = _int("EMBED_BATCH", 64)
TZ_DEFAULT = os.environ.get("TZ_DEFAULT", "Asia/Tokyo")
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
BIND_PORT = _int("BIND_PORT", 8000)
