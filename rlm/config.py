from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class RLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RLM_", env_file=".env", extra="ignore")

    port: int = 8081
    token_budget: int = 8000       # max tokens in context pack
    worker_pool_size: int = 4      # subprocess REPL workers
    walker_timeout_ms: int = 500   # per-walker deadline

    # Where host filesystem is mounted inside container
    host_prefix: str = "/host"

    # Persistent SQLite store path (survives server restarts)
    store_path: str = str(Path.home() / ".cc-rlm" / "store.db")

    # Security: allowlist of repo root prefixes that RLM is permitted to read.
    # Empty list = allow any path (single-user local installs).
    # Set to e.g. ["/Users/me/projects", "/home/me/src"] on shared machines.
    allowed_repo_roots: list[str] = []

    # Feature flags
    cache_enabled: bool = True          # mtime-invalidated walker result cache
    session_dedup_enabled: bool = True  # skip unchanged files across turns
    repo_index_enabled: bool = True     # incremental live repo index
    bm25_enabled: bool = True           # BM25 fallback when import graph is sparse


settings = RLMSettings()
