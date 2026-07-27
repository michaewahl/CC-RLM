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

    # Subagent routing split. When set, subagent traffic (Task-tool fan-out)
    # takes this route regardless of what the main agent is doing — the main
    # agent stays on frontier Claude while high-volume background work runs
    # locally. "" disables the split (all traffic routed identically).
    # Valid: "" | "repo_task" | "passthrough" | "fallback"
    subagent_route: str = ""

    # Header Claude Code uses to advertise the current file
    active_file_header: str = "x-cc-active-file"
    repo_path_header: str = "x-cc-repo-path"


settings = CCRSettings()
