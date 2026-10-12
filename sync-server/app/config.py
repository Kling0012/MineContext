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
# Provider: "fake" (deterministic, no network), "openai_compat" (/embeddings),
# or "cloudflare" (Workers AI OpenAI-compatible /embeddings; credentials from
# an env-style file, same model as the agent-memory system).
EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "fake")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "fake-embed")
EMBED_API_KEY_FILE = os.environ.get("EMBED_API_KEY_FILE", "")
# "cloudflare" provider: path to an env-style file with CLOUDFLARE_ACCOUNT_ID
# and CLOUDFLARE_API_TOKEN (secrets stay file-referenced, never in env/args).
EMBED_CF_ENV_FILE = os.environ.get("EMBED_CF_ENV_FILE", "")
EMBED_CF_TIMEOUT_SEC = _float("EMBED_CF_TIMEOUT_SEC", 60.0)
EMBED_DIM = _int("EMBED_DIM", 1024)
EMBED_INTERVAL_SEC = _int("EMBED_INTERVAL_SEC", 5)
EMBED_BATCH = _int("EMBED_BATCH", 64)
# OpenAI-compatible /v1/embeddings relay (P1-C): max inputs per request.
EMBED_RELAY_MAX_INPUTS = _int("EMBED_RELAY_MAX_INPUTS", 64)
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


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


# agent-memory import (P3-A): pull AI work records into events as ai_work.
AM_IMPORT_ENABLED = _bool("AM_IMPORT_ENABLED", True)
AM_BASE_URL = os.environ.get("AM_BASE_URL", "").rstrip("/")
AM_IMPORT_INTERVAL = _int("AM_IMPORT_INTERVAL", 300)
# First run (no cursor yet) starts this many hours back.
AM_IMPORT_LOOKBACK_HOURS = _float("AM_IMPORT_LOOKBACK_HOURS", 48.0)
AM_TIMEOUT_SEC = _float("AM_TIMEOUT_SEC", 15.0)

# Daily report generation (P3-B): a day's timeline -> LLM -> report event.
# Uses the LLM_* settings for transport; the report itself is written by
# REPORT_LLM_MODEL, and over-long material is pre-summarized per time block
# by REPORT_LLM_FLASH_MODEL before the final integration call.
REPORT_LLM_MODEL = os.environ.get("REPORT_LLM_MODEL", "glm-5.3")
REPORT_LLM_FLASH_MODEL = os.environ.get("REPORT_LLM_FLASH_MODEL", "glm-5.3-flash")
# Long generation: judged ends only need 30s, a full report may need minutes.
REPORT_LLM_TIMEOUT_SEC = _float("REPORT_LLM_TIMEOUT_SEC", 180.0)
# Material longer than this many characters is summarized block by block.
REPORT_CHUNK_CHARS = _int("REPORT_CHUNK_CHARS", 12000)
# Nightly scheduler: 23:55 same-day report + 06:00 force-regen of yesterday
# (TZ_DEFAULT). Failures are logged only (fail-soft).
REPORT_AUTO_ENABLED = _bool("REPORT_AUTO_ENABLED", True)

# Blog auto-writing (P5-B): what the built-in LLM's generated posts become.
# "off" = the scheduler skips blog generation entirely; "draft" (default) =
# write into drafts only (published after the user reviews); "publish" =
# publish immediately (the pre-P5-B behavior).
BLOG_AUTO = os.environ.get("BLOG_AUTO", "draft").strip().lower() or "draft"
if BLOG_AUTO not in ("off", "draft", "publish"):
    BLOG_AUTO = "draft"

# MCP (/mcp) Host header allow-list; empty = no DNS-rebinding check (tailnet-only).
MCP_ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
