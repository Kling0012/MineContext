"""Configuration from environment variables."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
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

# Work-session end judging (P2-B).
# Provider: "fake" (deterministic, dev/E2E) or "openai_compat" (/chat/completions).
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai_compat")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-5.3-flash")
LLM_API_KEY_FILE = os.environ.get("LLM_API_KEY_FILE", "")
LLM_TIMEOUT_SEC = _float("LLM_TIMEOUT_SEC", 30.0)
SESSION_JUDGE_INTERVAL = _int("SESSION_JUDGE_INTERVAL", 120)
SESSION_END_THRESHOLD = _float("SESSION_END_THRESHOLD", 0.75)
# Minutes without events from any device before an active session is ended as idle.
IDLE_MINUTES = _float("IDLE_MINUTES", 15.0)
