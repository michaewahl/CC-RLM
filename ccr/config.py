from pydantic_settings import BaseSettings, SettingsConfigDict


class CCRSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CCR_", env_file=".env")

    port: int = 8080
    rlm_url: str = "http://localhost:8081"
    vllm_url: str = "http://localhost:8000"
    anthropic_fallback_key: str = ""
    fallback_enabled: bool = True

    # Header Claude Code uses to advertise the current file
    active_file_header: str = "x-cc-active-file"
    repo_path_header: str = "x-cc-repo-path"


settings = CCRSettings()
