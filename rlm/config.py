from pydantic_settings import BaseSettings, SettingsConfigDict


class RLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RLM_", env_file=".env")

    port: int = 8081
    token_budget: int = 8000       # max tokens in context pack
    worker_pool_size: int = 4      # subprocess REPL workers
    walker_timeout_ms: int = 500   # per-walker deadline

    # Where host filesystem is mounted inside container
    host_prefix: str = "/host"


settings = RLMSettings()
