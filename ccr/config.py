from pydantic_settings import BaseSettings, SettingsConfigDict


class CCRSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CCR_", env_file=".env", extra="ignore")

    port: int = 8080
    rlm_url: str = "http://localhost:8081"
    vllm_url: str = "http://localhost:11434"   # Ollama default; vLLM is :8000
    anthropic_fallback_key: str = ""
    fallback_enabled: bool = True

    # If set, rewrites the `model` field in every forwarded request.
    # Required for Ollama — model name must match a pulled model, e.g. "qwen2.5-coder:7b"
    # Leave empty to pass the model field through unchanged (vLLM, Anthropic).
    model_override: str = "qwen2.5-coder:7b"

    # Skill Pruner: trim tool schema array before sending to the local model
    skill_pruner_enabled: bool = True
    skill_pruner_max_tools: int = 6

    # Header Claude Code uses to advertise the current file
    active_file_header: str = "x-cc-active-file"
    repo_path_header: str = "x-cc-repo-path"


settings = CCRSettings()
